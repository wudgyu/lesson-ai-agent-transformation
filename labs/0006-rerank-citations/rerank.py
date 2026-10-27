"""Second-stage reranking for lesson 0006.

`HeuristicReranker` is a deterministic, offline stand-in for a cross-encoder
reranker. It scores query–chunk pairs jointly with cheap features:

- query-term coverage, weighted by BM25 IDF
- exact query substring
- heading match
- first-stage rank prior
- MMR-style redundancy penalty

A production system would swap this for a cross-encoder (Cohere Rerank,
Voyage rerank, or a local bge-reranker). The interface is intentionally the
same: candidates in, ordered results out.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from common import Chunk, tokenize


@dataclass
class RerankResult:
    chunk_id: str
    score: float
    base_score: float
    first_stage_rank: int


class HeuristicReranker:
    def __init__(
        self,
        chunks_by_id: Dict[str, Chunk],
        idf: Dict[str, float],
        coverage_weight: float = 0.50,
        exact_weight: float = 0.20,
        heading_weight: float = 0.10,
        rank_weight: float = 0.20,
        redundancy_penalty: float = 0.15,
    ) -> None:
        self.chunks_by_id = chunks_by_id
        self.idf = idf
        self.coverage_weight = coverage_weight
        self.exact_weight = exact_weight
        self.heading_weight = heading_weight
        self.rank_weight = rank_weight
        self.redundancy_penalty = redundancy_penalty

    def score(
        self,
        query: str,
        chunk: Chunk,
        first_stage_rank: int,
        base_score: float = 0.0,
        max_base_score: float = 0.0,
    ) -> float:
        query_terms = list(dict.fromkeys(tokenize(query)))
        chunk_terms = set(tokenize(chunk.text))

        total_idf = sum(self.idf.get(term, 0.0) for term in query_terms)
        if total_idf > 0:
            hit_idf = sum(
                self.idf.get(term, 0.0)
                for term in query_terms
                if term in chunk_terms
            )
            coverage = hit_idf / total_idf
        else:
            coverage = 0.0

        exact = 1.0 if query.strip().lower() in chunk.text.lower() else 0.0
        heading_terms = set(tokenize(chunk.heading))
        heading = (
            sum(1 for term in query_terms if term in heading_terms) / len(query_terms)
            if query_terms
            else 0.0
        )
        rank_prior = 1.0 / max(1, first_stage_rank)
        score_prior = (base_score / max_base_score) if max_base_score > 0 else 0.0

        return (
            self.coverage_weight * coverage
            + self.exact_weight * exact
            + self.heading_weight * heading
            + self.rank_weight * rank_prior
            + 0.15 * score_prior
            + _intent_bonus(query, chunk)
        )

    def rerank(
        self,
        query: str,
        candidates: Sequence[Tuple[str, float]],
        top_n: int = 5,
    ) -> List[RerankResult]:
        scored: List[RerankResult] = []
        max_base_score = max((score for _, score in candidates), default=0.0)
        for rank, (chunk_id, base_score) in enumerate(candidates, start=1):
            chunk = self.chunks_by_id.get(chunk_id)
            if chunk is None:
                continue
            scored.append(
                RerankResult(
                    chunk_id=chunk_id,
                    score=self.score(
                        query, chunk, rank, base_score, max_base_score
                    ),
                    base_score=base_score,
                    first_stage_rank=rank,
                )
            )

        scored.sort(key=lambda item: (-item.score, item.chunk_id))
        selected: List[RerankResult] = []
        while scored and len(selected) < top_n:
            best = None
            best_adjusted = None
            for item in scored:
                redundancy = 0.0
                if selected:
                    redundancy = max(
                        _jaccard(
                            self.chunks_by_id[item.chunk_id].text,
                            self.chunks_by_id[other.chunk_id].text,
                        )
                        for other in selected
                    )
                adjusted = item.score - self.redundancy_penalty * redundancy
                if best is None or adjusted > best_adjusted:
                    best = item
                    best_adjusted = adjusted
            selected.append(
                RerankResult(
                    chunk_id=best.chunk_id,
                    score=best_adjusted,
                    base_score=best.base_score,
                    first_stage_rank=best.first_stage_rank,
                )
            )
            scored = [item for item in scored if item.chunk_id != best.chunk_id]
        return selected


def _intent_bonus(query: str, chunk: Chunk) -> float:
    """Small domain prior: RepoMedic queries reveal their intent lexically.

    This is deliberately crude. A cross-encoder learns these patterns; the
    heuristic only exists so the offline lab can demonstrate the two-stage
    interface without an API key.
    """
    q = query.lower()
    bonus = 0.0
    if any(keyword in q for keyword in ("根因", "原因", "为什么", "导致", "引起")):
        if chunk.doc_id.startswith("incidents/"):
            bonus += 0.08
        if "根因" in chunk.heading:
            bonus += 0.12
    if any(keyword in q for keyword in ("怎么", "如何", "流程", "步骤", "处置", "谁", "批准", "审批")):
        if chunk.doc_id.startswith("runbooks/"):
            bonus += 0.08
        if "审批" in chunk.text:
            bonus += 0.06
        if "谁" in q and ("负责人" in chunk.text or "on-call" in chunk.text):
            bonus += 0.12
    if any(keyword in q for keyword in ("是什么", "错误码", "报错")):
        if chunk.doc_id == "docs/error-codes.md":
            bonus += 0.08
        leaf = chunk.heading.split(">")[-1].strip().lower()
        if leaf and leaf in q:
            bonus += 0.12
    if any(keyword in q for keyword in ("配成", "配置", "maximum", "pool", "size")):
        if chunk.doc_id.startswith("config/"):
            bonus += 0.12
    if any(keyword in q for keyword in ("数据库", "mysql", "postgres", "postgresql")):
        if chunk.doc_id.startswith("config/"):
            bonus += 0.15
        if chunk.doc_id == "docs/architecture.md":
            bonus += 0.05
    if "n+1" in q and "n+1" in chunk.text.lower():
        bonus += 0.15
    if re.search(r"[a-z][A-Z]|\.java|_[a-z]", query):
        if chunk.doc_id.startswith("code/"):
            bonus += 0.18
    if "类" in q and chunk.doc_id.startswith("code/"):
        bonus += 0.08
    return min(bonus, 0.35)


def _jaccard(a: str, b: str) -> float:
    left = set(tokenize(a))
    right = set(tokenize(b))
    if not left and not right:
        return 0.0
    return len(left & right) / len(left | right)


def rerank_candidates(
    query: str,
    candidates: Sequence[Tuple[str, float]],
    chunks_by_id: Dict[str, Chunk],
    idf: Dict[str, float],
    top_n: int = 5,
    method: str = "heuristic",
) -> List[RerankResult]:
    if method == "none":
        return [
            RerankResult(
                chunk_id=chunk_id,
                score=score,
                base_score=score,
                first_stage_rank=rank,
            )
            for rank, (chunk_id, score) in enumerate(candidates[:top_n], start=1)
        ]
    if method == "heuristic":
        return HeuristicReranker(chunks_by_id, idf).rerank(query, candidates, top_n)
    raise ValueError("unknown reranker: %s" % method)
