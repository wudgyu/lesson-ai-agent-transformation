"""Shared plumbing for lesson 0004.

Lesson 0004 deliberately reuses lesson 0003's corpus, chunker, BM25 and
evaluation primitives instead of copying them. The import below is explicit:
this lab only works when `labs/0003-rag-retrieval/` is present.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

LESSON3 = Path(__file__).resolve().parent.parent / "0003-rag-retrieval"
if str(LESSON3) not in sys.path:
    sys.path.insert(0, str(LESSON3))

from rag import (  # noqa: E402
    BM25Index,
    Chunk,
    HybridIndex,
    LexicalVectorIndex,
    build_chunks,
    chunk_stats,
    chunks_from_index,
    load_documents,
    load_index,
    make_index,
    save_index,
    tokenize,
)

DEFAULT_INDEX = LESSON3 / "index" / "structure.json"
DEFAULT_CORPUS = LESSON3 / "corpus"
DEFAULT_RETRIEVAL_CASES = LESSON3 / "eval_cases.json"


def ensure_index(path: Optional[Path] = None) -> Path:
    """Return a usable index path, building it from lesson 3's corpus if needed."""
    index_path = Path(path) if path else DEFAULT_INDEX
    if index_path.exists():
        return index_path
    docs = load_documents(DEFAULT_CORPUS)
    chunks = build_chunks(docs, chunker="structure")
    index = make_index(docs, chunks, chunker="structure")
    save_index(index, index_path)
    return index_path


def load_chunks(index_path: Optional[Path] = None) -> List[Chunk]:
    return chunks_from_index(load_index(ensure_index(index_path)))


def build_retriever(name: str, chunks: Sequence[Chunk], candidate_k: int = 15):
    if name == "bm25":
        return BM25Index(chunks)
    if name == "hybrid":
        bm25 = BM25Index(chunks)
        return HybridIndex(bm25, LexicalVectorIndex(chunks), candidate_k=candidate_k)
    raise ValueError("unknown retriever: %s" % name)


def load_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def index_stats(index_path: Optional[Path] = None) -> Dict[str, float]:
    return chunk_stats(load_chunks(index_path))
