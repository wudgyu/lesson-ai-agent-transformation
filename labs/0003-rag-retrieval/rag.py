"""RAG retrieval primitives for lesson 0003.

只依赖标准库，Python 3.9 可运行。包含：

- 文档加载与两种 chunker：structure-aware / naive fixed-size
- 中英文混合 tokenizer（camelCase / snake_case / CJK bigram）
- BM25、lexical vector、可选 dense vector
- Reciprocal Rank Fusion (RRF)
- 检索评测所需的最小指标函数

设计原则：每个组件都能单独测试，不需要 API key。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

CORPUS_SUFFIXES = {".md", ".java", ".yml", ".yaml", ".txt"}

LATIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-/:#]*")
CJK_RE = re.compile(r"[一-鿿]+")
CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
SEP_RE = re.compile(r"[._\-/:#]+")


@dataclass
class Document:
    doc_id: str
    text: str
    title: str


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    heading: str
    text: str
    start_line: int
    end_line: int


# ---------------------------------------------------------------------------
# corpus loading
# ---------------------------------------------------------------------------


def load_documents(corpus_dir: Path) -> List[Document]:
    """按路径排序加载语料，保证 chunk 顺序与评测可复现。"""
    docs: List[Document] = []
    for path in sorted(Path(corpus_dir).rglob("*")):
        if not path.is_file() or path.suffix.lower() not in CORPUS_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        doc_id = path.relative_to(corpus_dir).as_posix()
        docs.append(Document(doc_id=doc_id, text=text, title=_document_title(path, text)))
    return docs


def _document_title(path: Path, text: str) -> str:
    if path.suffix.lower() == ".md":
        for line in text.splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    return path.name


# ---------------------------------------------------------------------------
# chunking
# ---------------------------------------------------------------------------


def _slug(text: str) -> str:
    slug = re.sub(r"[^\w一-鿿-]+", "-", text.lower(), flags=re.UNICODE)
    slug = re.sub(r"-+", "-", slug).strip("-_")
    return slug or "section"


def chunk_structure(doc: Document) -> List[Chunk]:
    """按 Markdown 二级标题切分；代码/配置整文件作为一个 chunk。

    每个 chunk 都会带上文档标题作为上下文前缀。这是最简单的一种
    contextual retrieval：不改原文，只让 chunk 不再“失去上下文”。
    """
    lines = doc.text.splitlines()
    if not doc.doc_id.lower().endswith(".md"):
        return [
            Chunk(
                chunk_id="%s#file" % doc.doc_id,
                doc_id=doc.doc_id,
                heading=doc.title,
                text=doc.text.strip(),
                start_line=1,
                end_line=max(1, len(lines)),
            )
        ]

    sections: List[Tuple[str, int, int]] = []  # (section title, start idx, end idx)
    title_idx = None
    for idx, line in enumerate(lines):
        if line.startswith("# ") and title_idx is None:
            title_idx = idx
            continue
        if line.startswith("## "):
            sections.append((line[3:].strip(), idx, len(lines)))

    for i in range(len(sections) - 1):
        sections[i] = (sections[i][0], sections[i][1], sections[i + 1][1])

    if not sections:
        body = "\n".join(lines[1:] if title_idx is not None else lines).strip()
        if not body:
            return []
        return [
            Chunk(
                chunk_id="%s#file" % doc.doc_id,
                doc_id=doc.doc_id,
                heading=doc.title,
                text="%s\n\n%s" % (doc.title, body),
                start_line=1,
                end_line=max(1, len(lines)),
            )
        ]

    chunks: List[Chunk] = []
    used = set()

    # 标题与第一个二级标题之间的前言（例如“摘要”之前的内容）
    first_start = sections[0][1]
    preamble_end = first_start
    preamble_start = (title_idx + 1) if title_idx is not None else 0
    preamble = "\n".join(lines[preamble_start:preamble_end]).strip()
    if preamble:
        heading = "%s > 概述" % doc.title
        chunks.append(
            Chunk(
                chunk_id=_unique_id("%s#overview" % doc.doc_id, used),
                doc_id=doc.doc_id,
                heading=heading,
                text="%s\n\n%s" % (doc.title, preamble),
                start_line=preamble_start + 1,
                end_line=max(preamble_start + 1, preamble_end),
            )
        )

    for title, start, end in sections:
        body = "\n".join(lines[start:end]).strip()
        if not body:
            continue
        heading = "%s > %s" % (doc.title, title)
        base_id = "%s#%s" % (doc.doc_id, _slug(title))
        chunks.append(
            Chunk(
                chunk_id=_unique_id(base_id, used),
                doc_id=doc.doc_id,
                heading=heading,
                text="%s\n\n%s" % (doc.title, body),
                start_line=start + 1,
                end_line=max(start + 1, end),
            )
        )
    return chunks


def _unique_id(base: str, used: set) -> str:
    candidate = base
    n = 2
    while candidate in used:
        candidate = "%s-%d" % (base, n)
        n += 1
    used.add(candidate)
    return candidate


def chunk_naive(doc: Document, size: int = 400, overlap: int = 80) -> List[Chunk]:
    """固定字符窗口切分，故意不尊重标题/函数边界，作为对照组。

    会尽量在窗口末尾的换行或空格处截断，但仍然可能把一句话、
    一个错误码或一个函数拆到两个 chunk 里。
    """
    if size <= 0:
        raise ValueError("size must be positive")
    if overlap < 0 or overlap >= size:
        raise ValueError("overlap must be >= 0 and < size")

    text = doc.text
    chunks: List[Chunk] = []
    start = 0
    index = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            window_start = start + max(1, size // 2)
            newline = text.rfind("\n", window_start, end)
            space = text.rfind(" ", window_start, end)
            cut = max(newline, space)
            if cut > start:
                end = cut + 1
        piece = text[start:end].strip()
        if piece:
            start_line = text.count("\n", 0, start) + 1
            end_line = text.count("\n", 0, end) + 1
            chunks.append(
                Chunk(
                    chunk_id="%s#c%03d" % (doc.doc_id, index),
                    doc_id=doc.doc_id,
                    heading=doc.title,
                    text=piece,
                    start_line=start_line,
                    end_line=max(start_line, end_line),
                )
            )
            index += 1
        if end >= len(text):
            break
        next_start = end - overlap
        if next_start <= start:
            next_start = end
        start = next_start
    return chunks


def build_chunks(
    docs: Sequence[Document],
    chunker: str = "structure",
    size: int = 400,
    overlap: int = 80,
) -> List[Chunk]:
    chunks: List[Chunk] = []
    for doc in docs:
        if chunker == "structure":
            chunks.extend(chunk_structure(doc))
        elif chunker == "naive":
            chunks.extend(chunk_naive(doc, size=size, overlap=overlap))
        else:
            raise ValueError("unknown chunker: %s" % chunker)
    seen = set()
    for chunk in chunks:
        if chunk.chunk_id in seen:
            raise ValueError("duplicate chunk_id: %s" % chunk.chunk_id)
        seen.add(chunk.chunk_id)
    return chunks


# ---------------------------------------------------------------------------
# tokenizer
# ---------------------------------------------------------------------------


def tokenize(text: str) -> List[str]:
    """中英文混合 tokenizer。

    - 英文/代码标识符：保留整串，同时拆 camelCase 与 snake_case；
    - 中文：切成重叠 bigram（无需分词词典）；
    - 标点与空白不产生 token。

    这不是“最好的” tokenizer，而是一个可解释、可测试的基线。
    生产系统应该按语料语言与代码风格校准分词。
    """
    tokens: List[str] = []
    for match in LATIN_RE.finditer(text):
        raw = match.group(0).strip("._-/:#")
        if not raw:
            continue
        lower = raw.lower()
        if len(lower) >= 2:
            tokens.append(lower)
        for part in SEP_RE.split(raw):
            if not part:
                continue
            part_lower = part.lower()
            if len(part_lower) >= 2 and part_lower != lower:
                tokens.append(part_lower)
            for sub in CAMEL_RE.findall(part):
                sub_lower = sub.lower()
                if len(sub_lower) >= 2 and sub_lower not in (lower, part_lower):
                    tokens.append(sub_lower)

    for match in CJK_RE.finditer(text):
        run = match.group(0)
        if len(run) == 1:
            tokens.append(run)
            continue
        for i in range(len(run) - 1):
            tokens.append(run[i : i + 2])
    return tokens


# ---------------------------------------------------------------------------
# retrievers
# ---------------------------------------------------------------------------


class BM25Index:
    """Okapi BM25。k1 控制词频饱和，b 控制文档长度归一化。"""

    def __init__(
        self,
        chunks: Sequence[Chunk],
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        self.chunks = list(chunks)
        self.k1 = k1
        self.b = b
        self.doc_tokens = [tokenize(chunk.text) for chunk in self.chunks]
        self.doc_len = [len(tokens) for tokens in self.doc_tokens]
        self.doc_tf = [Counter(tokens) for tokens in self.doc_tokens]
        self.avgdl = (sum(self.doc_len) / len(self.doc_len)) if self.doc_len else 0.0

        df: Counter = Counter()
        for tokens in self.doc_tokens:
            df.update(set(tokens))
        n_docs = len(self.chunks)
        self.idf = {
            term: math.log(1.0 + (n_docs - freq + 0.5) / (freq + 0.5))
            for term, freq in df.items()
        }

    def search(self, query: str, k: int = 10) -> List[Tuple[str, float]]:
        query_terms = list(dict.fromkeys(tokenize(query)))
        if not query_terms or not self.chunks:
            return []

        scored: List[Tuple[str, float]] = []
        for idx, chunk in enumerate(self.chunks):
            tf = self.doc_tf[idx]
            length = self.doc_len[idx]
            score = 0.0
            for term in query_terms:
                freq = tf.get(term)
                if not freq:
                    continue
                idf = self.idf.get(term, 0.0)
                denom = freq + self.k1 * (1.0 - self.b + self.b * length / self.avgdl)
                score += idf * (freq * (self.k1 + 1.0)) / denom
            if score > 0:
                scored.append((chunk.chunk_id, score))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:k]

    def scores(self, query: str) -> Dict[str, float]:
        return dict(self.search(query, k=len(self.chunks)))


class LexicalVectorIndex:
    """离线占位的“向量检索”：hashed TF + cosine。

    它只捕捉词面重合，不是语义 embedding。用途是让整条索引/融合/评测
    管线在没有 API key 时也能跑通；真实的语义召回需要 dense embedding。
    """

    def __init__(self, chunks: Sequence[Chunk], dim: int = 1024) -> None:
        self.chunks = list(chunks)
        self.dim = dim
        self.vectors: Dict[str, List[float]] = {}
        for chunk in self.chunks:
            self.vectors[chunk.chunk_id] = self._vectorize(tokenize(chunk.text))

    def _vectorize(self, tokens: Sequence[str]) -> List[float]:
        vec = [0.0] * self.dim
        counts = Counter(tokens)
        for token, count in counts.items():
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            idx = value % self.dim
            sign = 1.0 if (value >> 63) & 1 else -1.0
            vec[idx] += sign * (1.0 + math.log(count))
        return _l2_normalize(vec)

    def search(self, query: str, k: int = 10) -> List[Tuple[str, float]]:
        return self.search_by_vector(self._vectorize(tokenize(query)), k)

    def search_by_vector(
        self, query_vector: Sequence[float], k: int = 10
    ) -> List[Tuple[str, float]]:
        if len(query_vector) != self.dim:
            raise ValueError(
                "vector dimension mismatch: query=%d index=%d"
                % (len(query_vector), self.dim)
            )
        scored = [
            (chunk_id, _dot(query_vector, vec))
            for chunk_id, vec in self.vectors.items()
        ]
        scored = [item for item in scored if item[1] > 0]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:k]


class DenseIndex:
    """对预计算 embedding 做 cosine 检索；query 向量由调用方提供。"""

    def __init__(
        self,
        chunks: Sequence[Chunk],
        vectors: Dict[str, Sequence[float]],
        embed_query,
    ) -> None:
        self.chunks = list(chunks)
        self.vectors: Dict[str, List[float]] = {}
        for chunk in self.chunks:
            if chunk.chunk_id not in vectors:
                raise ValueError("missing embedding for chunk: %s" % chunk.chunk_id)
            self.vectors[chunk.chunk_id] = _l2_normalize(list(vectors[chunk.chunk_id]))
        self.embed_query = embed_query
        dims = {len(vec) for vec in self.vectors.values()}
        if len(dims) != 1:
            raise ValueError("embedding dimension mismatch across chunks: %s" % sorted(dims))
        self.dim = dims.pop() if dims else 0

    def search(self, query: str, k: int = 10) -> List[Tuple[str, float]]:
        query_vector = self.embed_query(query)
        return self.search_by_vector(query_vector, k)

    def search_by_vector(
        self, query_vector: Sequence[float], k: int = 10
    ) -> List[Tuple[str, float]]:
        if len(query_vector) != self.dim:
            raise ValueError(
                "vector dimension mismatch: query=%d index=%d"
                % (len(query_vector), self.dim)
            )
        normalized = _l2_normalize(list(query_vector))
        scored = [
            (chunk_id, _dot(normalized, vec))
            for chunk_id, vec in self.vectors.items()
        ]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:k]


class HybridIndex:
    """BM25 与向量检索各自召回 candidate_k，再用 RRF 融合。"""

    def __init__(
        self,
        bm25: BM25Index,
        vector,
        candidate_k: int = 50,
        rrf_k: int = 60,
    ) -> None:
        self.bm25 = bm25
        self.vector = vector
        self.candidate_k = candidate_k
        self.rrf_k = rrf_k

    def search(self, query: str, k: int = 10) -> List[Tuple[str, float]]:
        bm25_ranking = [
            chunk_id for chunk_id, _ in self.bm25.search(query, self.candidate_k)
        ]
        vector_ranking = [
            chunk_id for chunk_id, _ in self.vector.search(query, self.candidate_k)
        ]
        return rrf_fuse([bm25_ranking, vector_ranking], k=self.rrf_k)[:k]


def rrf_fuse(
    rankings: Sequence[Sequence[str]],
    k: int = 60,
    weights: Optional[Sequence[float]] = None,
) -> List[Tuple[str, float]]:
    """Reciprocal Rank Fusion：score = Σ weight / (k + rank)。

    用排名而不是原始分数，避免 BM25 分数与 cosine 分数的尺度不可比。
    """
    if weights is None:
        weights = [1.0] * len(rankings)
    if len(weights) != len(rankings):
        raise ValueError("weights length must match rankings")
    if k <= 0:
        raise ValueError("k must be positive")

    scores: Dict[str, float] = {}
    for ranking, weight in zip(rankings, weights):
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _l2_normalize(vector: List[float]) -> List[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


# ---------------------------------------------------------------------------
# evaluation helpers
# ---------------------------------------------------------------------------


def match_evidence(chunk: Chunk, evidence: Dict[str, str]) -> bool:
    """证据命中必须同时满足：同一文档 + chunk 文本包含证据短语。"""
    if chunk.doc_id != evidence.get("doc"):
        return False
    needle = evidence.get("contains", "")
    return needle.lower() in chunk.text.lower()


def score_ranking(
    ranking: Sequence[str],
    chunks_by_id: Dict[str, Chunk],
    case: Dict[str, Any],
    k: int,
) -> Dict[str, Any]:
    top = list(ranking[:k])
    top_chunks = [chunks_by_id[chunk_id] for chunk_id in top if chunk_id in chunks_by_id]
    evidence = case.get("evidence", [])

    matched: List[Dict[str, str]] = []
    missing: List[Dict[str, str]] = []
    for item in evidence:
        if any(match_evidence(chunk, item) for chunk in top_chunks):
            matched.append(item)
        else:
            missing.append(item)

    first_rank = None
    for rank, chunk in enumerate(top_chunks, start=1):
        if any(match_evidence(chunk, item) for item in evidence):
            first_rank = rank
            break

    useful = sum(
        1
        for chunk in top_chunks
        if any(match_evidence(chunk, item) for item in evidence)
    )
    context_chars = sum(len(chunk.text) for chunk in top_chunks)

    recall = (len(matched) / len(evidence)) if evidence else 0.0
    return {
        "recall": recall,
        "full": bool(evidence) and not missing,
        "mrr": (1.0 / first_rank) if first_rank else 0.0,
        "first_rank": first_rank,
        "precision": (useful / len(top_chunks)) if top_chunks else 0.0,
        "context_chars": context_chars,
        "matched": matched,
        "missing": missing,
        "top": top,
    }


def summarize(rows: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    total = len(rows)
    if total == 0:
        return {
            "cases": 0,
            "recall": 0.0,
            "full": 0.0,
            "mrr": 0.0,
            "precision": 0.0,
            "avg_context_chars": 0.0,
        }
    return {
        "cases": total,
        "recall": sum(row["recall"] for row in rows) / total,
        "full": sum(1.0 for row in rows if row["full"]) / total,
        "mrr": sum(row["mrr"] for row in rows) / total,
        "precision": sum(row.get("precision", 0.0) for row in rows) / total,
        "avg_context_chars": sum(row.get("context_chars", 0) for row in rows) / total,
    }


# ---------------------------------------------------------------------------
# index serialization
# ---------------------------------------------------------------------------


def make_index(
    docs: Sequence[Document],
    chunks: Sequence[Chunk],
    chunker: str,
    params: Optional[Dict[str, Any]] = None,
    embedding_model: Optional[str] = None,
    vectors: Optional[Dict[str, Sequence[float]]] = None,
) -> Dict[str, Any]:
    return {
        "chunker": chunker,
        "params": dict(params or {}),
        "documents": [doc.doc_id for doc in docs],
        "chunks": [asdict(chunk) for chunk in chunks],
        "embedding_model": embedding_model,
        "vectors": {key: list(value) for key, value in (vectors or {}).items()},
    }


def save_index(index: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_index(path: Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def chunks_from_index(index: Dict[str, Any]) -> List[Chunk]:
    return [Chunk(**item) for item in index.get("chunks", [])]


def chunk_stats(chunks: Sequence[Chunk]) -> Dict[str, float]:
    lengths = [len(chunk.text) for chunk in chunks]
    if not lengths:
        return {"chunks": 0, "avg_chars": 0.0, "max_chars": 0, "min_chars": 0}
    return {
        "chunks": len(lengths),
        "avg_chars": sum(lengths) / len(lengths),
        "max_chars": max(lengths),
        "min_chars": min(lengths),
    }
