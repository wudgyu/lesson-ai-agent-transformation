"""Citation protocol and verification for lesson 0006.

The key distinction:

- citation validity: the cited source ID exists;
- citation support: the quoted passage is actually in that source;
- citation coverage: every factual claim carries a citation;
- answer correctness: the answer contains the expected facts.

`check_citations` verifies the first three deterministically. Correctness is
checked against the case's expected facts. Full entailment (does the quote
really imply the claim?) needs an NLI model or an LLM judge; this lab keeps
that step explicit and optional.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

CITATION_RE = re.compile(r"\[S(\d+)\]")
SENTENCE_SPLIT_RE = re.compile(r"[。！？；]+")
META_PREFIXES = (
    "证据不足",
    "无法回答",
    "无法确定",
    "insufficient",
    "根据来源",
    "置信度",
    "说明",
    "注意",
)


@dataclass
class Source:
    source_id: str
    doc_id: str
    heading: str
    text: str
    start_line: int = 0
    end_line: int = 0


@dataclass
class Citation:
    source_id: str
    quote: str


@dataclass
class GroundedAnswer:
    answer: str
    citations: List[Citation] = field(default_factory=list)
    raw: str = ""


def normalize_source_id(value: str) -> str:
    text = (value or "").strip().upper()
    if re.fullmatch(r"\d+", text):
        return "S%s" % text
    return text


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def parse_grounded_answer(raw: str) -> GroundedAnswer:
    """Parse the model's JSON output, tolerating code fences or extra prose."""
    text = (raw or "").strip()
    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        pass

    if data is None:
        fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
        if fenced:
            try:
                data = json.loads(fenced.group(1).strip())
            except json.JSONDecodeError:
                data = None

    if data is None:
        candidate = _extract_json_object(text)
        if candidate:
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                data = None

    if not isinstance(data, dict):
        return GroundedAnswer(answer=text, citations=[], raw=raw)

    answer = str(data.get("answer", "") or "").strip()
    citations: List[Citation] = []
    for item in data.get("citations", []) or []:
        if not isinstance(item, dict):
            continue
        source_id = normalize_source_id(str(item.get("source_id", "")))
        quote = str(item.get("quote", "") or "").strip()
        if source_id or quote:
            citations.append(Citation(source_id=source_id, quote=quote))
    return GroundedAnswer(answer=answer or text, citations=citations, raw=raw)


def _extract_json_object(text: str) -> Optional[str]:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def check_citations(
    grounded: GroundedAnswer,
    sources: Sequence[Source],
    min_coverage: float = 0.8,
) -> Dict[str, Any]:
    source_map = {source.source_id: source for source in sources}
    answer_ids = {
        "S%s" % number for number in CITATION_RE.findall(grounded.answer or "")
    }
    citation_ids = {citation.source_id for citation in grounded.citations}
    invalid_ids = sorted((answer_ids | citation_ids) - set(source_map))

    missing_quote: List[Citation] = []
    quote_not_found: List[Citation] = []
    valid_citations = 0
    for citation in grounded.citations:
        source = source_map.get(citation.source_id)
        if source is None:
            continue
        if not citation.quote:
            missing_quote.append(citation)
            continue
        if normalize_text(citation.quote) not in normalize_text(source.text):
            quote_not_found.append(citation)
            continue
        valid_citations += 1

    total_citations = len(grounded.citations)
    citation_precision = (
        valid_citations / total_citations if total_citations else 0.0
    )

    claims = _claims(grounded.answer)
    quoted_text = [
        normalize_text(citation.quote)
        for citation in grounded.citations
        if citation.quote
    ]
    uncited_claims: List[str] = []
    for claim in claims:
        if CITATION_RE.search(claim):
            continue
        plain = normalize_text(CITATION_RE.sub("", claim))
        # A citation whose quote spans the claim also covers it.
        if any(plain and plain in quote for quote in quoted_text):
            continue
        uncited_claims.append(claim)
    citation_coverage = (
        (len(claims) - len(uncited_claims)) / len(claims) if claims else 1.0
    )

    valid = (
        not invalid_ids
        and not missing_quote
        and not quote_not_found
        and citation_coverage >= min_coverage
    )
    return {
        "valid": valid,
        "invalid_ids": invalid_ids,
        "missing_quote": [citation.__dict__ for citation in missing_quote],
        "quote_not_found": [citation.__dict__ for citation in quote_not_found],
        "uncited_claims": uncited_claims,
        "citation_precision": citation_precision,
        "citation_coverage": citation_coverage,
        "answer_ids": sorted(answer_ids),
        "citation_ids": sorted(citation_ids),
    }


def _claims(answer: str) -> List[str]:
    claims: List[str] = []
    for raw in SENTENCE_SPLIT_RE.split(answer or ""):
        sentence = raw.strip()
        if len(sentence) < 4:
            continue
        plain = CITATION_RE.sub("", sentence).strip()
        if not plain:
            continue
        lowered = plain.lower()
        if any(lowered.startswith(prefix.lower()) for prefix in META_PREFIXES):
            continue
        claims.append(sentence)
    return claims


def citation_recall(
    expected_docs: Sequence[str],
    grounded: GroundedAnswer,
    sources: Sequence[Source],
) -> Tuple[float, List[str]]:
    source_by_id = {source.source_id: source for source in sources}
    valid_ids = set()
    for citation in grounded.citations:
        source = source_by_id.get(citation.source_id)
        if source is None or not citation.quote:
            continue
        if normalize_text(citation.quote) in normalize_text(source.text):
            valid_ids.add(citation.source_id)
    cited_docs = {
        source_by_id[source_id].doc_id
        for source_id in valid_ids
        if source_id in source_by_id
    }
    expected = list(dict.fromkeys(expected_docs))
    if not expected:
        return 1.0, []
    missing = [doc for doc in expected if doc not in cited_docs]
    return (len(expected) - len(missing)) / len(expected), missing


def answer_contains_facts(answer: str, facts: Sequence[str]) -> Tuple[bool, List[str]]:
    lowered = (answer or "").lower()
    missing = [fact for fact in facts if fact.lower() not in lowered]
    return not missing, missing


def forbidden_hits(answer: str, forbidden: Sequence[str]) -> List[str]:
    lowered = (answer or "").lower()
    return [item for item in forbidden if item.lower() in lowered]


def is_abstention(answer: str) -> bool:
    lowered = (answer or "").lower()
    return any(
        marker in lowered
        for marker in (
            "证据不足",
            "无法回答",
            "无法确定",
            "没有足够",
            "insufficient evidence",
        )
    )
