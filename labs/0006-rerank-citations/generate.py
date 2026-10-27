"""Grounded answer generation for lesson 0006.

Two backends:

- `extractive`: deterministic sentence selection. It is a pipeline demo, not
  an LLM evaluation. Useful without an API key.
- `llm`: any OpenAI-compatible chat endpoint. The prompt demands JSON with
  an answer plus quote-level citations.

The citation protocol is deliberately generic: source tags `[S1]`, `[S2]`...
and an explicit "insufficient evidence" escape hatch. Anthropic's Citations
API is the production-grade structured alternative; see the lesson.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from citation import (
    GroundedAnswer,
    Citation,
    Source,
    normalize_text,
    parse_grounded_answer,
)
from common import Chunk, tokenize

def load_dotenv(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def build_sources(chunks: Sequence[Chunk]) -> List[Source]:
    sources: List[Source] = []
    for index, chunk in enumerate(chunks, start=1):
        sources.append(
            Source(
                source_id="S%d" % index,
                doc_id=chunk.doc_id,
                heading=chunk.heading,
                text=chunk.text,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
            )
        )
    return sources


def build_prompt(query: str, sources: Sequence[Source]) -> str:
    lines = [
        "你是 RepoMedic 的排障助手。只能依据下面提供的来源回答，不要使用外部知识。",
        "",
        "要求：",
        "1. 每个事实性结论后面必须标注来源编号，例如 [S1]。",
        "2. 只能引用下面出现过的编号；不要编造来源。",
        "3. citations 数组里，为每个编号给出支持该结论的原文句子 quote；quote 必须是来源中的原句。",
        "4. 如果来源不足以回答，answer 写“证据不足：...”并说明缺什么，citations 为空。",
        "5. 只输出 JSON，不要输出 Markdown 代码块或额外解释。",
        "",
        '输出格式：{"answer": "...", "citations": [{"source_id": "S1", "quote": "..."}]}',
        "",
        "问题：%s" % query,
        "",
        "来源：",
    ]
    for source in sources:
        lines.append(
            "[%s] doc=%s heading=%s lines=%d-%d\n%s"
            % (
                source.source_id,
                source.doc_id,
                source.heading,
                source.start_line,
                source.end_line,
                source.text,
            )
        )
    return "\n\n".join(lines)


def chat_complete(
    messages: Sequence[Dict[str, str]],
    model: Optional[str] = None,
    temperature: float = 0.1,
) -> Tuple[str, Dict[str, int]]:
    load_dotenv()
    api_key = os.getenv("LLM_API_KEY")
    if not api_key:
        raise RuntimeError(
            "LLM_API_KEY is not set. Copy .env.example to .env and fill it in, "
            "or run with --backend extractive."
        )
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - only on the real API path
        raise RuntimeError(
            "openai package is not installed. Run: pip install -r requirements.txt"
        ) from exc

    client = OpenAI(
        api_key=api_key,
        base_url=os.getenv("LLM_BASE_URL") or None,
        timeout=60.0,
        max_retries=2,
    )
    response = client.chat.completions.create(
        model=model or os.getenv("LLM_MODEL", "gpt-4o-mini"),
        messages=list(messages),
        temperature=temperature,
    )
    content = response.choices[0].message.content or ""
    usage = {}
    if response.usage is not None:
        usage = {
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens": response.usage.total_tokens,
        }
    return content, usage


def extractive_answer(
    query: str,
    sources: Sequence[Source],
    min_overlap: int = 2,
) -> GroundedAnswer:
    """Pick the best-matching block/line window, then cite it.

    This is an extractive pipeline demo, not an LLM. It deliberately returns
    a span from one source so the citation checker has something concrete to
    verify. A real generator can synthesize across sources.
    """
    query_terms = set(tokenize(query))
    best: Optional[Tuple[float, Source, str]] = None
    for source_rank, source in enumerate(sources, start=1):
        for span in _candidate_spans(source.text):
            body = _strip_headings(span)
            if len(body.strip()) < 4:
                continue
            span_terms = set(tokenize(span))
            overlap = len(query_terms & span_terms)
            if overlap < min_overlap:
                continue
            score = overlap / max(1.0, len(span_terms) ** 0.5)
            score += 0.15 / source_rank
            if best is None or score > best[0]:
                best = (score, source, body)

    if best is None:
        return GroundedAnswer(
            answer="证据不足：检索到的来源没有覆盖这个问题。",
            citations=[],
        )

    _, source, body = best
    body = _truncate(body, 500)
    answer = "%s [%s]" % (body, source.source_id)
    return GroundedAnswer(
        answer=answer,
        citations=[Citation(source_id=source.source_id, quote=body)],
    )


def _candidate_spans(text: str) -> List[str]:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    spans: List[str] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        # Merge a heading-only block with the paragraph that follows it.
        if all(line.strip().startswith("#") for line in block.splitlines()):
            if index + 1 < len(blocks):
                block = block + "\n" + blocks[index + 1]
                index += 1
        spans.append(block)
        index += 1

    for span in list(spans):
        if len(span) > 700:
            lines = span.splitlines()
            best_line = max(
                range(len(lines)),
                key=lambda i: len(set(tokenize(lines[i]))),
            )
            spans.append("\n".join(lines[max(0, best_line - 3) : best_line + 6]))
    return spans


def _strip_headings(text: str) -> str:
    lines = [line for line in text.splitlines() if not line.strip().startswith("#")]
    return "\n".join(lines).strip()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for marker in ("。", "！", "？", "\n"):
        position = cut.rfind(marker)
        if position > limit // 2:
            return cut[: position + 1].strip()
    return cut.strip()


def fixture_answer(
    case_id: str,
    sources: Sequence[Source],
    fixtures: Dict[str, Any],
) -> GroundedAnswer:
    """Replay a scripted model output, mapping doc IDs to the current source tags.

    Used by the offline answer eval to exercise the citation checker without
    pretending that a fake model is a real generator.
    """
    fixture = fixtures.get(case_id)
    if not fixture:
        return GroundedAnswer(answer="证据不足：没有 fixture。", citations=[])

    doc_to_source: Dict[str, str] = {}
    citations: List[Citation] = []
    for item in fixture.get("citations", []):
        source = _source_for_quote(
            item.get("source_doc", ""), item.get("quote", ""), sources
        )
        source_id = source.source_id if source else "S?"
        doc_to_source.setdefault(item.get("source_doc", ""), source_id)
        citations.append(Citation(source_id=source_id, quote=item.get("quote", "")))

    # Fall back to the first source for docs mentioned in the answer but not
    # in the citations list.
    for source in sources:
        doc_to_source.setdefault(source.doc_id, source.source_id)

    def replace(match: re.Match) -> str:
        return doc_to_source.get(match.group(1)) or "S?"

    answer = re.sub(r"\{S:([^}]+)\}", replace, fixture.get("answer", ""))
    return GroundedAnswer(answer=answer, citations=citations)


def _source_for_quote(
    doc_id: str, quote: str, sources: Sequence[Source]
) -> Optional[Source]:
    candidates = [source for source in sources if source.doc_id == doc_id]
    if not candidates:
        return None
    needle = normalize_text(quote)
    for source in candidates:
        if needle and needle in normalize_text(source.text):
            return source
    return candidates[0]


def generate_answer(
    query: str,
    sources: Sequence[Source],
    backend: str = "extractive",
    model: Optional[str] = None,
    min_overlap: int = 2,
) -> Tuple[GroundedAnswer, Dict[str, int]]:
    if backend == "extractive":
        return extractive_answer(query, sources, min_overlap=min_overlap), {}
    if backend == "llm":
        prompt = build_prompt(query, sources)
        raw, usage = chat_complete(
            [
                {
                    "role": "system",
                    "content": "你是一个只依据给定来源回答、并严格输出 JSON 的排障助手。",
                },
                {"role": "user", "content": prompt},
            ],
            model=model,
        )
        return parse_grounded_answer(raw), usage
    raise ValueError("unknown backend: %s" % backend)
