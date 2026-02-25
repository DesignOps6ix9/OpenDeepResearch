from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .db import EventStore
from .engine import EventBus, RunManager

BASE_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = BASE_DIR / "static"
DB_PATH = BASE_DIR / "data" / "events.db"

settings = Settings.from_env()
store = EventStore(DB_PATH)
bus = EventBus()
manager = RunManager(store=store, bus=bus, settings=settings)

app = FastAPI(title="DeepDeepResearch M2/M3", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
async def recover_stale_runs() -> None:
    await manager.recover_stale_runs()


class CreateRunRequest(BaseModel):
    goal: str = Field(min_length=3)
    max_steps: int = Field(default=4, ge=1, le=50)
    config: dict[str, Any] = Field(default_factory=dict)


class CreateRunResponse(BaseModel):
    run_id: str
    status: str
    ws_path: str


class SteerRequest(BaseModel):
    content: str = Field(min_length=1)
    scope: str = Field(default="global")


@app.get("/")
async def user_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "user.html")


@app.get("/api/system/capabilities")
async def system_capabilities() -> dict[str, Any]:
    return {
        "llm_enabled": bool(settings.llm_api_key),
        "search_providers": {
            "tavily": bool(settings.tavily_api_key),
            "serper": bool(settings.serper_api_key),
            "crossref": bool(settings.crossref_base_url),
        },
        "reader_providers": {
            "firecrawl": bool(settings.firecrawl_api_key),
            "jina_reader": bool(settings.jina_reader_api_key),
        },
        "rank_providers": {
            "cohere": bool(settings.cohere_api_key),
        },
        "trusted_domains": settings.trusted_domains,
        "default_blocked_domains": settings.default_blocked_domains,
    }


@app.post("/api/runs", response_model=CreateRunResponse)
async def create_run(body: CreateRunRequest) -> CreateRunResponse:
    run = await manager.create_run(goal=body.goal, max_steps=body.max_steps, config=body.config)
    return CreateRunResponse(run_id=run["run_id"], status=run["status"], ws_path=f"/ws/runs/{run['run_id']}")


@app.get("/api/runs")
async def list_runs(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    safe_limit = max(1, min(limit, 200))
    safe_offset = max(0, offset)
    runs = await manager.list_runs(limit=safe_limit, offset=safe_offset)
    return {"runs": runs, "limit": safe_limit, "offset": safe_offset}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    run = await manager.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@app.get("/api/runs/{run_id}/events")
async def get_events(run_id: str, after_id: int = 0, limit: int = 300) -> dict[str, Any]:
    run = await manager.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    events = await manager.get_events(run_id=run_id, after_id=after_id, limit=limit)
    return {"events": events}


@app.post("/api/runs/{run_id}/steer")
async def submit_steer(run_id: str, body: SteerRequest) -> dict[str, Any]:
    try:
        steer = await manager.submit_steer(run_id=run_id, content=body.content, scope=body.scope)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"steer": steer}


@app.websocket("/ws/runs/{run_id}")
async def run_events_websocket(websocket: WebSocket, run_id: str) -> None:
    run = await manager.get_run(run_id)
    if run is None:
        await websocket.close(code=4404)
        return

    await websocket.accept()

    # Replay recent history for late-joining clients.
    history = await manager.get_events(run_id=run_id, after_id=0, limit=500)
    for event in history:
        await websocket.send_json({"type": "event", "event": event})

    subscriber = await bus.subscribe(run_id)

    async def sender() -> None:
        while True:
            event = await subscriber.get()
            await websocket.send_json({"type": "event", "event": event})

    async def receiver() -> None:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if message.get("type") != "steer":
                continue

            content = str(message.get("content", "")).strip()
            scope = str(message.get("scope", "global")).strip() or "global"
            if not content:
                continue

            try:
                steer = await manager.submit_steer(run_id=run_id, content=content, scope=scope)
                await websocket.send_json({"type": "ack", "steer": steer})
            except ValueError:
                await websocket.send_json({"type": "error", "message": "run not found"})

    send_task = asyncio.create_task(sender())
    receive_task = asyncio.create_task(receiver())

    try:
        done, pending = await asyncio.wait(
            {send_task, receive_task},
            return_when=asyncio.FIRST_EXCEPTION,
        )

        for task in done:
            task.result()

        for task in pending:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    except WebSocketDisconnect:
        pass
    finally:
        send_task.cancel()
        receive_task.cancel()
        await asyncio.gather(send_task, receive_task, return_exceptions=True)
        await bus.unsubscribe(run_id, subscriber)
