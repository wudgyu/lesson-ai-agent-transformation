"""FastAPI adapter for the run service.

The HTTP layer is intentionally thin:

- validate the request and the two headers that matter (Idempotency-Key,
  Last-Event-ID);
- translate service admission decisions into 202/200/409/422/429/503;
- stream the event log as SSE.

All execution decisions live in ``run_service.py``.  This is also why a client
disconnect does not cancel the run: the worker task is owned by the service,
not by the response generator.
"""

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from errors import RunNotFound
from models import RunRequest
from run_service import RunService, ServiceConfig
from sse import format_event, format_ping, format_retry


class RunRequestIn(BaseModel):
    service: str = Field(min_length=1, max_length=120)
    incident: str = Field(min_length=1, max_length=2000)
    priority: str = Field(default="normal", pattern="^(normal|critical)$")


def create_app(config=None, clock=None, store=None):
    cfg = config or ServiceConfig()

    @asynccontextmanager
    async def lifespan(app):
        service = RunService(store=store, config=cfg, clock=clock)
        await service.start()
        app.state.service = service
        try:
            yield
        finally:
            await service.drain(cfg.drain_timeout_s)

    app = FastAPI(
        title="RepoMedic Serving Lab",
        version="1.0",
        description="A run-oriented API with SSE, idempotency, retry budgets and graceful drain.",
        lifespan=lifespan,
    )

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/readyz")
    async def readyz(request: Request):
        service = request.app.state.service
        body = {"ready": service.ready(), "metrics": service.snapshot_metrics()}
        return body if service.ready() else JSONResponse(status_code=503, content=body)

    @app.post("/v1/runs", status_code=202)
    async def create_run(
        body: RunRequestIn,
        request: Request,
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
        tenant: str = Header(default="demo", alias="X-Tenant"),
    ):
        # In production ``tenant`` must come from the authenticated principal.
        # A client-supplied header is shown here only because the lab has no
        # auth layer; trusting it would be a tenant-isolation bug.
        service = request.app.state.service
        admission = await service.submit(
            RunRequest(
                tenant=tenant or "demo",
                service=body.service,
                incident=body.incident,
                idempotency_key=idempotency_key or "",
                priority=body.priority,
            )
        )
        if admission.status == 202:
            location = "/v1/runs/%s" % admission.run_id
            return JSONResponse(
                status_code=202,
                content={
                    "run_id": admission.run_id,
                    "status": "accepted",
                    "location": location,
                    "events": location + "/events",
                },
                headers={"Location": location},
            )
        if admission.status == 200:
            location = "/v1/runs/%s" % admission.run_id
            return JSONResponse(
                status_code=200,
                content={
                    "run_id": admission.run_id,
                    "status": "duplicate",
                    "location": location,
                    "events": location + "/events",
                },
                headers={"Location": location},
            )

        headers = {}
        if admission.status == 429 and admission.retry_after is not None:
            headers["Retry-After"] = str(admission.retry_after)
        if admission.status == 409 and admission.run_id:
            headers["Location"] = "/v1/runs/%s" % admission.run_id
        raise HTTPException(
            status_code=admission.status,
            detail={
                "code": admission.code,
                "message": admission.message,
                "run_id": admission.run_id,
            },
            headers=headers or None,
        )

    @app.get("/v1/runs/{run_id}")
    async def get_run(run_id: str, request: Request):
        service = request.app.state.service
        try:
            return service.get(run_id).public()
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail=exc.detail or {"code": exc.code})

    @app.get("/v1/runs/{run_id}/events")
    async def stream_events(
        run_id: str,
        request: Request,
        last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
    ):
        service = request.app.state.service
        try:
            service.get(run_id)
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail=exc.detail or {"code": exc.code})

        parsed = None
        if last_event_id is not None:
            try:
                parsed = int(last_event_id)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "bad_last_event_id", "message": "must be an integer"},
                )
            if parsed < 0:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "bad_last_event_id", "message": "must be >= 0"},
                )

        async def generate():
            yield format_retry(2000)
            async for event in service.subscribe(
                run_id, last_event_id=parsed, heartbeat_s=service.config.heartbeat_s
            ):
                if event.type == "ping":
                    yield format_ping()
                else:
                    yield format_event(event)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/v1/runs/{run_id}/cancel", status_code=202)
    async def cancel_run(run_id: str, request: Request):
        service = request.app.state.service
        try:
            run = await service.cancel_and_wait(run_id)
        except RunNotFound as exc:
            raise HTTPException(status_code=404, detail=exc.detail or {"code": exc.code})
        return {
            "run_id": run.run_id,
            "status": run.status,
            "cancel_requested": run.cancel_requested,
        }

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api:app", host="127.0.0.1", port=8000, reload=False)
