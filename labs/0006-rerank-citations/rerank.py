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

中文导读：两阶段检索中的第二阶段
--------------------------------
第一阶段（BM25 / hybrid）按词频与文档统计快速召回 candidate_k 个候选，
但每个 chunk 是独立打分的，看不到 query 与 chunk 之间的细粒度对应关系。
重排阶段拿到候选列表后，对每个 (query, chunk) 对重新打分：

    score = 0.50 * IDF 加权词覆盖率
          + 0.20 * query 原文子串命中
          + 0.10 * 标题命中率
          + 0.20 * 第一阶段排名先验（1/rank）
          + 0.15 * 第一阶段分数先验（按候选集最高分归一化）
          + 意图先验（_intent_bonus，封顶 0.35）

然后 `rerank()` 用 MMR 式贪心选择扣除“与已选结果的冗余度”，输出 top-N。
所有分数只用于排序，不是概率；权重之和也不等于 1。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from common import Chunk, tokenize


@dataclass
class RerankResult:
    """一条重排结果。

    - chunk_id: chunk 的稳定标识，用于回查原文、生成引用
    - score: 最终得分；在 `HeuristicReranker.rerank` 中可能已扣除冗余惩罚
    - base_score: 第一阶段（BM25 / hybrid）给出的原始分数
    - first_stage_rank: 在第一阶段候选列表中的名次，从 1 开始
    """

    chunk_id: str
    score: float
    base_score: float
    first_stage_rank: int


