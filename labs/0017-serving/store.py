"""In-memory state that must survive a process restart in production.

The lab uses one ``Store`` object for the "before" and "after" service in the
restart tests.  In a real deployment this would be Postgres/Redis:

- ``runs``: status, result, event log, checkpoint;
- ``idempotency``: request key -> fingerprint + run id;
- ``effects``: side effects already applied, keyed by run + phase.

Keeping them in one store makes the restart boundary explicit: if it lives in
process memory, it does not survive SIGTERM.
"""

import hashlib
import json
from typing import Dict, Optional, Set

from models import IdempotencyRecord, Run, RunRequest


def fingerprint(request: RunRequest) -> str:
    """Stable hash of everything that makes two requests 'the same'."""
    raw = json.dumps(request.payload(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class EffectLedger:
    """The smallest possible idempotency ledger for side effects."""

    def __init__(self):
        self._done = set()

    def contains(self, run_id, phase):
        return (run_id, phase) in self._done

    def mark(self, run_id, phase):
        self._done.add((run_id, phase))

    def count(self):
        return len(self._done)


class Store:
    def __init__(self):
        self.runs: Dict[str, Run] = {}
        self.idempotency: Dict[tuple, IdempotencyRecord] = {}
        self.effects = EffectLedger()
        self._next_run = 1

    def next_run_id(self):
        run_id = "run-%04d" % self._next_run
        self._next_run += 1
        return run_id

    def add_run(self, run):
        self.runs[run.run_id] = run

    def get_run(self, run_id):
        return self.runs.get(run_id)

    def get_idempotency(self, tenant, key):
        return self.idempotency.get((tenant, key))

    def put_idempotency(self, tenant, key, record):
        self.idempotency[(tenant, key)] = record

    def expire_idempotency(self, now):
        expired = [k for k, rec in self.idempotency.items() if rec.expires_at <= now]
        for key in expired:
            del self.idempotency[key]
        return len(expired)

    def stats(self):
        return {
            "runs": len(self.runs),
            "idempotency_keys": len(self.idempotency),
            "effects": self.effects.count(),
        }
