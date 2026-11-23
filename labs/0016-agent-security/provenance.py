"""Provenance, taint tracking and secret redaction.

This is a deliberately small information-flow-control (IFC) model.  It does not
parse a programming language; it tracks the origins attached to tool arguments.
That is enough to demonstrate the security property the lesson cares about:

    untrusted data must not flow into an egress, exec or autonomous write sink.

Real systems need more: field-level labels, declassification rules, and a
control-flow mechanism such as CaMeL or a Dual LLM.  The simplification is the
point: you can test this model exhaustively.
"""

import re
from typing import Dict, Iterable, Tuple

from models import SECRET, TRUSTED, UNTRUSTED, Arg, Origin

# Patterns are intentionally narrow.  A security control that tries to catch
# every possible secret with one regex becomes both noisy and easy to bypass.
# Production systems should combine pattern matching with a secret broker and
# binary-level data classification.
SECRET_PATTERNS = (
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("aws_secret", re.compile(r"AWS_SECRET_ACCESS_KEY\s*=\s*[A-Za-z0-9/+=]{20,}")),
    ("github_token", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("password", re.compile(r"(?i)password\s*=\s*[^\s&]{4,}")),
)


def secret_kinds(text: str) -> Tuple[str, ...]:
    return tuple(name for name, pattern in SECRET_PATTERNS if pattern.search(text))


def contains_secret(text: str) -> bool:
    return bool(secret_kinds(text))


def redact(text: str) -> Tuple[str, int]:
    """Replace secret-looking spans and return ``(clean_text, redaction_count)``."""

    count = 0
    clean = text
    for name, pattern in SECRET_PATTERNS:
        clean, n = pattern.subn("[REDACTED:%s]" % name, clean)
        count += n
    return clean, count


class TaintTracker:
    """Resolve argument origins to a trust label.

    Rules:
    - no origins => trusted literal from the plan;
    - unknown origin => untrusted (fail closed);
    - any secret origin => secret;
    - any untrusted origin => untrusted;
    - otherwise trusted.

    A value can have several origins.  Mixing a trusted literal with an
    untrusted snippet does not launder the untrusted part.
    """

    def __init__(self, origins: Iterable[Origin]):
        self.origins: Dict[str, Origin] = {origin.id: origin for origin in origins}

    def trust_of(self, arg: Arg) -> str:
        if not arg.origins:
            return TRUSTED
        trusts = []
        for origin_id in arg.origins:
            origin = self.origins.get(origin_id)
            if origin is None:
                return UNTRUSTED
            trusts.append(origin.trust)
        if SECRET in trusts:
            return SECRET
        if UNTRUSTED in trusts:
            return UNTRUSTED
        return TRUSTED

    def is_tainted(self, arg: Arg) -> bool:
        return self.trust_of(arg) in (UNTRUSTED, SECRET)

    def describe(self, arg: Arg) -> str:
        trust = self.trust_of(arg)
        if not arg.origins:
            return "%s(literal)" % trust
        return "%s(%s)" % (trust, ", ".join(arg.origins))
