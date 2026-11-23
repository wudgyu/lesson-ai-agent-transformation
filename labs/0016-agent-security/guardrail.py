"""A deliberately limited regex guardrail.

It exists to demonstrate two things:

1. Detection is useful for obvious, known strings.
2. Detection is not an authorization boundary: paraphrase, encoding,
   zero-width characters and ordinary path/URL/command abuse all get through.

The hardened pipeline does not rely on this class.  It enforces capabilities,
provenance and egress rules at the tool boundary.
"""

import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class GuardrailHit:
    pattern: str
    source: str
    excerpt: str


PATTERNS: Tuple[Tuple[str, "re.Pattern"], ...] = (
    ("ignore_instructions", re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.IGNORECASE)),
    ("disregard_instructions", re.compile(r"disregard\s+(all\s+)?(previous|prior)\s+instructions", re.IGNORECASE)),
    ("system_prompt", re.compile(r"system\s+prompt", re.IGNORECASE)),
    ("you_are_now", re.compile(r"you\s+are\s+now", re.IGNORECASE)),
    ("rm_rf", re.compile(r"rm\s+-rf", re.IGNORECASE)),
    ("curl_url", re.compile(r"curl\s+https?://", re.IGNORECASE)),
    ("wget_url", re.compile(r"wget\s+https?://", re.IGNORECASE)),
    ("base64_decode", re.compile(r"base64\s+-d", re.IGNORECASE)),
    ("ssh_material", re.compile(r"(\.ssh|id_rsa|id_ed25519)", re.IGNORECASE)),
    ("secret_env", re.compile(r"secrets?/[^\s]*\.env", re.IGNORECASE)),
    ("private_key", re.compile(r"BEGIN\s+(RSA\s+|OPENSSH\s+)?PRIVATE\s+KEY", re.IGNORECASE)),
    ("exfiltrate", re.compile(r"exfiltrat", re.IGNORECASE)),
    ("send_secret", re.compile(r"send\s+(the\s+)?(secret|token|password|env)", re.IGNORECASE)),
    ("system_line", re.compile(r"^\s*\[?SYSTEM\]?\b", re.MULTILINE)),
    ("delete_repo", re.compile(r"delete\s+(the\s+)?repo", re.IGNORECASE)),
)


class RegexGuardrail:
    def scan(self, texts: Iterable[Tuple[str, str]]) -> Optional[GuardrailHit]:
        """Scan ``(source_id, content)`` pairs and return the first hit."""

        for source, content in texts:
            if not content:
                continue
            for name, pattern in PATTERNS:
                match = pattern.search(content)
                if match:
                    start = max(0, match.start() - 24)
                    end = min(len(content), match.end() + 24)
                    excerpt = content[start:end].replace("\n", " ")
                    return GuardrailHit(name, source, excerpt)
        return None
