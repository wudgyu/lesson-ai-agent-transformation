"""Optional HTTP tests for the FastAPI adapter.

These tests need ``fastapi`` and ``httpx`` (and a TestClient).  They are skipped
automatically when those packages are missing so the core lab stays
standard-library only.

    pip install fastapi uvicorn httpx
    python3 -m unittest test_api -v
"""

import unittest

try:
    from fastapi.testclient import TestClient  # noqa: F401

    from api import create_app
    from clock import FakeClock
    from run_service import ServiceConfig
    from sse import parse_frames

    HAS_FASTAPI = True
except ImportError:  # pragma: no cover - exercised only without deps
    HAS_FASTAPI = False


@unittest.skipUnless(HAS_FASTAPI, "FastAPI is not installed")
class ApiTests(unittest.TestCase):
    def make_client(self, clock=None, **overrides):
        options = {
            "workers": 1,
            "max_queue": 4,
            "per_tenant_capacity": 100,
            "per_tenant_refill": 0.0,
            "phase_latency_s": 0.0,
            "run_deadline_s": 5.0,
            "drain_timeout_s": 0.05,
            "heartbeat_s": None,
        }
        options.update(overrides)
        app = create_app(config=ServiceConfig(**options), clock=clock or FakeClock())
        client = TestClient(app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def post_run(self, client, key="k1", incident="R01", tenant="t1", **body):
        payload = {"service": "checkout-api", "incident": incident}
        payload.update(body)
        return client.post(
            "/v1/runs",
            json=payload,
            headers={"Idempotency-Key": key, "X-Tenant": tenant},
        )

    def test_health_and_readiness(self):
        client = self.make_client()
        self.assertEqual(client.get("/healthz").status_code, 200)
        ready = client.get("/readyz")
        self.assertEqual(ready.status_code, 200)
        self.assertTrue(ready.json()["ready"])

    def test_create_run_returns_202_location_and_result(self):
        client = self.make_client()
        response = self.post_run(client)
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body["status"], "accepted")
        self.assertIn("/events", body["events"])
        self.assertIn("Location", response.headers)

        detail = client.get("/v1/runs/%s" % body["run_id"])
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["status"], "succeeded")
        self.assertEqual(
            detail.json()["result"]["root_cause"],
            "missing null guard in OrderMapper.toDto",
        )

    def test_missing_idempotency_key_is_400(self):
        client = self.make_client()
        response = client.post(
            "/v1/runs",
            json={"service": "checkout-api", "incident": "R01"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["code"], "missing_idempotency_key")

    def test_duplicate_completed_is_200(self):
        client = self.make_client()
        first = self.post_run(client).json()
        second = self.post_run(client)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["run_id"], first["run_id"])

    def test_same_key_different_payload_is_422(self):
        client = self.make_client()
        self.post_run(client)
        response = self.post_run(client, incident="R02")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "idempotency_conflict")

    def test_inflight_duplicate_is_409_with_location(self):
        from clock import Clock

        # R03 hangs for 10 seconds; with a real clock the run stays in progress
        # long enough for the second request to hit the in-flight branch.
        client = self.make_client(
            clock=Clock(), run_deadline_s=30.0, phase_latency_s=0.001
        )
        first = self.post_run(client, incident="R03")
        self.assertEqual(first.status_code, 202)
        second = self.post_run(client, incident="R03")
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["detail"]["code"], "in_progress")
        self.assertEqual(
            second.headers.get("Location"),
            "/v1/runs/%s" % first.json()["run_id"],
        )

    def test_sse_stream_frames_and_headers(self):
        client = self.make_client()
        run_id = self.post_run(client).json()["run_id"]
        with client.stream("GET", "/v1/runs/%s/events" % run_id) as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/event-stream", response.headers["content-type"])
            self.assertEqual(response.headers["cache-control"], "no-cache")
            self.assertEqual(response.headers["x-accel-buffering"], "no")
            text = "".join(response.iter_text())
        frames = parse_frames(text)
        types = [f["event"] for f in frames]
        self.assertIn("queued", types)
        self.assertEqual(types[-1], "done")
        self.assertTrue(all(f["id"] is None or isinstance(f["id"], int) for f in frames))

    def test_sse_last_event_id_resume(self):
        client = self.make_client()
        run_id = self.post_run(client).json()["run_id"]
        with client.stream(
            "GET",
            "/v1/runs/%s/events" % run_id,
            headers={"Last-Event-ID": "5"},
        ) as response:
            frames = parse_frames("".join(response.iter_text()))
        ids = [f["id"] for f in frames if f["id"] is not None]
        self.assertTrue(ids)
        self.assertTrue(all(i > 5 for i in ids), ids)
        self.assertEqual(frames[-1]["event"], "done")

    def test_sse_unknown_run_is_404(self):
        client = self.make_client()
        self.assertEqual(client.get("/v1/runs/run-9999").status_code, 404)
        self.assertEqual(client.get("/v1/runs/run-9999/events").status_code, 404)

    def test_sse_bad_last_event_id_is_400(self):
        client = self.make_client()
        run_id = self.post_run(client).json()["run_id"]
        response = client.get(
            "/v1/runs/%s/events" % run_id,
            headers={"Last-Event-ID": "abc"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["code"], "bad_last_event_id")

    def test_rate_limit_returns_retry_after(self):
        client = self.make_client(per_tenant_capacity=1, per_tenant_refill=0.0)
        self.assertEqual(self.post_run(client, key="a").status_code, 202)
        response = self.post_run(client, key="b")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers.get("Retry-After"), "60")
        self.assertEqual(response.json()["detail"]["code"], "tenant_rate_limited")

    def test_drain_rejects_new_runs_and_readiness_is_503(self):
        client = self.make_client()
        client.app.state.service.begin_shutdown()
        self.assertEqual(client.get("/readyz").status_code, 503)
        response = self.post_run(client, key="after-drain")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "draining")

    def test_cancel_endpoint(self):
        from clock import Clock

        client = self.make_client(
            clock=Clock(), run_deadline_s=30.0, phase_latency_s=0.001
        )
        run_id = self.post_run(client, incident="R03").json()["run_id"]
        response = client.post("/v1/runs/%s/cancel" % run_id)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "cancelled")
        detail = client.get("/v1/runs/%s" % run_id).json()
        self.assertEqual(detail["status"], "cancelled")
        self.assertEqual(detail["error"]["code"], "cancelled")


if __name__ == "__main__":
    unittest.main()
