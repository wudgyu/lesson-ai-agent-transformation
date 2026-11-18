"""Small shared helpers: JSONL, text normalisation, .env loading."""
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple


def load_jsonl(path: Path) -> Iterator[Tuple[int, Dict[str, Any]]]:
    """Yield (line_number, object); raise ValueError with line context on bad JSON."""
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("%s:%d: invalid JSON: %s" % (path, line_no, exc))
            if not isinstance(value, dict):
                raise ValueError("%s:%d: each line must be a JSON object" % (path, line_no))
            yield line_no, value


def normalize_text(text: str) -> str:
    """Collapse whitespace and lowercase. Quotes are compared after this."""
    return " ".join(text.lower().split())


def quote_is_grounded(quote: str, source_text: str) -> bool:
    """A quote is grounded when it appears (after whitespace normalisation) in the source."""
    if not quote.strip():
        return False
    return normalize_text(quote) in normalize_text(source_text)


def load_env(path: Optional[Path] = None) -> Dict[str, str]:
    """Read simple KEY=VALUE lines. Existing environment variables win."""
    env = dict(os.environ)
    if path is None:
        path = Path(__file__).resolve().parent / ".env"
    if Path(path).exists():
        with Path(path).open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                env.setdefault(key.strip(), value.strip().strip("'").strip('"'))
    return env


def pct(value: float, digits: int = 1) -> str:
    return ("%." + str(digits) + "f%%") % (value * 100.0)


def mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0