class HeuristicReranker:
    """离线启发式重排器。

    接口对齐生产环境的 cross-encoder（候选进、有序结果出），但内部用可解释的
    加权特征代替神经网络，因此不需要 API key，结果也完全可复现。
    """

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
        # 第一阶段只传回 chunk_id 与分数；重排要读原文和标题，所以需要这个索引。
        self.chunks_by_id = chunks_by_id
        # IDF 表来自第一阶段索引：越罕见的词，命中时贡献越大。
        self.idf = idf
        # 各特征权重。它们只决定相对排序，不是概率，也不做归一化。
        self.coverage_weight = coverage_weight
        self.exact_weight = exact_weight
        self.heading_weight = heading_weight
        self.rank_weight = rank_weight
        # MMR 冗余惩罚系数：新候选与已选候选越相似，扣分越多。
        self.redundancy_penalty = redundancy_penalty

    def score(
        self,
        query: str,
        chunk: Chunk,
        first_stage_rank: int,
        base_score: float = 0.0,
        max_base_score: float = 0.0,
    ) -> float:
        """给单个 (query, chunk) 对打一个重排分。

        与第一阶段“每个 chunk 独立打分”不同，这里同时看到 query 与 chunk
        文本，属于 pairwise 打分，因此能利用词覆盖、标题、短语等交互特征。
        """
        # 去重但保留顺序：同一个词在 query 中重复出现只算一次，
        # 避免重复词把覆盖率拉偏。
        query_terms = list(dict.fromkeys(tokenize(query)))
        # 转成 set 以便 O(1) 判断某个 query 词是否出现在 chunk 中。
        chunk_terms = set(tokenize(chunk.text))

        # IDF 加权覆盖率 = 命中的 query 词权重 / 全部 query 词权重。
        # 用 IDF 而非简单计数：命中“根因”这类稀有词，比命中常见词更有信息量。
        # 未登录词（idf 表中没有）按 0 计，因此不会拉低覆盖率。
        total_idf = sum(self.idf.get(term, 0.0) for term in query_terms)
        if total_idf > 0:
            hit_idf = sum(
                self.idf.get(term, 0.0)
                for term in query_terms
                if term in chunk_terms
            )
            coverage = hit_idf / total_idf
        else:
            # query 全是未登录词，无法判断覆盖程度，置 0。
            coverage = 0.0

        # 短语级特征：整个 query（去首尾空白、忽略大小写）作为子串出现，
        # 说明 chunk 很可能直接包含用户要的表述，给一个离散奖励。
        exact = 1.0 if query.strip().lower() in chunk.text.lower() else 0.0
        # 标题命中率：query 词有多少出现在 chunk 的 heading 中。
        # heading 由结构切分产生（形如 "故障 > 根因 > 数据库连接池"），
        # 命中标题通常意味着 chunk 的主题与问题一致。
        heading_terms = set(tokenize(chunk.heading))
        heading = (
            sum(1 for term in query_terms if term in heading_terms) / len(query_terms)
            if query_terms
            else 0.0
        )
        # 排名先验：第一阶段越靠前，先验越高（1/rank 衰减）。
        # 它起“锚定”作用，防止重排仅凭某个弱特征就彻底推翻第一阶段排序。
        rank_prior = 1.0 / max(1, first_stage_rank)
        # 分数先验：除以候选集内的最高分做归一化，消除不同 retriever 的
        # 分数尺度差异（BM25 分与 hybrid 分不能直接比较）。
        score_prior = (base_score / max_base_score) if max_base_score > 0 else 0.0

        # 加权求和。0.15 是分数先验的固定权重（未暴露为构造参数）；
        # 意图先验是领域规则的小幅加成，见 _intent_bonus。
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
        """对第一阶段候选重排，并选出至多 top_n 个结果。

        分两步：
        1) 对每个候选计算重排分（`score`）；
        2) 用 MMR 式贪心选择：每轮挑“重排分 - 冗余惩罚”最高的候选，
           在相关性之外兼顾结果多样性，避免 top-N 全是内容重复的 chunk。
        """
        scored: List[RerankResult] = []
        # 先取候选集中的最高第一阶段分，作为 score_prior 的归一化分母。
        max_base_score = max((score for _, score in candidates), default=0.0)
        # enumerate 从 1 开始：第一阶段名次天然是 1-based。
        for rank, (chunk_id, base_score) in enumerate(candidates, start=1):
            # 候选 id 理论上都应存在；防御式跳过，避免脏数据导致 KeyError。
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

        # 先按重排分降序；同分时按 chunk_id 升序，保证评测结果可复现。
        scored.sort(key=lambda item: (-item.score, item.chunk_id))
        selected: List[RerankResult] = []
        # 贪心选择：每轮都重新计算候选与“已选集合”的冗余度，
        # 因此同一候选在不同轮次得到的调整分可能不同。
        while scored and len(selected) < top_n:
            best = None
            best_adjusted = None
            for item in scored:
                redundancy = 0.0
                if selected:
                    # 冗余度取与任一已选 chunk 的最大 Jaccard 相似度：
                    # 只要和其中一个高度重复，就应该被压制。
                    redundancy = max(
                        _jaccard(
                            self.chunks_by_id[item.chunk_id].text,
                            self.chunks_by_id[other.chunk_id].text,
                        )
                        for other in selected
                    )
                # 相关性分减去冗余惩罚，得到本轮的选择依据。
                adjusted = item.score - self.redundancy_penalty * redundancy
                if best is None or adjusted > best_adjusted:
                    best = item
                    best_adjusted = adjusted
            # 注意：返回结果里的 score 记录的是选择时的调整分，
            # 而不是第一步算出的原始重排分。
            selected.append(
                RerankResult(
                    chunk_id=best.chunk_id,
                    score=best_adjusted,
                    base_score=best.base_score,
                    first_stage_rank=best.first_stage_rank,
                )
            )
            # 已选中的候选移出候选池，进入下一轮。
            scored = [item for item in scored if item.chunk_id != best.chunk_id]
        return selected


