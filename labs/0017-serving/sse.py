"""Server-Sent Events wire format.

The lab formats SSE by hand for three reasons:

1. the semantics (event ids, replay, snapshot on gap) are the lesson, not the
   framework helper;
2. it works on FastAPI versions before ``fastapi.sse`` (0.135);
3. the same formatter is used by the tests and the CLI.

FastAPI 0.135+ users can replace this with
``EventSourceResponse``/``ServerSentEvent``; FastAPI then sets the required
headers and emits a keep-alive comment every 15 seconds.
"""

import json

from models import Event


def format_event(event):
    """Encode one ``Event`` as an SSE frame.

    A frame is a set of ``field: value`` lines followed by a blank line.
    ``data`` must be split across multiple ``data:`` lines if it contains a
    newline; our JSON is single-line, but the split keeps the formatter correct
    for raw strings too.
    """
    lines = ["id: %s" % event.seq, "event: %s" % event.type]
    payload = json.dumps(event.data, ensure_ascii=False, separators=(",", ":"))
    for line in payload.split("\n"):
        lines.append("data: %s" % line)
    return "\n".join(lines) + "\n\n"


def format_comment(text):
    return ": %s\n\n" % text


def format_retry(milliseconds):
    return "retry: %d\n\n" % int(milliseconds)


def format_ping():
    return format_comment("ping")


def parse_frames(text):
    """Parse an SSE text blob into ``[{event, id, data}, ...]``.

    Only what the lab emits is supported: one data line, JSON payload.  This is
    deliberately a test/CLI helper, not a browser-grade parser.
    """
    frames = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        frame = {"event": "message", "id": None, "data": None}
        data_lines = []
        for line in block.split("\n"):
            if line.startswith(":") or not line:
                continue
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                frame["event"] = value
            elif field == "id":
                frame["id"] = int(value) if value.isdigit() else value
            elif field == "data":
                data_lines.append(value)
            elif field == "retry":
                frame["retry"] = int(value)
        if data_lines:
            raw = "\n".join(data_lines)
            try:
                frame["data"] = json.loads(raw)
            except ValueError:
                frame["data"] = raw
        frames.append(frame)
    return frames