def _intent_bonus(query: str, chunk: Chunk) -> float:
    """Small domain prior: RepoMedic queries reveal their intent lexically.

    This is deliberately crude. A cross-encoder learns these patterns; the
    heuristic only exists so the offline lab can demonstrate the two-stage
    interface without an API key.

    中文说明：这是一组“问题类型 → 文档类型 / 关键词”的领域先验。
    例如问“根因 / 为什么”时偏向 incidents/，问“怎么 / 步骤 / 审批”时偏向
    runbooks/，问错误码时偏向 docs/error-codes.md。规则刻意简单，仅用于
    离线演示；生产环境应由 cross-encoder 从数据中学习这类模式。
    """
    q = query.lower()
    bonus = 0.0
    # 根因类问题：事故复盘（incidents/）比通用文档更可能给出原因；
    # 标题直接含“根因”的 chunk 再加分。
    if any(keyword in q for keyword in ("根因", "原因", "为什么", "导致", "引起")):
        if chunk.doc_id.startswith("incidents/"):
            bonus += 0.08
        if "根因" in chunk.heading:
            bonus += 0.12
    # 操作类问题：处置步骤通常在 runbooks/；涉及审批时看正文是否提到“审批”；
    # 问“谁负责”时，正文含“负责人 / on-call”的 chunk 更相关。
    if any(keyword in q for keyword in ("怎么", "如何", "流程", "步骤", "处置", "谁", "批准", "审批")):
        if chunk.doc_id.startswith("runbooks/"):
            bonus += 0.08
        if "审批" in chunk.text:
            bonus += 0.06
        if "谁" in q and ("负责人" in chunk.text or "on-call" in chunk.text):
            bonus += 0.12
    # 定义 / 报错类问题：错误码集中在 docs/error-codes.md；
    # 若标题末级（如 "E1042"）出现在 query 中，说明精确命中该错误码。
    if any(keyword in q for keyword in ("是什么", "错误码", "报错")):
        if chunk.doc_id == "docs/error-codes.md":
            bonus += 0.08
        leaf = chunk.heading.split(">")[-1].strip().lower()
        if leaf and leaf in q:
            bonus += 0.12
    # 配置类问题：config/ 下的文档优先。
    if any(keyword in q for keyword in ("配成", "配置", "maximum", "pool", "size")):
        if chunk.doc_id.startswith("config/"):
            bonus += 0.12
    # 数据库问题：配置文档最直接，架构文档次之。
    if any(keyword in q for keyword in ("数据库", "mysql", "postgres", "postgresql")):
        if chunk.doc_id.startswith("config/"):
            bonus += 0.15
        if chunk.doc_id == "docs/architecture.md":
            bonus += 0.05
    # 代码级信号：只有 query 和正文都提到 N+1 才加分，避免仅标题相关。
    if "n+1" in q and "n+1" in chunk.text.lower():
        bonus += 0.15
    # camelCase / .java / snake_case 等标识符通常意味着用户在看代码，
    # 因此 code/ 下的 chunk 优先。
    if re.search(r"[a-z][A-Z]|\.java|_[a-z]", query):
        if chunk.doc_id.startswith("code/"):
            bonus += 0.18
    # 中文“类”配合代码目录，进一步偏向代码文档。
    if "类" in q and chunk.doc_id.startswith("code/"):
        bonus += 0.08
    # 封顶，防止多条规则叠加后意图先验压过真正的内容相关性。
    return min(bonus, 0.35)


def _jaccard(a: str, b: str) -> float:
    """两个文本的 token 集合 Jaccard 相似度，用于衡量 chunk 间冗余。

    Jaccard = |交集| / |并集|，取值 0~1；两者都为空时返回 0，
    避免 0/0 除零。按 token 集合计算，忽略词序与重复。
    """
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
    """重排入口：按 method 选择“不重排”基线或启发式重排。

    - method="none": 消融基线，保留第一阶段顺序，用于衡量重排本身带来的增益；
    - method="heuristic": 调用 `HeuristicReranker`。
    """
    if method == "none":
        # 只截取前 top_n：first_stage_rank 就是它在第一阶段的名次。
        # base_score 同时填入 score，使返回结构与重排路径保持一致。
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
    # 显式报错而不是静默回退，避免评测时误用拼错的 method。
    raise ValueError("unknown reranker: %s" % method)
