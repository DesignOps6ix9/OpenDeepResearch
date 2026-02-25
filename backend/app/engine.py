from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .db import EventStore, utc_now_iso
from .llm import LLMClient, parse_json_object
from .providers import LOW_QUALITY_DOMAINS, ResearchToolkit, SearchResult, SourceDocument, extract_domain, is_academic_query

STEP_GAP_SECONDS = 0.35
TOKEN_DELAY_SECONDS = 0.02
STAGE_TIMEOUT_SECONDS = {
    "query_expansion": 45.0,
    "search_web": 70.0,
    "read_sources": 95.0,
    "structured_notes": 90.0,
    "outline": 45.0,
    "write_synthesis": 110.0,
    "self_check": 55.0,
    "step_summary": 55.0,
}
PLAN_NOISE_PATTERNS = (
    re.compile(r"^[-*\s]*(user input timeline|user-input timeline|timeline|用户输入时间线|输入时间线)\b", re.IGNORECASE),
    re.compile(r"^[-*\s]*(steering update|干预更新|纠偏更新|supervisor decision|监督结论)\b", re.IGNORECASE),
    re.compile(r"^[-*\s]*(clarification confirmed|澄清确认)\b", re.IGNORECASE),
    re.compile(r"^[-*\s]*\[(initial query|clarification|steer)\]", re.IGNORECASE),
    re.compile(r"^[-*\s]*(initial query|clarification|steer)\b", re.IGNORECASE),
)


class EventBus:
    """In-memory pub/sub for streaming run events to websocket clients."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, run_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        async with self._lock:
            self._subscribers.setdefault(run_id, set()).add(queue)
        return queue

    async def unsubscribe(self, run_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            run_subscribers = self._subscribers.get(run_id)
            if not run_subscribers:
                return
            run_subscribers.discard(queue)
            if not run_subscribers:
                self._subscribers.pop(run_id, None)

    async def publish(self, run_id: str, event: dict[str, Any]) -> None:
        async with self._lock:
            queues = list(self._subscribers.get(run_id, set()))
        for queue in queues:
            queue.put_nowait(event)


@dataclass
class RunContext:
    run_id: str
    status: str
    state: dict[str, Any]
    task: asyncio.Task[Any] | None = None
    interrupt_event: asyncio.Event = field(default_factory=asyncio.Event)


class RunManager:
    def __init__(self, store: EventStore, bus: EventBus, settings: Settings) -> None:
        self.store = store
        self.bus = bus
        self.settings = settings
        self.llm = LLMClient(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            temperature=settings.research_model_temperature,
        )
        self.research_toolkit = ResearchToolkit(
            tavily_api_key=settings.tavily_api_key,
            serper_api_key=settings.serper_api_key,
            firecrawl_api_key=settings.firecrawl_api_key,
            cohere_api_key=settings.cohere_api_key,
            cohere_rerank_model=settings.cohere_rerank_model,
            crossref_base_url=settings.crossref_base_url,
            crossref_max_results_per_query=settings.crossref_max_results_per_query,
            jina_reader_api_key=settings.jina_reader_api_key,
            trusted_domains=settings.trusted_domains,
        )
        self._active_runs: dict[str, RunContext] = {}
        self._lock = asyncio.Lock()

    def _detect_language(self, text: str) -> str:
        if re.search(r"[\u4e00-\u9fff]", text or ""):
            return "zh"
        return "en"

    def _is_zh(self, state: dict[str, Any]) -> bool:
        language = str(state.get("language", "")).lower()
        if language.startswith("zh"):
            return True
        return self._detect_language(str(state.get("goal", ""))) == "zh"

    def _lang_text(self, state: dict[str, Any], zh_value: Any, en_value: Any) -> Any:
        return zh_value if self._is_zh(state) else en_value

    def _strip_markdown_markers(self, text: str) -> str:
        value = str(text or "").replace("\r\n", "\n")
        # Remove emphasis markers like **text** and keep plain readable text.
        value = re.sub(r"\*{2,}", "", value)
        # Remove markdown heading tokens.
        value = re.sub(r"^\s*#{1,6}\s*", "", value, flags=re.MULTILINE)
        # Normalize empty-line noise after cleanup.
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()

    def _extract_clarification_summary(self, state: dict[str, Any]) -> str:
        clarification = state.get("clarification", {})
        if not isinstance(clarification, dict):
            return ""
        pieces: list[str] = []
        response = str(clarification.get("response", "")).strip()
        if response:
            pieces.append(f"用户澄清确认：{response}")

        applied = clarification.get("applied_updates", {})
        if isinstance(applied, dict):
            refined_goal = str(applied.get("refined_goal", "")).strip()
            if refined_goal:
                pieces.append(f"范围收敛：{refined_goal}")
            selected_focus = str(applied.get("selected_focus", "")).strip()
            if selected_focus:
                pieces.append(f"聚焦方向：{selected_focus}")
            for key in ("new_constraints", "output_requirements"):
                values = applied.get(key, [])
                if isinstance(values, list):
                    cleaned = [str(item).strip() for item in values if str(item).strip()]
                    if cleaned:
                        pieces.append("；".join(cleaned[:3]))

        return "；".join(piece for piece in pieces if piece).strip()

    def _build_report_source_map(self, completed_steps: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        source_map: dict[str, dict[str, Any]] = {}
        url_to_id: dict[str, str] = {}
        next_index = 1

        def register(
            *,
            url: str,
            title: str = "",
            domain: str = "",
            snippet: str = "",
            source_type: str = "web",
            step_id: str = "",
            step_title: str = "",
        ) -> str:
            nonlocal next_index
            clean_url = str(url).strip()
            if not clean_url:
                return ""
            source_id = url_to_id.get(clean_url)
            if not source_id:
                source_id = f"S{next_index}"
                next_index += 1
                url_to_id[clean_url] = source_id
                source_map[source_id] = {
                    "source_id": source_id,
                    "url": clean_url,
                    "title": str(title).strip() or clean_url,
                    "domain": str(domain).strip() or extract_domain(clean_url) or "",
                    "snippet": str(snippet).strip(),
                    "source_type": str(source_type).strip() or "web",
                    "step_id": str(step_id).strip(),
                    "step_title": str(step_title).strip(),
                }
                return source_id

            current = source_map.get(source_id, {})
            if current and not current.get("title") and str(title).strip():
                current["title"] = str(title).strip()
            if current and not current.get("snippet") and str(snippet).strip():
                current["snippet"] = str(snippet).strip()
            if current and not current.get("domain") and str(domain).strip():
                current["domain"] = str(domain).strip()
            if current and not current.get("step_title") and str(step_title).strip():
                current["step_title"] = str(step_title).strip()
            return source_id

        for step_item in completed_steps:
            if not isinstance(step_item, dict):
                continue
            step_id = str(step_item.get("step_id", "")).strip()
            step_title = str(step_item.get("title", "")).strip()

            for src in step_item.get("top_sources", []):
                if not isinstance(src, dict):
                    continue
                register(
                    url=str(src.get("url", "")).strip(),
                    title=str(src.get("title", "")).strip(),
                    domain=str(src.get("domain", "")).strip(),
                    snippet=str(src.get("snippet", "") or src.get("excerpt", "")).strip(),
                    source_type="top_source",
                    step_id=step_id,
                    step_title=step_title,
                )

            for note in step_item.get("notes", []):
                if not isinstance(note, dict):
                    continue
                note_text = str(note.get("quote_or_note", "") or note.get("evidence", "")).strip()
                note_source = str(note.get("source", "")).strip()
                note_source_type = str(note.get("source_type", "")).strip() or "note"
                citations = note.get("citations", [])
                if not isinstance(citations, list):
                    citations = []
                for citation in citations:
                    register(
                        url=str(citation).strip(),
                        title=note_source,
                        snippet=note_text,
                        source_type=note_source_type,
                        step_id=step_id,
                        step_title=step_title,
                    )

        return source_map

    def _build_source_catalog_text(self, source_map: dict[str, dict[str, Any]], limit: int = 60) -> str:
        lines: list[str] = []
        for source_id, source in list(source_map.items())[:limit]:
            title = str(source.get("title", "")).strip().replace("\n", " ")
            domain = str(source.get("domain", "")).strip()
            url = str(source.get("url", "")).strip()
            lines.append(f"{source_id} | {title} | {domain} | {url}")
        return "\n".join(lines)

    async def create_run(self, goal: str, max_steps: int, config: dict[str, Any] | None = None) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        language = self._detect_language(goal)

        run_state = {
            "goal": goal,
            "language": language,
            "clarification": {
                "status": "pending",
                "prompt": "",
                "response": "",
                "waiting_emitted": False,
                "history": [
                    {
                        "role": "user",
                        "content": goal,
                        "at": utc_now_iso(),
                    }
                ],
                "applied_updates": {},
            },
            "constraints": [],
            "required_sources": list(self.settings.default_allowed_domains),
            "blocked_sources": list(self.settings.default_blocked_domains),
            "steering_notes": [],
            "pending_steer_changes": [],
            "supervisor": {
                "accepted_directives": [],
                "rejected_directives": [],
                "last_reviewed_version": 0,
                "latest_guidance": "",
                "reflections": [],
                "decisions": [],
                "quality_gate_history": [],
            },
            "plan": [],
            "plan_revision": 0,
            "completed_steps": [],
            "research_memory": [],
            "outline": [],
            "final_report": None,
            "next_step_index": 0,
            "step_id_counter": 0,
            "max_steps": max_steps,
            "flags": {
                "replan": True,
                "stop_requested": False,
                "interrupt_requested": False,
                "auto_replan_attempts": 0,
            },
        }

        run_config = {
            "trusted_domains": self.settings.trusted_domains,
            "search_max_results_per_query": self.settings.search_max_results_per_query,
            "max_sources_per_step": self.settings.max_sources_per_step,
            "max_read_sources_per_step": self.settings.max_read_sources_per_step,
            "crossref_base_url": self.settings.crossref_base_url,
            "cohere_rerank_model": self.settings.cohere_rerank_model,
            **(config or {}),
        }

        self.store.create_run(run_id=run_id, goal=goal, config=run_config, state=run_state)

        context = RunContext(run_id=run_id, status="queued", state=run_state)
        async with self._lock:
            self._active_runs[run_id] = context

        context.task = asyncio.create_task(self._run_loop(context), name=f"run-loop-{run_id}")

        return self.store.get_run(run_id) or {
            "run_id": run_id,
            "status": "queued",
            "goal": goal,
            "config": run_config,
            "state": run_state,
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        }

    async def submit_steer(self, run_id: str, content: str, scope: str = "global") -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise ValueError(f"run not found: {run_id}")

        step_number = int(run["state"].get("next_step_index", 0))

        active_context: RunContext | None = None
        async with self._lock:
            active_context = self._active_runs.get(run_id)

        # Treat very-early user input as clarification confirmation to avoid
        # pending/awaiting race where the run appears "stuck" before research starts.
        if active_context is not None:
            clarification = active_context.state.get("clarification", {})
            clarification_status = str(clarification.get("status", "")).strip().lower() if isinstance(clarification, dict) else ""
            is_pre_research = (
                int(active_context.state.get("next_step_index", 0)) == 0
                and not active_context.state.get("completed_steps")
            )
            if isinstance(clarification, dict) and clarification_status in {"pending", "awaiting_user"} and is_pre_research:
                steer = self.store.insert_steer(run_id=run_id, scope="clarification", content=content)
                self.store.mark_steer_consumed([int(steer["id"])])

                updates = await self._apply_clarification_response(active_context.state, content)
                clarification["response"] = content
                clarification["status"] = "done"
                clarification["responded_at"] = utc_now_iso()
                clarification["waiting_emitted"] = False
                history = clarification.get("history")
                if isinstance(history, list):
                    history.append({"role": "user", "content": content, "at": utc_now_iso()})
                clarification["applied_updates"] = updates

                active_context.state["flags"]["replan"] = True
                active_context.state["flags"]["interrupt_requested"] = False
                active_context.interrupt_event.clear()
                self.store.update_run(run_id, status=active_context.status, state=active_context.state)

                await self._emit(
                    run_id=run_id,
                    step=step_number,
                    node="clarifier",
                    event_type="clarification_confirmed",
                    payload={
                        "content": content,
                        "updates": updates,
                    },
                )
                return {
                    **steer,
                    "used_as": "clarification_response",
                    "updates": updates,
                }

        steer = self.store.insert_steer(run_id=run_id, scope=scope, content=content)

        if active_context is not None:
            active_context.state["flags"]["interrupt_requested"] = True
            active_context.state["flags"]["replan"] = True
            active_context.interrupt_event.set()
            self.store.update_run(run_id, status=active_context.status, state=active_context.state)

        await self._emit(
            run_id=run_id,
            step=step_number,
            node="interruption_gateway",
            event_type="steer_received",
            payload={
                "id": steer["id"],
                "version": steer["version"],
                "scope": steer["scope"],
                "content": steer["content"],
                "interrupt_triggered": active_context is not None,
            },
        )

        if active_context is not None:
            await self._emit(
                run_id=run_id,
                step=step_number,
                node="interruption_gateway",
                event_type="interrupt_requested",
                payload={
                    "reason": "new_user_steer",
                    "steer_version": steer["version"],
                },
            )
        return steer

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self.store.get_run(run_id)

    async def list_runs(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        return self.store.list_runs(limit=limit, offset=offset)

    async def get_events(self, run_id: str, after_id: int = 0, limit: int = 300) -> list[dict[str, Any]]:
        return self.store.list_events(run_id=run_id, after_id=after_id, limit=limit)

    async def recover_stale_runs(self) -> dict[str, int]:
        runs = self.store.list_runs(limit=10000, offset=0)
        recovered = 0
        recovered_with_report = 0

        for run in runs:
            run_id = str(run.get("run_id", "")).strip()
            status = str(run.get("status", "")).strip().lower()
            if not run_id or status != "running":
                continue

            full_run = self.store.get_run(run_id)
            if full_run is None:
                continue

            state = full_run.get("state", {})
            if not isinstance(state, dict):
                state = {}
            completed_steps = state.get("completed_steps", [])
            if not isinstance(completed_steps, list):
                completed_steps = []

            final_report = state.get("final_report")
            if not isinstance(final_report, dict):
                final_report = await self._build_final_report(
                    state=state,
                    completed_steps=completed_steps,
                    status="stopped",
                )
                state["final_report"] = final_report
                recovered_with_report += 1

                await self._emit(
                    run_id=run_id,
                    step=int(state.get("next_step_index", len(completed_steps))),
                    node="recovery",
                    event_type="final_report",
                    payload=final_report,
                )

            self.store.update_run(run_id=run_id, status="stopped", state=state)
            recovered += 1

            await self._emit(
                run_id=run_id,
                step=int(state.get("next_step_index", len(completed_steps))),
                node="recovery",
                event_type="run_recovered",
                payload={
                    "previous_status": "running",
                    "new_status": "stopped",
                    "completed_steps": len(completed_steps),
                    "has_final_report": bool(state.get("final_report")),
                },
            )

        return {"recovered_runs": recovered, "recovered_with_report": recovered_with_report}

    async def _emit(self, run_id: str, step: int, node: str, event_type: str, payload: dict[str, Any]) -> None:
        event = self.store.insert_event(
            run_id=run_id,
            step=step,
            node=node,
            event_type=event_type,
            payload=payload,
        )
        await self.bus.publish(run_id, event)

    async def _run_loop(self, ctx: RunContext) -> None:
        try:
            ctx.status = "running"
            self.store.update_run(ctx.run_id, status=ctx.status, state=ctx.state)

            await self._emit(
                run_id=ctx.run_id,
                step=0,
                node="system",
                event_type="run_started",
                payload={
                    "goal": ctx.state["goal"],
                    "max_steps": ctx.state["max_steps"],
                    "models": {
                        "supervisor": self.settings.llm_model_supervisor,
                        "planner": self.settings.llm_model_planner,
                        "researcher": self.settings.llm_model_researcher,
                    },
                    "providers": {
                        "tavily": bool(self.settings.tavily_api_key),
                        "serper": bool(self.settings.serper_api_key),
                        "crossref": bool(self.settings.crossref_base_url),
                        "firecrawl": bool(self.settings.firecrawl_api_key),
                        "jina_reader": bool(self.settings.jina_reader_api_key),
                        "cohere_rerank": bool(self.settings.cohere_api_key),
                    },
                    "language": ctx.state.get("language", "en"),
                    "clarification_required": True,
                },
            )

            while ctx.status == "running":
                clarification_ready = await self._clarifier_gate(ctx)
                if not clarification_ready:
                    self.store.update_run(ctx.run_id, status=ctx.status, state=ctx.state)
                    await asyncio.sleep(STEP_GAP_SECONDS)
                    continue

                await self._collect_steer(ctx)
                await self._supervisor(ctx)
                await self._planner(ctx)
                await self._researcher(ctx)
                await self._decide_next(ctx)

                persisted_status = ctx.status
                if ctx.status in {"completed", "stopped"} and not isinstance(ctx.state.get("final_report"), dict):
                    # Keep user-facing status truthful while reporter is still generating final deliverable.
                    persisted_status = "finalizing"
                self.store.update_run(ctx.run_id, status=persisted_status, state=ctx.state)
                if ctx.status == "running" and not ctx.state["flags"].get("interrupt_requested"):
                    await asyncio.sleep(STEP_GAP_SECONDS)

            if ctx.status in {"completed", "stopped"}:
                await self._finalize_report(ctx)
                self.store.update_run(ctx.run_id, status=ctx.status, state=ctx.state)

            await self._emit(
                run_id=ctx.run_id,
                step=ctx.state["next_step_index"],
                node="system",
                event_type="run_finished",
                payload={
                    "status": ctx.status,
                    "completed_steps": len(ctx.state["completed_steps"]),
                    "has_final_report": bool(ctx.state.get("final_report")),
                },
            )

        except Exception as exc:  # noqa: BLE001
            ctx.status = "failed"
            try:
                await self._finalize_report(ctx)
            except Exception:
                pass
            self.store.update_run(ctx.run_id, status=ctx.status, state=ctx.state)
            await self._emit(
                run_id=ctx.run_id,
                step=ctx.state.get("next_step_index", 0),
                node="system",
                event_type="run_failed",
                payload={
                    "error": str(exc),
                    "has_final_report": bool(ctx.state.get("final_report")),
                },
            )
        finally:
            async with self._lock:
                self._active_runs.pop(ctx.run_id, None)

    async def _collect_steer(self, ctx: RunContext) -> None:
        step = int(ctx.state["next_step_index"])
        pending = self.store.list_unconsumed_steer(ctx.run_id)

        if not pending:
            await self._emit(
                run_id=ctx.run_id,
                step=step,
                node="collect_steer",
                event_type="steer_none",
                payload={"message": "no new steer"},
            )
            return

        pending_changes: list[dict[str, Any]] = []
        steer_ids: list[int] = []

        for steer in pending:
            steer_ids.append(int(steer["id"]))
            parsed = self._parse_steer(steer)
            pending_changes.append(parsed)
            ctx.state["steering_notes"].append(parsed)

        ctx.state["pending_steer_changes"].extend(pending_changes)
        self.store.mark_steer_consumed(steer_ids)

        await self._emit(
            run_id=ctx.run_id,
            step=step,
            node="collect_steer",
            event_type="steer_collected",
            payload={
                "count": len(pending_changes),
                "pending_versions": [item["version"] for item in pending_changes],
                "changes": pending_changes,
            },
        )

    async def _clarifier_gate(self, ctx: RunContext) -> bool:
        clarification = ctx.state.get("clarification", {})
        if not isinstance(clarification, dict):
            return True

        status = str(clarification.get("status", "done"))
        # User may have replied before the clarifier question is emitted.
        if status == "pending" and str(clarification.get("response", "")).strip():
            clarification["status"] = "done"
            clarification["waiting_emitted"] = False
            return True
        if status == "done":
            return True

        step = int(ctx.state.get("next_step_index", 0))

        if status == "pending":
            clarifier_text = await self._build_clarifier_prompt(ctx.state)
            # A user reply may arrive while clarifier prompt is being generated.
            # In that case, do not overwrite with awaiting_user.
            if str(clarification.get("status", "")).strip().lower() == "done" or str(clarification.get("response", "")).strip():
                clarification["status"] = "done"
                clarification["waiting_emitted"] = False
                return True
            clarification["prompt"] = clarifier_text
            history = clarification.get("history")
            if isinstance(history, list):
                history.append({"role": "assistant", "content": clarifier_text, "at": utc_now_iso()})

            upper = clarifier_text.strip().upper()
            if upper in {"CLEAR", "CHAT"}:
                clarification["status"] = "done"
                clarification["waiting_emitted"] = False
                await self._emit(
                    run_id=ctx.run_id,
                    step=step,
                    node="clarifier",
                    event_type="clarifier_auto_clear",
                    payload={
                        "message": clarifier_text,
                    },
                )
                return True

            clarification["status"] = "awaiting_user"
            clarification["waiting_emitted"] = False
            await self._emit(
                run_id=ctx.run_id,
                step=step,
                node="clarifier",
                event_type="clarifier_question",
                payload={
                    "text": clarifier_text,
                    "message": self._lang_text(
                        ctx.state,
                        "请先回复澄清问题（例如选择 A/B/C 并补充输出要求），然后开始正式研究。",
                        "Please answer the clarification question first (e.g., choose A/B/C and add output requirements).",
                    ),
                },
            )
            return False

        if status == "awaiting_user":
            if not clarification.get("waiting_emitted"):
                clarification["waiting_emitted"] = True
                await self._emit(
                    run_id=ctx.run_id,
                    step=step,
                    node="clarifier",
                    event_type="clarifier_waiting",
                    payload={
                        "message": self._lang_text(
                            ctx.state,
                            "等待用户确认研究方向与输出要求。",
                            "Waiting for user clarification before starting research.",
                        )
                    },
                )
            return False

        return True

    async def _build_clarifier_prompt(self, state: dict[str, Any]) -> str:
        goal = str(state.get("goal", "")).strip()
        if not goal:
            return self._lang_text(
                state,
                "CLEAR",
                "CLEAR",
            )

        lowered = goal.lower()
        trivial_tokens = {"hello", "hi", "test", "你好", "测试"}
        if lowered in trivial_tokens or re.fullmatch(r"\s*\d+\s*[\+\-\*\/]\s*\d+\s*", lowered):
            return "CHAT"

        clarification = state.get("clarification", {})
        history = clarification.get("history", []) if isinstance(clarification, dict) else []
        history_lines: list[str] = []
        if isinstance(history, list):
            for item in history[-8:]:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role", "")).strip() or "user"
                content = str(item.get("content", "")).strip()
                if content:
                    history_lines.append(f"{role}: {content}")
        conversation_history = "\n".join(history_lines)

        if not self.llm.enabled:
            if self._is_zh(state):
                return (
                    "AI Insight: 这个主题可以从市场、技术和执行策略三条路径展开，结论会因研究焦点不同而差异很大。\n\n"
                    "请选择研究重点：\n\n"
                    "A. 现状与格局: 聚焦核心现状、关键参与方和近期变化。 (Suitable for: 决策者)\n\n"
                    "B. 方法与机制: 聚焦底层机制、驱动因素和可验证证据。 (Suitable for: 研究与分析团队)\n\n"
                    "C. 落地与建议: 聚焦可执行方案、风险与优先级。 (Suitable for: 业务负责人)\n\n"
                    "问题: 你更看重“现状判断、机制解释、还是落地建议”？并说明输出格式（如报告/表格/清单）。"
                )
            return (
                "AI Insight: This topic can be approached through market context, mechanism analysis, or execution strategy, "
                "and the final output will differ significantly by focus.\n\n"
                "Please choose a research focus:\n\n"
                "A. Current Landscape: Focus on status quo, key players, and recent shifts. (Suitable for: Decision-makers)\n\n"
                "B. Mechanisms & Evidence: Focus on drivers, methods, and verifiable evidence. (Suitable for: Analysts)\n\n"
                "C. Execution & Recommendations: Focus on actions, risks, and priorities. (Suitable for: Operators)\n\n"
                "Question: Which do you want first: landscape, mechanisms, or execution recommendations? Also specify output format."
            )

        system_prompt = (
            "# 角色\n"
            "你是研究需求澄清专家。目标不是直接回答问题，而是先澄清用户真实研究意图。\n\n"
            "# 关键约束\n"
            "1. 不要生成研究计划。\n"
            "2. 不要直接回答用户问题。\n"
            "3. 不要生成编号步骤（1、2、3）。\n"
            "4. 必须提供 3 个不同的研究方向（A/B/C）供用户选择。\n\n"
            "5. 不要输出 Markdown 强调符号，不要出现 ** 或 *****。\n\n"
            "# 闲聊处理\n"
            "若用户输入为 \"Hello\"、\"Hi\"、\"Test\" 或简单算术（如 \"1+1\"），只输出：CHAT\n\n"
            "# 终止条件\n"
            "若用户已经明确选择路径，或意图已非常清晰，只输出：CLEAR\n\n"
            "# 输出格式（严格遵守）\n"
            "AI Insight: [用 1 句话概括该主题复杂性]\n\n"
            "请选择研究重点：\n\n"
            "A. [方向名]: [方向描述] (Suitable for: [适用人群])\n\n"
            "B. [方向名]: [方向描述] (Suitable for: [适用人群])\n\n"
            "C. [方向名]: [方向描述] (Suitable for: [适用人群])\n\n"
            "问题: [一个用于缩小范围的追问]\n\n"
            "# 语言\n"
            "输出语言必须与用户输入语言一致。"
        )
        user_prompt = self._lang_text(
            state,
            f"对话历史:\n{conversation_history}\n\n当前用户问题: {goal}",
            f"Conversation History:\n{conversation_history}\n\nCurrent User Query: {goal}",
        )

        response = await self.llm.chat(
            model=self.settings.llm_model_supervisor,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=700,
        )
        if response is None or not response.text.strip():
            return self._lang_text(
                state,
                (
                    "AI Insight: 这个话题在研究目标、证据口径和最终输出形式上都存在分歧空间。\n\n"
                    "请选择研究重点：\n\n"
                    "A. 全景认知: 梳理现状、核心问题与关键参与方。 (Suitable for: 决策层)\n\n"
                    "B. 证据剖析: 深挖关键因素、数据证据与争议点。 (Suitable for: 研究人员)\n\n"
                    "C. 行动建议: 提供可执行方案、风险边界和优先级。 (Suitable for: 业务执行者)\n\n"
                    "问题: 你希望最终输出偏“洞察报告”还是“行动清单”？是否有篇幅和格式要求？"
                ),
                (
                    "AI Insight: This topic has multiple valid scopes with different evidence and output expectations.\n\n"
                    "Please choose a research focus:\n\n"
                    "A. Landscape View: Map status quo, key issues, and stakeholders. (Suitable for: Executives)\n\n"
                    "B. Evidence Deep Dive: Examine drivers, data, and conflicting evidence. (Suitable for: Analysts)\n\n"
                    "C. Actionable Playbook: Prioritized actions, risks, and execution path. (Suitable for: Operators)\n\n"
                    "Question: Should the output be a strategy brief or an action checklist? Any format/length constraints?"
                ),
            )
        return self._strip_markdown_markers(response.text.strip())

    async def _apply_clarification_response(self, state: dict[str, Any], response_text: str) -> dict[str, Any]:
        content = str(response_text or "").strip()
        updates: dict[str, Any] = {
            "selected_focus": "",
            "new_constraints": [],
            "output_requirements": [],
        }
        if not content:
            return updates

        if self.llm.enabled:
            prompt = self._lang_text(
                state,
                (
                    "请把用户对澄清问题的回复转成 JSON。"
                    "输出 ONLY JSON，结构："
                    '{"selected_focus":"", "refined_goal":"", "new_constraints":["..."], "output_requirements":["..."]}。'
                    "不要解释文字。\n"
                    f"原始目标: {state.get('goal','')}\n"
                    f"用户回复: {content}"
                ),
                (
                    "Convert the user's clarification response into JSON. "
                    "Output ONLY JSON with shape: "
                    '{"selected_focus":"", "refined_goal":"", "new_constraints":["..."], "output_requirements":["..."]}.\n'
                    f"Original goal: {state.get('goal','')}\n"
                    f"User response: {content}"
                ),
            )
            parsed = await self.llm.chat(
                model=self.settings.llm_model_supervisor,
                system_prompt=self._lang_text(
                    state,
                    "你是研究需求解析器，只输出合法 JSON。",
                    "You are a requirement parser that outputs valid JSON only.",
                ),
                user_prompt=prompt,
                max_tokens=500,
            )
            data = parse_json_object(parsed.text) if parsed is not None else None
            if isinstance(data, dict):
                selected_focus = str(data.get("selected_focus", "")).strip()
                refined_goal = str(data.get("refined_goal", "")).strip()
                new_constraints = data.get("new_constraints", [])
                output_requirements = data.get("output_requirements", [])

                if selected_focus:
                    updates["selected_focus"] = selected_focus
                if refined_goal:
                    state["goal"] = refined_goal
                    state["language"] = self._detect_language(refined_goal)
                    updates["refined_goal"] = refined_goal

                if isinstance(new_constraints, list):
                    for item in new_constraints:
                        text = str(item).strip()
                        if text and text not in state["constraints"]:
                            state["constraints"].append(text)
                            updates["new_constraints"].append(text)
                if isinstance(output_requirements, list):
                    for item in output_requirements:
                        text = str(item).strip()
                        if not text:
                            continue
                        requirement = self._lang_text(
                            state,
                            f"输出要求：{text}",
                            f"Output requirement: {text}",
                        )
                        if requirement not in state["constraints"]:
                            state["constraints"].append(requirement)
                        updates["output_requirements"].append(text)

        if not updates["new_constraints"] and not updates["output_requirements"] and "refined_goal" not in updates:
            choice_match = re.search(r"\b([abcABC])\b", content)
            if choice_match:
                selected = choice_match.group(1).upper()
                updates["selected_focus"] = selected
                focus_constraint = self._lang_text(
                    state,
                    f"用户选择研究方向：{selected}",
                    f"User selected focus: {selected}",
                )
                if focus_constraint not in state["constraints"]:
                    state["constraints"].append(focus_constraint)
                    updates["new_constraints"].append(focus_constraint)

            fallback_constraint = self._lang_text(
                state,
                f"用户澄清补充：{content}",
                f"User clarification: {content}",
            )
            if fallback_constraint not in state["constraints"]:
                state["constraints"].append(fallback_constraint)
                updates["new_constraints"].append(fallback_constraint)

        return updates

    def _parse_steer(self, steer: dict[str, Any]) -> dict[str, Any]:
        raw = steer["content"].strip()

        def value_after_colon(text: str) -> str:
            if ":" in text:
                return text.split(":", 1)[1].strip()
            if "：" in text:
                return text.split("：", 1)[1].strip()
            return ""

        segments = [
            segment.strip()
            for segment in raw.replace("；", ";").replace("\n", ";").split(";")
            if segment.strip()
        ]
        if not segments:
            segments = [raw]

        changes: list[dict[str, Any]] = []
        for segment in segments:
            lowered = segment.lower()

            if lowered.startswith("change goal") or lowered.startswith("change question") or "改问题" in segment or "换方向" in segment:
                candidate = value_after_colon(segment)
                if candidate:
                    changes.append({"type": "goal", "value": candidate})
                    continue

            if lowered.startswith("ban source") or lowered.startswith("exclude source") or "禁用来源" in segment or "禁来源" in segment:
                candidate = value_after_colon(segment)
                if candidate:
                    changes.append({"type": "blocked_source", "value": candidate})
                    continue

            if lowered.startswith("allow source") or lowered.startswith("preferred source") or "增加来源" in segment or "只用来源" in segment:
                candidate = value_after_colon(segment)
                if candidate:
                    changes.append({"type": "required_source", "value": candidate})
                    continue

            if lowered.startswith("constraint") or "约束" in segment:
                candidate = value_after_colon(segment) or segment
                changes.append({"type": "constraint", "value": candidate})
                continue

            if "stop" in lowered or "停止" in segment:
                changes.append({"type": "stop", "value": True})
                continue

            if "replan" in lowered or "重规划" in segment or "重新计划" in segment:
                changes.append({"type": "replan", "value": True})
                continue

            changes.append({"type": "constraint", "value": segment})

        return {
            "version": steer["version"],
            "scope": steer["scope"],
            "content": raw,
            "applied_at": None,
            "review_status": "pending",
            "changes": changes,
        }

    async def _supervisor(self, ctx: RunContext) -> None:
        step = int(ctx.state["next_step_index"])
        pending: list[dict[str, Any]] = ctx.state["pending_steer_changes"]

        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        if pending:
            for note in pending:
                per_note_accepted: list[dict[str, Any]] = []
                per_note_rejected: list[dict[str, Any]] = []

                for change in note["changes"]:
                    verdict = self._supervisor_verdict(ctx.state, change)
                    if verdict["accepted"]:
                        self._apply_change(ctx.state, change)
                        per_note_accepted.append({"change": change, "reason": verdict["reason"]})
                    else:
                        per_note_rejected.append({"change": change, "reason": verdict["reason"]})

                note["review_status"] = "reviewed"
                note["applied_at"] = utc_now_iso()
                note["accepted"] = per_note_accepted
                note["rejected"] = per_note_rejected

                accepted.extend(per_note_accepted)
                rejected.extend(per_note_rejected)

                ctx.state["supervisor"]["last_reviewed_version"] = max(
                    int(ctx.state["supervisor"]["last_reviewed_version"]),
                    int(note["version"]),
                )

            ctx.state["pending_steer_changes"] = []
            if accepted:
                ctx.state["flags"]["replan"] = True
            ctx.state["flags"]["interrupt_requested"] = False
            ctx.interrupt_event.clear()

            ctx.state["supervisor"]["accepted_directives"].append(
                {
                    "timestamp": utc_now_iso(),
                    "count": len(accepted),
                    "items": accepted,
                }
            )
            if rejected:
                ctx.state["supervisor"]["rejected_directives"].append(
                    {
                        "timestamp": utc_now_iso(),
                        "count": len(rejected),
                        "items": rejected,
                    }
                )

        latest_completed = self._latest_completed_step(ctx.state)
        latest_step_id = ""
        latest_summary_text = ""
        has_new_evidence = False
        step_summary_generated = False

        if isinstance(latest_completed, dict):
            latest_step_id = str(latest_completed.get("step_id", "")).strip()
            latest_summary_text = str(latest_completed.get("step_summary", "")).strip()
            has_new_evidence = bool(latest_completed.get("notes")) or bool(latest_completed.get("top_sources"))

            if has_new_evidence and not latest_summary_text:
                step_payload = {
                    "id": latest_completed.get("step_id", ""),
                    "title": latest_completed.get("title", ""),
                    "objective": latest_completed.get("objective", ""),
                    "query": (latest_completed.get("queries") or [""])[0] if isinstance(latest_completed.get("queries"), list) else "",
                }
                notes_payload = latest_completed.get("notes", [])
                synthesis_payload = latest_completed.get("synthesis", {}) if isinstance(latest_completed.get("synthesis"), dict) else {}
                self_check_payload = latest_completed.get("self_check", {}) if isinstance(latest_completed.get("self_check"), dict) else {}
                generated_summary = await self._build_step_summary(
                    ctx.state,
                    step_payload,
                    notes_payload if isinstance(notes_payload, list) else [],
                    synthesis_payload,
                    self_check_payload,
                )
                latest_summary_text = str(generated_summary).strip()
                if latest_summary_text:
                    latest_completed["step_summary"] = latest_summary_text
                    step_summary_generated = True
                    await self._emit(
                        run_id=ctx.run_id,
                        step=step,
                        node="reporter",
                        event_type="step_summary",
                        payload={
                            "step_id": latest_step_id,
                            "summary": latest_summary_text,
                            "auto_generated": True,
                            "source": "supervisor_quality_gate",
                        },
                    )

        quality_gate_entry = {
            "timestamp": utc_now_iso(),
            "step": step,
            "latest_step_id": latest_step_id,
            "has_new_evidence": has_new_evidence,
            "has_step_summary": bool(latest_summary_text),
            "auto_generated": step_summary_generated,
            "rhythm_stage": self._research_rhythm_stage(ctx.state),
        }
        ctx.state["supervisor"].setdefault("quality_gate_history", []).append(quality_gate_entry)

        summary = await self._supervisor_summary(ctx.state, accepted, rejected)
        reflection = await self._supervisor_reflection(ctx.state)
        decision = await self._supervisor_decision(ctx.state, accepted, rejected)
        ctx.state["supervisor"]["latest_guidance"] = reflection
        ctx.state["supervisor"]["reflections"].append(
            {
                "timestamp": utc_now_iso(),
                "step": step,
                "guidance": reflection,
                "steer_handled": bool(pending),
            }
        )
        ctx.state["supervisor"]["decisions"].append(
            {
                "timestamp": utc_now_iso(),
                "step": step,
                "decision": decision,
            }
        )
        if decision.get("action") == "end":
            ctx.state["flags"]["stop_requested"] = True
        elif decision.get("action") == "call_planner":
            ctx.state["flags"]["replan"] = True
        elif decision.get("action") == "call_reporter":
            # We keep the loop non-blocking: reporter summaries are ensured by quality gate,
            # then planner continues with the updated summary context.
            ctx.state["flags"]["replan"] = True

        output = {
            "accepted": accepted,
            "rejected": rejected,
            "summary": summary,
            "reflection": reflection,
            "decision": decision,
            "quality_gate": quality_gate_entry,
            "state_head": {
                "goal": ctx.state["goal"],
                "constraints": ctx.state["constraints"],
                "required_sources": ctx.state["required_sources"],
                "blocked_sources": ctx.state["blocked_sources"],
            },
            "interrupt_resolved": bool(pending),
            "mode": "steer_review" if pending else "periodic_reflection",
        }

        await self._emit(
            run_id=ctx.run_id,
            step=step,
            node="supervisor",
            event_type="supervisor_output",
            payload=output,
        )

    def _supervisor_verdict(self, state: dict[str, Any], change: dict[str, Any]) -> dict[str, Any]:
        change_type = change.get("type")
        value = str(change.get("value", "")).strip()

        if change_type == "note":
            return {
                "accepted": False,
                "reason": self._lang_text(
                    state,
                    "自由备注已记录，但不具备可执行动作。",
                    "free-form note was stored but not executable",
                ),
            }

        if change_type == "blocked_source" and value in state["required_sources"]:
            return {
                "accepted": False,
                "reason": self._lang_text(
                    state,
                    "与已要求的来源冲突。",
                    "conflicts with already-required source",
                ),
            }

        if change_type == "required_source" and value in state["blocked_sources"]:
            return {
                "accepted": False,
                "reason": self._lang_text(
                    state,
                    "与已屏蔽来源冲突。",
                    "conflicts with blocked source",
                ),
            }

        return {
            "accepted": True,
            "reason": self._lang_text(
                state,
                "已应用到运行状态。",
                "applied to run state",
            ),
        }

    def _apply_change(self, state: dict[str, Any], change: dict[str, Any]) -> None:
        change_type = change.get("type")
        value = change.get("value")

        if change_type == "goal" and isinstance(value, str) and value:
            state["goal"] = value
            state["language"] = self._detect_language(value)
            return

        if change_type == "constraint" and isinstance(value, str) and value:
            if value not in state["constraints"]:
                state["constraints"].append(value)
            return

        if change_type == "required_source" and isinstance(value, str) and value:
            if value not in state["required_sources"]:
                state["required_sources"].append(value)
            return

        if change_type == "blocked_source" and isinstance(value, str) and value:
            if value not in state["blocked_sources"]:
                state["blocked_sources"].append(value)
            return

        if change_type == "stop":
            state["flags"]["stop_requested"] = True
            return

        if change_type == "replan":
            state["flags"]["replan"] = True

    async def _supervisor_summary(
        self,
        state: dict[str, Any],
        accepted: list[dict[str, Any]],
        rejected: list[dict[str, Any]],
    ) -> str:
        if not accepted and not rejected:
            return self._lang_text(
                state,
                "本轮无新增干预，沿用当前目标与约束继续推进。",
                "No new steer directives this round; continue with the current goal and constraints.",
            )

        if not self.llm.enabled:
            return self._lang_text(
                state,
                f"已接受 {len(accepted)} 条指令，拒绝 {len(rejected)} 条。当前目标：{state['goal']}",
                f"Accepted {len(accepted)} directives and rejected {len(rejected)}. Goal now: {state['goal']}",
            )

        latest_completed = self._latest_completed_step(state)
        latest_step_summary = ""
        if isinstance(latest_completed, dict):
            latest_step_summary = str(latest_completed.get("step_summary", "")).strip()
        quality_gate = state.get("supervisor", {}).get("quality_gate_history", [])
        latest_gate = quality_gate[-1] if isinstance(quality_gate, list) and quality_gate else {}
        rhythm_stage = self._research_rhythm_stage(state)

        prompt = self._lang_text(
            state,
            (
                "你是 supervisor：研究流程调度器 + 可交互改向控制器。"
                "请用 2-3 句总结本轮 steering 处理结果，必须覆盖："
                "1) 哪些变更被接受/拒绝；2) 对 goal/constraints/source_prefs 的影响；3) 是否需要重规划。"
                "4) 质量闸门状态（新证据/完成步骤是否已有可读 step_summary）。"
                "5) 当前节奏阶段（框架→现实证据→边界/反例→凝练交付物）。"
                "输出中文纯文本，不要 JSON。\n"
                f"Accepted: {json.dumps(accepted, ensure_ascii=False)}\n"
                f"Rejected: {json.dumps(rejected, ensure_ascii=False)}\n"
                f"Current state head: {json.dumps({'goal': state['goal'], 'constraints': state['constraints'], 'required_sources': state['required_sources'], 'blocked_sources': state['blocked_sources']}, ensure_ascii=False)}\n"
                f"Latest rhythm stage: {rhythm_stage}\n"
                f"Latest quality gate: {json.dumps(latest_gate, ensure_ascii=False)}\n"
                f"Latest step summary (short): {latest_step_summary[:500]}"
            ),
            (
                "You are the supervisor and steering controller. Summarize steering handling in 2-3 sentences, covering: "
                "accepted/rejected deltas, impact on goal/constraints/source_prefs, whether replanning is needed, "
                "quality-gate status (readable step_summary), and current 2-4 round rhythm stage. "
                "Output plain text, not JSON.\n"
                f"Accepted: {json.dumps(accepted, ensure_ascii=False)}\n"
                f"Rejected: {json.dumps(rejected, ensure_ascii=False)}\n"
                f"Current state head: {json.dumps({'goal': state['goal'], 'constraints': state['constraints'], 'required_sources': state['required_sources'], 'blocked_sources': state['blocked_sources']}, ensure_ascii=False)}\n"
                f"Latest rhythm stage: {rhythm_stage}\n"
                f"Latest quality gate: {json.dumps(latest_gate, ensure_ascii=False)}\n"
                f"Latest step summary (short): {latest_step_summary[:500]}"
            ),
        )

        response = await self.llm.chat(
            model=self.settings.llm_model_supervisor,
            system_prompt=self._lang_text(
                state,
                (
                    "你是 supervisor：一个“研究流程调度器 + 可交互改向控制器”。"
                    "你需要在保证研究质量的前提下，用最少步骤完成用户目标，并优先响应 steering。"
                    "质量闸门：每步可验证、可中断、可汇总；证据要进入 evidence_store。"
                    "每轮若有新证据或步骤完成，必须确保存在可读 step_summary，并让 planner 下一轮引用它。"
                    "遵循 2-4 轮节奏：框架 -> 现实证据 -> 边界/反例 -> 凝练交付物，避免过早跑到合规/政策旁支。"
                    "若 goal 偏产品/MVP，不允许只做学术综述。始终用中文。"
                ),
                (
                    "You are the supervisor orchestrator and steering controller. "
                    "Prioritize steering, enforce step_summary quality gates for each new evidence/step, follow a 2-4 round rhythm "
                    "(framework -> real evidence -> boundary/counterexample -> condensed deliverable), and avoid purely academic drift for product/MVP goals."
                ),
            ),
            user_prompt=prompt,
            max_tokens=220,
        )
        if response is None:
            return self._lang_text(
                state,
                f"已接受 {len(accepted)} 条指令，拒绝 {len(rejected)} 条。当前目标：{state['goal']}",
                f"Accepted {len(accepted)} directives and rejected {len(rejected)}. Goal now: {state['goal']}",
            )
        return response.text.strip()

    async def _supervisor_reflection(self, state: dict[str, Any]) -> str:
        completed_steps = state.get("completed_steps", [])
        latest_step = completed_steps[-1] if isinstance(completed_steps, list) and completed_steps else None
        latest_step_summary = str(latest_step.get("step_summary", "")).strip() if isinstance(latest_step, dict) else ""
        rhythm_stage = self._research_rhythm_stage(state)

        if not self.llm.enabled:
            if not isinstance(latest_step, dict):
                return self._lang_text(
                    state,
                    "先完成问题边界定义与高质量来源盘点，再进入证据提取。",
                    "Start with scope definition and high-quality source collection before deeper evidence extraction.",
                )
            latest_summary = (
                str(latest_step.get("synthesis", {}).get("summary", "")).strip()
                if isinstance(latest_step.get("synthesis"), dict)
                else ""
            )
            if latest_summary:
                return self._lang_text(
                    state,
                    f"上一轮结论：{latest_summary}。下一轮请优先补齐证据缺口和反例验证。",
                    f"Last-round takeaway: {latest_summary}. Next, prioritize evidence gaps and contradiction checks.",
                )
            return self._lang_text(
                state,
                "下一轮请优先补齐证据缺口，并明确不确定性来源。",
                "Next, prioritize unresolved evidence gaps and clarify uncertainty sources.",
            )

        latest_payload = latest_step if isinstance(latest_step, dict) else {}
        steering_tail = state.get("steering_notes", [])
        if isinstance(steering_tail, list):
            steering_tail = steering_tail[-3:]
        else:
            steering_tail = []
        prompt = self._lang_text(
            state,
            (
                "你是 deep research 的 supervisor。"
                "请基于最新已完成轮次做反思，并给 planner 下一轮可执行指导。"
                "输出 3-5 句中文，不要 markdown。"
                "必须包含：本轮发现、证据缺口、下一轮优先方向、对用户纠偏的落实。"
                "必须要求 planner 在下一轮 Progress Summary 中引用最新 step_summary（可概述，不要照抄）。"
                f"当前节奏阶段：{rhythm_stage}（框架→现实证据→边界/反例→凝练交付物）。\n"
                f"Goal: {state['goal']}\n"
                f"Constraints: {json.dumps(state['constraints'], ensure_ascii=False)}\n"
                f"Recent steering notes: {json.dumps(steering_tail, ensure_ascii=False)}\n"
                f"Latest step summary: {latest_step_summary[:1400]}\n"
                f"Latest completed step: {json.dumps(latest_payload, ensure_ascii=False)[:9000]}"
            ),
            (
                "You are the supervisor in a deep-research loop. "
                "Reflect on the latest completed step and give actionable next-round guidance to planner. "
                "Output 3-5 plain-text sentences with findings, evidence gaps, next priority, and steering alignment. "
                "Require planner to explicitly reference the latest step_summary in the next Progress Summary. "
                f"Current rhythm stage: {rhythm_stage} (framework -> real evidence -> boundary/counterexample -> condensed deliverable).\n"
                f"Goal: {state['goal']}\n"
                f"Constraints: {json.dumps(state['constraints'], ensure_ascii=False)}\n"
                f"Recent steering notes: {json.dumps(steering_tail, ensure_ascii=False)}\n"
                f"Latest step summary: {latest_step_summary[:1400]}\n"
                f"Latest completed step: {json.dumps(latest_payload, ensure_ascii=False)[:9000]}"
            ),
        )
        response = await self.llm.chat(
            model=self.settings.llm_model_supervisor,
            system_prompt=self._lang_text(
                state,
                (
                    "你是 supervisor：研究流程调度器 + 可交互改向控制器。"
                    "决策策略：小步快跑，优先下一步最关键证据；每步产出都应可被 reporter step_summary 总结。"
                    "每轮出现新证据/完成步骤时，必须有可读 step_summary，且下一轮 planner 要引用该 summary。"
                    "强制 2-4 轮节奏：框架 -> 现实证据 -> 边界/反例 -> 凝练交付物，避免早期合规/政策旁支。"
                    "如果用户目标偏产品/MVP，必须推动用户画像/场景、方案对比、评估指标进入下一轮。"
                    "始终用中文。"
                ),
                (
                    "You are a rigorous supervisor orchestrator. "
                    "Use small-step decisions, enforce step_summary quality gates with explicit carry-over into planner summaries, "
                    "follow a 2-4 round rhythm, and include product-oriented dimensions when needed."
                ),
            ),
            user_prompt=prompt,
            max_tokens=360,
        )

        if response is None or not response.text.strip():
            return self._lang_text(
                state,
                "下一轮请围绕关键证据缺口补充高质量来源，并明确反例与不确定性。",
                "Next round: fill critical evidence gaps with high-quality sources and make counterexamples/uncertainties explicit.",
            )
        return response.text.strip()

    async def _supervisor_decision(
        self,
        state: dict[str, Any],
        accepted: list[dict[str, Any]],
        rejected: list[dict[str, Any]],
    ) -> dict[str, Any]:
        allowed_actions = {"call_planner", "call_researcher", "call_reporter", "end"}

        pending_steps = [item for item in state.get("plan", []) if isinstance(item, dict) and item.get("status") == "pending"]
        latest_completed = self._latest_completed_step(state)
        latest_has_evidence = (
            bool(latest_completed.get("notes")) or bool(latest_completed.get("top_sources"))
            if isinstance(latest_completed, dict)
            else False
        )
        latest_has_step_summary = bool(str(latest_completed.get("step_summary", "")).strip()) if isinstance(latest_completed, dict) else False
        rhythm_stage = self._research_rhythm_stage(state)
        fallback_action = "call_planner"
        if latest_has_evidence and not latest_has_step_summary:
            fallback_action = "call_reporter"
        elif state.get("flags", {}).get("stop_requested"):
            fallback_action = "end"
        elif pending_steps and not state.get("flags", {}).get("replan"):
            fallback_action = "call_researcher"
        elif len(state.get("completed_steps", [])) >= int(state.get("max_steps", 1)):
            fallback_action = "call_reporter"

        fallback = {
            "action": fallback_action,
            "reason": self._lang_text(
                state,
                "基于当前状态执行默认调度决策。",
                "Default supervisor decision from current runtime state.",
            ),
            "inputs": {
                "goal": state.get("goal", ""),
                "constraints": state.get("constraints", []),
                "source_prefs": {
                    "required": state.get("required_sources", []),
                    "blocked": state.get("blocked_sources", []),
                },
            },
        }

        if not self.llm.enabled:
            return fallback

        prompt = self._lang_text(
            state,
            (
                "请扮演 supervisor 做一次调度决策。"
                "严格输出一个 JSON："
                '{"action":"call_planner|call_researcher|call_reporter|end","reason":"...","inputs":{...}}。'
                "不要输出任何额外文本。\n"
                f"Goal: {state.get('goal','')}\n"
                f"Constraints: {json.dumps(state.get('constraints', []), ensure_ascii=False)}\n"
                f"Source prefs: {json.dumps({'required': state.get('required_sources', []), 'blocked': state.get('blocked_sources', [])}, ensure_ascii=False)}\n"
                f"Steering accepted: {json.dumps(accepted, ensure_ascii=False)}\n"
                f"Steering rejected: {json.dumps(rejected, ensure_ascii=False)}\n"
                f"Pending plan steps: {json.dumps([s.get('id') for s in pending_steps], ensure_ascii=False)}\n"
                f"Completed steps: {len(state.get('completed_steps', []))}\n"
                f"Max steps: {state.get('max_steps', 0)}\n"
                f"Latest step has new evidence: {latest_has_evidence}\n"
                f"Latest step has step_summary: {latest_has_step_summary}\n"
                f"Rhythm stage: {rhythm_stage}\n"
                "决策约束：若有新 evidence 或 step 完成但缺少 step_summary，必须优先 call_reporter。"
                "优先保持 2-4 轮节奏（框架→现实证据→边界/反例→凝练交付物），避免过早跑到合规/政策旁支。"
            ),
            (
                "Act as supervisor and output one decision JSON only with shape "
                '{"action":"call_planner|call_researcher|call_reporter|end","reason":"...","inputs":{...}}.\n'
                f"Goal: {state.get('goal','')}\n"
                f"Constraints: {json.dumps(state.get('constraints', []), ensure_ascii=False)}\n"
                f"Source prefs: {json.dumps({'required': state.get('required_sources', []), 'blocked': state.get('blocked_sources', [])}, ensure_ascii=False)}\n"
                f"Steering accepted: {json.dumps(accepted, ensure_ascii=False)}\n"
                f"Steering rejected: {json.dumps(rejected, ensure_ascii=False)}\n"
                f"Pending plan steps: {json.dumps([s.get('id') for s in pending_steps], ensure_ascii=False)}\n"
                f"Completed steps: {len(state.get('completed_steps', []))}\n"
                f"Max steps: {state.get('max_steps', 0)}\n"
                f"Latest step has new evidence: {latest_has_evidence}\n"
                f"Latest step has step_summary: {latest_has_step_summary}\n"
                f"Rhythm stage: {rhythm_stage}\n"
                "Constraint: if new evidence/step completion exists without a readable step_summary, choose call_reporter first. "
                "Keep 2-4 round rhythm (framework -> real evidence -> boundary/counterexample -> condensed deliverable)."
            ),
        )

        response = await self.llm.chat(
            model=self.settings.llm_model_supervisor,
            system_prompt=self._lang_text(
                state,
                (
                    "你是 supervisor：一个“研究流程调度器 + 可交互改向控制器”。"
                    "steering 优先级最高；必要时触发 replan。"
                    "确保每步可验证、可中断、可被 reporter 总结。"
                    "每轮若有新证据或步骤完成，必须有可读 step_summary，并要求 planner 下一轮引用。"
                    "遵循 2-4 轮节奏：框架 -> 现实证据 -> 边界/反例 -> 凝练交付物，避免过早分散到合规/政策旁支。"
                    "若目标偏产品/MVP，不允许只做学术综述。"
                ),
                (
                    "You are the supervisor orchestrator and steering controller. "
                    "Prioritize steering, enforce step_summary quality gates with planner carry-over, keep 2-4 round rhythm, "
                    "and avoid purely academic drift for product/MVP goals."
                ),
            ),
            user_prompt=prompt,
            max_tokens=380,
        )
        if response is None or not response.text.strip():
            return fallback

        data = parse_json_object(response.text)
        if not isinstance(data, dict):
            return fallback

        action = str(data.get("action", "")).strip()
        if action not in allowed_actions:
            action = fallback["action"]
        reason = str(data.get("reason", "")).strip() or fallback["reason"]
        inputs = data.get("inputs", {})
        if not isinstance(inputs, dict):
            inputs = fallback["inputs"]

        return {
            "action": action,
            "reason": reason,
            "inputs": inputs,
        }

    async def _planner(self, ctx: RunContext) -> None:
        step = int(ctx.state["next_step_index"])
        remaining_steps = max(0, int(ctx.state["max_steps"]) - step)
        has_pending = any(item["status"] == "pending" for item in ctx.state["plan"])

        if has_pending and not ctx.state["flags"]["replan"]:
            await self._emit(
                run_id=ctx.run_id,
                step=step,
                node="planner",
                event_type="planner_kept",
                payload={
                    "pending_steps": [item["id"] for item in ctx.state["plan"] if item["status"] == "pending"],
                },
            )
            return

        if remaining_steps <= 0:
            await self._emit(
                run_id=ctx.run_id,
                step=step,
                node="planner",
                event_type="planner_skipped",
                payload={"reason": "no remaining steps"},
            )
            return

        plan_steps, planner_notes = await self._generate_plan(ctx.state, remaining_steps=remaining_steps)

        if not plan_steps:
            plan_steps = self._fallback_plan(ctx.state, remaining_steps=remaining_steps)

        base_round = int(ctx.state.get("next_step_index", 0))
        for index, item in enumerate(plan_steps, start=1):
            item["id"] = f"step-{base_round + index}"
            item["status"] = "pending"

        ctx.state["plan_revision"] = int(ctx.state.get("plan_revision", 0)) + 1
        ctx.state["plan"] = plan_steps
        ctx.state["flags"]["replan"] = False
        ctx.state["flags"]["auto_replan_attempts"] = 0

        await self._emit(
            run_id=ctx.run_id,
            step=step,
            node="planner",
            event_type="planner_output",
            payload={
                "goal": ctx.state["goal"],
                "constraints": ctx.state["constraints"],
                "steps": plan_steps,
                "plan_revision": ctx.state["plan_revision"],
                "remaining_steps": remaining_steps,
                "notes": planner_notes,
            },
        )

    async def _generate_plan(self, state: dict[str, Any], remaining_steps: int) -> tuple[list[dict[str, Any]], str]:
        if remaining_steps <= 0:
            return [], "planner skipped (remaining_steps <= 0)"

        completed_steps = state.get("completed_steps", [])
        completed_summaries: list[dict[str, Any]] = []
        for step in completed_steps[-4:]:
            if not isinstance(step, dict):
                continue
            queries = step.get("queries", [])
            first_query = ""
            if isinstance(queries, list):
                for candidate in queries:
                    text = str(candidate).strip()
                    if text:
                        first_query = text
                        break
            completed_summaries.append(
                {
                    "title": str(step.get("title", "")).strip(),
                    "objective": str(step.get("objective", "")).strip(),
                    "query": first_query,
                    "queries": queries if isinstance(queries, list) else [],
                    "summary": str(step.get("synthesis", {}).get("summary", "")).strip()
                    if isinstance(step.get("synthesis"), dict)
                    else "",
                    "step_summary": str(step.get("step_summary", "")).strip(),
                    "evidence_count": len(step.get("notes", [])) if isinstance(step.get("notes"), list) else 0,
                    "uncertainties": step.get("self_check", {}).get("uncertainties", [])
                    if isinstance(step.get("self_check"), dict)
                    else [],
                }
            )

        target_steps = 1
        supervisor_guidance = str(state.get("supervisor", {}).get("latest_guidance", "")).strip()
        clarification_response = str(state.get("clarification", {}).get("response", "")).strip()
        latest_step_summary = ""
        if completed_summaries:
            latest_step_summary = str(completed_summaries[-1].get("step_summary", "")).strip()
        rhythm_stage = self._research_rhythm_stage(state)

        if not self.llm.enabled:
            return self._fallback_plan(state, remaining_steps=remaining_steps), "planner fallback (no llm key configured)"

        user_prompt = self._lang_text(
            state,
            (
                "你是 Planner：只负责规划下一步，不做执行。你的产出面向普通读者/做事的人。普通网站优先。"
                "每轮只输出一段完整自然语言（不要编号、不要长计划、不要学术腔、不要标题）。这段话必须同时包含两件事："
                "1) 已掌握的事实：仅可来自 evidence_store 或 step_summary；如果目前没有可靠证据，不要臆测，直接指出证据缺口。"
                "2) 下一步研究方向：用 1-3 句话说明接下来要弄清楚哪些关键问题。"
                "优质来源优先原则：优先普通但可靠、可读性强的来源（权威机构/大学/医院科普与指南、专业协会科普、成熟媒体深度解释、知名产品官方帮助中心/实践指南）；"
                "其次政府/标准/政策文件（仅做边界定义或必要背景）；学术论文仅在用户明确要求论文/学术或必须严谨到需要研究结论时才使用，且不主导。"
                "禁止：研究计划书、章节目录、大段理论背景、多层子步骤、学术术语堆砌、运行日志或元信息。"
                "\n返回 ONLY JSON，格式："
                '{"plan_summary":"...","steps":[{"id":"S1","title":"...","narrative_plan":"(单段完整自然语言)","progress_summary":["..."],"next_focus":"...",'
                '"next_directions":[{"priority":"P0|P1","direction":"...","evidence_to_find":"...","deliverable":"...","where_to_check":"..."}],"query":"...","intent":"...","method":"...","outputs":"...","payload":{"queries":["..."],"notes":"普通网站优先"}}]}'
                "\n强制规则："
                "1) next_focus 用 1-3 句自然语言。"
                "2) next_directions 最多 4 条，收敛到一个最稳主线。"
                "3) 禁止输出运行元信息：User input timeline / Steering update / Initial query / Clarification / 监督结论。"
                "4) 若存在 step_summary，本轮 narrative_plan 必须引用其要点（同义改写，不照抄）。"
                "5) 输出语言必须与用户问题一致。"
                "6) 禁止在 narrative_plan 中出现“【掌握的信息】/【下一步要查什么】/What We Know/What To Check Next”等标题词。"
                f"\nGoal: {state['goal']}"
                f"\nConstraints: {json.dumps(state['constraints'], ensure_ascii=False)}"
                f"\nPreferred sources: {json.dumps(state['required_sources'], ensure_ascii=False)}"
                f"\nBlocked sources: {json.dumps(state['blocked_sources'], ensure_ascii=False)}"
                f"\nClarification response: {clarification_response}"
                f"\nSupervisor guidance: {supervisor_guidance}"
                f"\nRhythm stage: {rhythm_stage}"
                f"\nLatest step summary: {latest_step_summary[:1600]}"
                f"\nRecent completed steps: {json.dumps(completed_summaries, ensure_ascii=False)}"
            ),
            (
                "You are Planner: plan the next step only, no execution. Write for normal readers/doers. Ordinary websites first. "
                "Output one complete natural-language paragraph each round (no numbering, no long plan, no headings, no academic tone): "
                "1) Facts already known, strictly from evidence_store or step_summary; if evidence is weak, explicitly state the gap and do not speculate. "
                "2) Next research direction in 1-3 natural sentences describing the key questions to clarify next. "
                "Source preference: ordinary but reliable readable sources first (institutional explainers, university/hospital guides, association explainers, mature media explainers, official product help/practice pages). "
                "Government/standard/policy docs are secondary for boundary definitions; papers only when explicitly requested or rigor-critical, never dominant by default. "
                "Do not output plan docs, chapter outlines, theory dumps, nested sub-steps, jargon piles, runtime logs, or meta info. "
                "Return ONLY JSON with shape: "
                '{"plan_summary":"...","steps":[{"id":"S1","title":"...","narrative_plan":"(one natural paragraph)","progress_summary":["..."],"next_focus":"...",'
                '"next_directions":[{"priority":"P0|P1","direction":"...","evidence_to_find":"...","deliverable":"...","where_to_check":"..."}],"query":"...","intent":"...","method":"...","outputs":"...","payload":{"queries":["..."],"notes":"ordinary websites first"}}]}\n'
                "Rules: next_focus must be 1-3 sentences; next_directions max 4; exclude runtime meta logs; language follows user language; paraphrase latest step_summary when available; avoid headings like What We Know / What To Check Next.\n"
                f"Goal: {state['goal']}\n"
                f"Constraints: {json.dumps(state['constraints'], ensure_ascii=False)}\n"
                f"Preferred sources: {json.dumps(state['required_sources'], ensure_ascii=False)}\n"
                f"Blocked sources: {json.dumps(state['blocked_sources'], ensure_ascii=False)}\n"
                f"Clarification response: {clarification_response}\n"
                f"Supervisor guidance: {supervisor_guidance}\n"
                f"Rhythm stage: {rhythm_stage}\n"
                f"Latest step summary: {latest_step_summary[:1600]}\n"
                f"Recent completed steps: {json.dumps(completed_summaries, ensure_ascii=False)}"
            ),
        )

        response = await self.llm.chat(
            model=self.settings.llm_model_planner,
            system_prompt=self._lang_text(
                state,
                (
                    "你是 Planner：只负责规划下一步，不做执行。产出面向普通读者/做事的人，普通网站优先。"
                    "每轮只输出一段完整自然语言。"
                    "内容必须同时覆盖：已确认事实（仅基于 evidence_store/step_summary）+ 下一步关键问题（1-3句）。"
                    "默认先用普通但可靠、可读性强的来源；政府/政策仅做边界；学术仅在必要严谨场景才补充且不主导。"
                    "禁止研究计划书、章节目录、大段理论背景、多层子步骤、术语堆砌、运行日志和元信息。"
                    "禁止出现“【掌握的信息】/【下一步要查什么】”等标题。"
                ),
                (
                    "You are Planner: plan the next step only for normal readers/doers. Ordinary reliable websites first. "
                    "Output exactly one natural paragraph with known evidence-grounded facts and next key direction in 1-3 sentences. "
                    "Avoid headings, academic tone, long planning docs, nested procedures, and runtime/meta logs."
                ),
            ),
            user_prompt=user_prompt,
            max_tokens=800,
        )

        if response is None:
            return self._fallback_plan(state, remaining_steps=remaining_steps), "planner fallback (llm request failed)"

        data = parse_json_object(response.text)
        steps = data.get("steps") if isinstance(data, dict) else None
        if not isinstance(steps, list):
            return self._fallback_plan(state, remaining_steps=remaining_steps), "planner fallback (invalid json from llm)"

        clean_steps: list[dict[str, Any]] = []
        seen_signatures: set[str] = set()
        completed_signatures = {
            self._normalize_step_signature(
                str(item.get("title", "")),
                str(item.get("query", "")),
            )
            for item in completed_summaries
            if isinstance(item, dict)
        }

        for item in steps[:target_steps]:
            if not isinstance(item, dict):
                continue
            title = self._normalize_plan_text(item.get("title", ""), max_len=90) or self._lang_text(state, "下一轮研究", "Next Research Step")
            progress_summary = self._normalize_plan_list(
                item.get("progress_summary", []) or item.get("progress", []),
                max_items=6,
                max_len=180,
            )
            if not progress_summary and latest_step_summary:
                progress_summary = [
                    self._lang_text(
                        state,
                        f"承接上一轮 step_summary：{self._normalize_plan_text(latest_step_summary, max_len=140)}",
                        f"Carry over latest step_summary: {self._normalize_plan_text(latest_step_summary, max_len=140)}",
                    )
                ]
            if not progress_summary:
                progress_summary = [
                    self._lang_text(
                        state,
                        "目前只确认了意图与范围，尚未形成稳定证据结论。",
                        "Only intent and scope are confirmed so far, with no stable evidence conclusion yet.",
                    )
                ]

            next_directions: list[dict[str, str]] = []
            raw_next_directions = item.get("next_directions", [])
            if isinstance(raw_next_directions, list):
                for raw_direction in raw_next_directions[:4]:
                    if isinstance(raw_direction, dict):
                        direction_text = self._normalize_plan_text(
                            raw_direction.get("direction", "") or raw_direction.get("task", ""),
                            max_len=140,
                        )
                        if not direction_text:
                            continue
                        next_directions.append(
                            {
                                "priority": self._normalize_priority(raw_direction.get("priority")),
                                "direction": direction_text,
                                "evidence_to_find": self._normalize_plan_text(raw_direction.get("evidence_to_find", ""), max_len=150),
                                "where_to_check": self._normalize_plan_text(
                                    raw_direction.get("where_to_check", "") or raw_direction.get("source_type", ""),
                                    max_len=90,
                                ),
                                "deliverable": self._normalize_plan_text(raw_direction.get("deliverable", ""), max_len=120),
                            }
                        )
                    elif isinstance(raw_direction, str) and raw_direction.strip():
                        next_directions.append(
                            {
                                "priority": "P1",
                                "direction": self._normalize_plan_text(raw_direction, max_len=140),
                                "evidence_to_find": "",
                                "where_to_check": "",
                                "deliverable": "",
                            }
                        )
            next_focus = self._normalize_plan_text(
                item.get("next_focus", "") or item.get("next_direction", ""),
                max_len=360,
            )
            if next_focus and not next_directions:
                next_directions = [
                    {
                        "priority": "P0",
                        "direction": next_focus,
                        "evidence_to_find": "",
                        "where_to_check": self._lang_text(
                            state,
                            "优先普通但可靠、可读性强的网站。",
                            "Start with ordinary reliable and readable websites.",
                        ),
                        "deliverable": "",
                    }
                ]

            intent = self._normalize_plan_text(
                item.get("intent", "") or item.get("objective", ""),
                max_len=220,
            ) or self._lang_text(state, "补充关键证据并压缩不确定性", "Gather key evidence and reduce uncertainty")
            method = self._normalize_plan_text(item.get("method", ""), max_len=240) or self._lang_text(
                state,
                "检索高质量来源、阅读正文、提取证据并核对反例。",
                "Search trusted sources, read full text, extract evidence, and check counterexamples.",
            )
            outputs = self._normalize_plan_text(
                item.get("outputs", "") or item.get("deliverable", ""),
                max_len=240,
            ) or self._lang_text(
                state,
                "本轮结构化产出：结论-证据-引用-不确定性。",
                "Structured step memo: claims, evidence, citations, and uncertainties.",
            )
            acceptance = self._normalize_plan_text(
                item.get("acceptance", "") or item.get("expected_evidence", ""),
                max_len=240,
            ) or self._lang_text(
                state,
                "输出可被核验，且关键结论至少对应两类来源证据。",
                "Output is verifiable and key claims are supported by at least two source types.",
            )
            fallback = self._normalize_plan_text(item.get("fallback", ""), max_len=220) or self._lang_text(
                state,
                "若证据不足，扩大来源类型并显式标注不确定性。",
                "If evidence is weak, broaden source types and make uncertainty explicit.",
            )

            payload_raw = item.get("payload", {})
            payload_queries: list[str] = []
            source_priority: list[str] = []
            payload_notes = ""
            if isinstance(payload_raw, dict):
                raw_queries = payload_raw.get("queries", [])
                if isinstance(raw_queries, list):
                    payload_queries = [
                        self._normalize_plan_text(q, max_len=180)
                        for q in raw_queries
                        if self._normalize_plan_text(q, max_len=180)
                    ]
                elif isinstance(raw_queries, str) and raw_queries.strip():
                    payload_queries = [self._normalize_plan_text(raw_queries, max_len=180)]

                raw_source_priority = payload_raw.get("source_priority", [])
                if isinstance(raw_source_priority, list):
                    source_priority = [
                        self._normalize_plan_text(s, max_len=90)
                        for s in raw_source_priority
                        if self._normalize_plan_text(s, max_len=90)
                    ]
                elif isinstance(raw_source_priority, str) and raw_source_priority.strip():
                    source_priority = [self._normalize_plan_text(raw_source_priority, max_len=90)]

                payload_notes = self._normalize_plan_text(payload_raw.get("notes", ""), max_len=260)
            elif isinstance(payload_raw, str) and payload_raw.strip():
                payload_notes = self._normalize_plan_text(payload_raw, max_len=260)

            query = self._normalize_plan_text(
                item.get("query", "")
                or (payload_queries[0] if payload_queries else "")
                or state["goal"],
                max_len=220,
            )
            if not source_priority:
                source_priority = []

            if not next_directions:
                next_directions = [
                    {
                        "priority": "P0",
                        "direction": intent,
                        "evidence_to_find": self._lang_text(
                            state,
                            "补齐最关键的事实证据与反例",
                            "Fill the most critical factual evidence and counterexamples",
                        ),
                        "where_to_check": self._lang_text(
                            state,
                            "先查普通但可靠的网站与案例，再按需补官方口径。",
                            "Start with ordinary reliable websites and cases, then add official references if needed.",
                        ),
                        "deliverable": outputs
                        or self._lang_text(state, "阶段结论与证据清单", "interim conclusions and evidence list"),
                    }
                ]

            expected_evidence = self._normalize_plan_text(
                "；".join(direction.get("evidence_to_find", "") for direction in next_directions if direction.get("evidence_to_find")),
                max_len=240,
            ) or acceptance
            deliverable = self._normalize_plan_text(
                "；".join(direction.get("deliverable", "") for direction in next_directions if direction.get("deliverable")),
                max_len=240,
            ) or outputs
            objective = intent
            rationale = self._normalize_plan_text(item.get("rationale", ""), max_len=220) or self._lang_text(
                state,
                f"围绕“{objective}”补齐当前证据缺口。",
                f"Clarify unresolved uncertainty around: {objective}",
            )
            narrative_plan = self._build_plan_narrative(state, progress_summary, next_directions)

            round_think = self._normalize_plan_text(
                narrative_plan or item.get("round_think", "") or item.get("plan_summary", "") or objective,
                max_len=320,
            ) or objective
            deliverables = self._normalize_plan_list(
                item.get("deliverables", []) or item.get("outputs", []) or [deliverable or outputs],
                max_items=2,
                max_len=140,
            )
            key_questions = self._normalize_plan_list(
                item.get("key_questions", []) or item.get("questions", []),
                max_items=3,
                max_len=160,
            )
            if not key_questions:
                key_questions = self._normalize_plan_list(
                    [item.get("evidence_to_find", "")] + [direction.get("evidence_to_find", "") for direction in next_directions],
                    max_items=3,
                    max_len=160,
                )
            actions = self._normalize_plan_list(
                item.get("actions", []) or item.get("action_steps", []) or [direction.get("direction", "") for direction in next_directions] or [method],
                max_items=5,
                max_len=180,
            )
            source_directions = self._normalize_plan_list(
                item.get("source_directions", []) or item.get("sources", []) or [direction.get("where_to_check", "") for direction in next_directions] or source_priority,
                max_items=4,
                max_len=180,
            )
            steering_options = self._normalize_plan_list(
                item.get("steering_options", []) or item.get("steering", []),
                max_items=4,
                max_len=180,
            )
            if not steering_options:
                steering_options = self._lang_text(
                    state,
                    [
                        "选项A：先扩大样本范围（城乡/代际）再收敛分类。",
                        "选项B：先锁定2-3类核心情感并深挖需求场景。",
                        "输出偏好：你更想要表格、叙述还是对比矩阵？",
                    ],
                    [
                        "Option A: broaden sample scope first, then refine categories.",
                        "Option B: lock 2-3 core emotion categories first, then deepen scenarios.",
                        "Output preference: table, narrative, or comparison matrix?",
                    ],
                )

            signature = self._normalize_step_signature(title, query)
            if signature in seen_signatures or signature in completed_signatures:
                continue
            seen_signatures.add(signature)

            clean_steps.append(
                {
                    "title": title,
                    "query": query,
                    "objective": objective,
                    "rationale": rationale,
                    "method": method,
                    "expected_evidence": expected_evidence,
                    "deliverable": deliverable,
                    "intent": intent,
                    "outputs": outputs,
                    "acceptance": acceptance,
                    "fallback": fallback,
                    "narrative_plan": narrative_plan,
                    "round_think": round_think,
                    "progress_summary": progress_summary,
                    "next_directions": next_directions[:4],
                    "deliverables": deliverables,
                    "key_questions": key_questions,
                    "actions": actions,
                    "source_directions": source_directions,
                    "steering_options": steering_options,
                    "payload": {
                        "queries": payload_queries or [query],
                        "source_priority": source_priority,
                        "notes": payload_notes,
                    },
                }
            )

        if len(clean_steps) < 1:
            return self._fallback_plan(state, remaining_steps=remaining_steps), "planner fallback (insufficient valid steps)"

        return clean_steps[:target_steps], "planner generated via llm"

    def _fallback_plan(self, state: dict[str, Any], remaining_steps: int) -> list[dict[str, Any]]:
        goal = state["goal"]
        completed_count = len(state.get("completed_steps", [])) if isinstance(state.get("completed_steps", []), list) else 0
        preferred = ", ".join(state["required_sources"]) if state["required_sources"] else self._lang_text(state, "高权威来源", "high-authority sources")
        constraints = "；".join(state["constraints"]) if state["constraints"] else self._lang_text(state, "无额外约束", "no extra constraints")

        if self._is_zh(state):
            templates = [
                {
                    "title": "问题边界与分类框架",
                    "query": f"{goal} 定义 分类框架 评估维度",
                    "objective": "先定义研究对象、场景和评价维度，避免后续结论漂移。",
                    "rationale": "没有统一口径就无法比较不同来源的数据和案例。",
                    "method": "优先查找官方定义、行业框架、机构指南和产品口径说明。",
                    "expected_evidence": "政策文件、行业协会说明、机构框架文档、官方术语定义。",
                    "deliverable": "清晰的研究边界与维度表。",
                },
                {
                    "title": "高质量来源检索",
                    "query": f"{goal} 最新 数据 报告 行业案例 官方披露 {preferred}",
                    "objective": "围绕核心维度收集高质量且多样化的来源，避免只依赖论文。",
                    "rationale": "来源质量决定后续结论可信度。",
                    "method": "多查询检索、来源去重、跨域名平衡（政策/行业/企业/媒体）。",
                    "expected_evidence": "监管与政策页面、行业报告、企业公开披露、产品文档、可信媒体。",
                    "deliverable": "可追溯来源清单与质量标注。",
                },
                {
                    "title": "证据抽取与反例核验",
                    "query": f"{goal} 关键结论 证据 反例 约束 {constraints}",
                    "objective": "提取结论-证据链并验证反例与冲突观点。",
                    "rationale": "避免单向叙述，提升结论稳健性。",
                    "method": "结构化笔记（claim-evidence-citations）+ 冲突比对。",
                    "expected_evidence": "可核验原始数据、案例细节、口径差异与边界条件。",
                    "deliverable": "证据矩阵与冲突点说明。",
                },
                {
                    "title": "综合写作与不确定性标注",
                    "query": f"{goal} 汇总 不确定性 下一步",
                    "objective": "形成结构化结论，明确不确定性和下一步建议。",
                    "rationale": "最终报告需要可执行建议与风险边界。",
                    "method": "先提纲后写作，段落绑定证据引用。",
                    "expected_evidence": "跨来源一致点/分歧点与置信边界。",
                    "deliverable": "最终研究报告草稿。",
                },
            ]
        else:
            templates = [
                {
                    "title": "Scope and framing",
                    "query": f"{goal} definitions taxonomy evaluation dimensions",
                    "objective": "Define scope, target population, and evaluation axes for comparable evidence.",
                    "rationale": "A stable frame avoids vague or inconsistent conclusions.",
                    "method": "Collect policy, industry, institutional, and official product definitions.",
                    "expected_evidence": "Policy pages, industry guidance, institutional frameworks, official terminology.",
                    "deliverable": "A clear scope and evaluation matrix.",
                },
                {
                    "title": "High-signal retrieval",
                    "query": f"{goal} latest data reports case studies official disclosures {preferred}",
                    "objective": "Collect high-quality, diverse sources for each key dimension, not papers only.",
                    "rationale": "Source quality drives final report reliability.",
                    "method": "Run multi-query retrieval, deduplicate, and balance policy/industry/company/media evidence.",
                    "expected_evidence": "Regulatory pages, industry reports, company disclosures, product docs, credible media.",
                    "deliverable": "Curated source set with quality notes.",
                },
                {
                    "title": "Evidence extraction and contradiction check",
                    "query": f"{goal} claims evidence counterexamples constraints {constraints}",
                    "objective": "Extract claims/evidence and validate contradictory findings.",
                    "rationale": "Decision-quality output requires tradeoffs and counterarguments.",
                    "method": "Claim-evidence-citation notes plus contradiction checks.",
                    "expected_evidence": "Verifiable findings, methodological caveats, and disagreement points.",
                    "deliverable": "Evidence matrix with contradiction analysis.",
                },
                {
                    "title": "Synthesis and uncertainty labeling",
                    "query": f"{goal} synthesis uncertainty next actions",
                    "objective": "Produce an evidence-grounded synthesis with uncertainties and actions.",
                    "rationale": "Final output should include confidence boundaries and practical next steps.",
                    "method": "Outline-first writing with citation-bound paragraphs.",
                    "expected_evidence": "Cross-source convergence/divergence and residual unknowns.",
                    "deliverable": "Final structured report draft.",
                },
            ]

        index = min(completed_count, len(templates) - 1)
        selected = dict(templates[index])
        selected.setdefault("intent", str(selected.get("objective", "")).strip())
        selected.setdefault("outputs", str(selected.get("deliverable", "")).strip())
        selected.setdefault("acceptance", str(selected.get("expected_evidence", "")).strip())
        selected.setdefault(
            "fallback",
            self._lang_text(
                state,
                "若证据不足则扩大来源类型并明确不确定性。",
                "If evidence is weak, broaden source types and make uncertainty explicit.",
            ),
        )
        payload = selected.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        payload.setdefault("queries", [str(selected.get("query", goal)).strip()])
        payload.setdefault("source_priority", [])
        payload.setdefault("notes", "")
        selected["payload"] = payload
        latest_completed = self._latest_completed_step(state)
        latest_step_summary = str(latest_completed.get("step_summary", "")).strip() if isinstance(latest_completed, dict) else ""
        progress_summary = (
            [self._normalize_plan_text(latest_step_summary, max_len=170)]
            if latest_step_summary
            else [
                self._lang_text(
                    state,
                    "目前只确认了研究意图与范围，正在补齐第一轮可验证证据。",
                    "Only intent and scope are confirmed so far; first-round verifiable evidence is being collected.",
                )
            ]
        )
        next_directions = [
            {
                "priority": "P0",
                "direction": self._normalize_plan_text(selected.get("objective", ""), max_len=140),
                "evidence_to_find": self._normalize_plan_text(selected.get("expected_evidence", ""), max_len=150),
                "where_to_check": self._lang_text(
                    state,
                    "优先普通可靠网站与案例文章，必要时补官方口径。",
                    "Prioritize ordinary reliable websites and case writeups, then add official sources only when needed.",
                ),
                "deliverable": self._normalize_plan_text(selected.get("deliverable", ""), max_len=120),
            },
            {
                "priority": "P1",
                "direction": self._lang_text(
                    state,
                    "补充行业/案例侧证据以校正可落地性。",
                    "Add industry/case evidence to validate practical feasibility.",
                ),
                "evidence_to_find": self._lang_text(
                    state,
                    "可执行案例、关键约束与反例",
                    "actionable cases, key constraints, and counterexamples",
                ),
                "where_to_check": self._lang_text(
                    state,
                    "行业网站、用户经验文章与可信媒体深度稿。",
                    "Industry sites, user-experience writeups, and credible long-form media.",
                ),
                "deliverable": self._lang_text(state, "阶段结论补充说明", "supplementary interim notes"),
            },
        ]
        selected["progress_summary"] = progress_summary
        selected["next_directions"] = next_directions
        selected["narrative_plan"] = self._build_plan_narrative(state, progress_summary, next_directions)
        selected["round_think"] = str(selected["narrative_plan"]).strip()
        selected["deliverables"] = self._normalize_plan_list([selected.get("deliverable", "")], max_items=2, max_len=160)
        selected["key_questions"] = self._lang_text(
            state,
            [
                "本轮最需要先确认的定义和边界是什么？",
                "哪些高质量来源能直接支持核心结论？",
                "目前结论最大的证据缺口在哪里？",
            ],
            [
                "Which definition and boundary must be fixed first this round?",
                "Which high-quality sources can directly support core claims?",
                "What is the largest remaining evidence gap right now?",
            ],
        )
        selected["actions"] = self._normalize_plan_list([selected.get("method", "")], max_items=5, max_len=180)
        selected["source_directions"] = self._normalize_plan_list(
            [
                self._lang_text(state, "官方/机构发布与政策资料", "Official and institutional releases"),
                self._lang_text(state, "行业报告与产品/企业公开披露", "Industry reports and company/product disclosures"),
                self._lang_text(state, "学术/综述用于关键论断核验", "Academic/review sources for critical claim validation"),
            ],
            max_items=4,
            max_len=180,
        )
        selected["steering_options"] = self._lang_text(
            state,
            [
                "选项A：先扩展覆盖面（更多人群/区域）再收敛结论。",
                "选项B：先聚焦关键问题（如孤独、寄托）做深挖。",
                "输出偏好：你更想要表格、叙述还是对比矩阵？",
            ],
            [
                "Option A: broaden population/region coverage first, then converge.",
                "Option B: focus deeply on key problems first (e.g., loneliness, support).",
                "Output preference: table, narrative, or comparison matrix?",
            ],
        )
        return [selected]

    def _is_plan_noise_line(self, value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        return any(pattern.search(text) for pattern in PLAN_NOISE_PATTERNS)

    def _normalize_plan_text(self, value: Any, max_len: int = 220) -> str:
        lines = str(value or "").splitlines()
        cleaned: list[str] = []
        for line in lines:
            line_text = str(line).strip()
            if not line_text:
                continue
            if self._is_plan_noise_line(line_text):
                continue
            cleaned.append(line_text)
        merged = " ".join(cleaned)
        merged = re.sub(r"\s+", " ", merged).strip()
        merged = re.sub(r"(【掌握的信息】|【下一步要查什么】|what we know|what to check next)", "", merged, flags=re.IGNORECASE)
        merged = re.sub(r"\s+", " ", merged).strip()
        if max_len > 0 and len(merged) > max_len:
            return f"{merged[:max_len].rstrip()}..."
        return merged

    def _normalize_plan_list(self, value: Any, max_items: int = 5, max_len: int = 180) -> list[str]:
        items: list[str] = []
        if isinstance(value, list):
            raw_items = value
        elif isinstance(value, tuple):
            raw_items = list(value)
        else:
            text = self._normalize_plan_text(value, max_len=max_len)
            raw_items = [text] if text else []

        for raw in raw_items:
            cleaned = self._normalize_plan_text(raw, max_len=max_len)
            if not cleaned:
                continue
            items.append(cleaned)
            if len(items) >= max_items:
                break
        return items

    def _normalize_score_0_3(self, value: Any, default: int = 1) -> int:
        try:
            number = int(float(value))
        except Exception:
            return max(0, min(3, int(default)))
        return max(0, min(3, number))

    def _normalize_step_signature(self, title: str, query: str) -> str:
        raw = f"{title}::{query}".lower().strip()
        compact = " ".join(raw.replace("\n", " ").split())
        return compact[:280]

    def _latest_completed_step(self, state: dict[str, Any]) -> dict[str, Any] | None:
        completed_steps = state.get("completed_steps", [])
        if not isinstance(completed_steps, list) or not completed_steps:
            return None
        latest = completed_steps[-1]
        if not isinstance(latest, dict):
            return None
        return latest

    def _research_rhythm_stage(self, state: dict[str, Any]) -> str:
        completed = state.get("completed_steps", [])
        completed_count = len(completed) if isinstance(completed, list) else 0
        if completed_count <= 0:
            return self._lang_text(state, "框架", "framework")
        if completed_count == 1:
            return self._lang_text(state, "现实证据", "real-world evidence")
        if completed_count == 2:
            return self._lang_text(state, "边界与反例", "boundaries and counterexamples")
        return self._lang_text(state, "凝练交付物", "deliverable consolidation")

    def _normalize_priority(self, value: Any) -> str:
        text = str(value or "").strip().upper()
        if text in {"P0", "P1"}:
            return text
        return "P1"

    def _build_plan_narrative(
        self,
        state: dict[str, Any],
        progress_summary: list[str],
        next_directions: list[dict[str, str]],
    ) -> str:
        progress_items = [self._normalize_plan_text(item, max_len=180) for item in progress_summary]
        progress_items = [item for item in progress_items if item][:6]
        known_text = "；".join(progress_items[:4]) if progress_items else self._lang_text(
            state,
            "目前还没有可靠材料，缺口是缺少可交叉验证的事实证据和可复用案例",
            "Reliable material is still insufficient; the gap is verifiable facts and reusable real-world cases",
        )

        direction_lines: list[str] = []
        for direction in next_directions[:4]:
            direction_name = self._normalize_plan_text(direction.get("direction", ""), max_len=120)
            if not direction_name:
                continue
            evidence_to_find = self._normalize_plan_text(direction.get("evidence_to_find", ""), max_len=130)
            where_to_check = self._normalize_plan_text(direction.get("where_to_check", ""), max_len=90)
            deliverable = self._normalize_plan_text(direction.get("deliverable", ""), max_len=110)
            line = self._lang_text(
                state,
                f"{direction_name}；重点补 {evidence_to_find or '关键事实与反例'}；优先看 {where_to_check or '普通可靠网站'}，并产出 {deliverable or '可直接阅读的结论要点'}。",
                f"{direction_name}; fill {evidence_to_find or 'critical facts and counterexamples'}; check {where_to_check or 'ordinary reliable websites'} first and deliver {deliverable or 'reader-ready takeaways'}.",
            )
            direction_lines.append(line)

        next_text = " ".join(direction_lines[:2]) if direction_lines else self._lang_text(
            state,
            "下一步先补齐最关键证据并收敛争议点，优先读取普通但可靠、可读性强的网站内容，再补官方边界口径",
            "Next, close the key evidence gaps and disputed points using ordinary reliable readable web sources first, then add official boundary references",
        )

        return self._lang_text(
            state,
            f"目前已确认：{known_text}。接下来将重点推进：{next_text}。",
            f"Confirmed so far: {known_text}. Next focus: {next_text}.",
        )

    async def _researcher(self, ctx: RunContext) -> None:
        step_number = int(ctx.state["next_step_index"])

        if ctx.state["flags"].get("interrupt_requested"):
            await self._emit(
                run_id=ctx.run_id,
                step=step_number,
                node="interruption_gateway",
                event_type="research_paused_for_steer",
                payload={
                    "reason": "interrupt_flag_active_before_step",
                    "next_step_index": step_number,
                },
            )
            return

        if step_number >= int(ctx.state["max_steps"]):
            await self._emit(
                run_id=ctx.run_id,
                step=step_number,
                node="researcher",
                event_type="step_skipped",
                payload={"reason": "max steps reached"},
            )
            return

        next_step = next((item for item in ctx.state["plan"] if item["status"] == "pending"), None)
        if next_step is None:
            await self._emit(
                run_id=ctx.run_id,
                step=step_number,
                node="researcher",
                event_type="step_skipped",
                payload={"reason": "no pending step"},
            )
            return

        next_step["status"] = "running"

        await self._emit(
            run_id=ctx.run_id,
            step=step_number,
            node="researcher",
            event_type="step_started",
            payload={
                "step_id": next_step["id"],
                "title": next_step["title"],
                "query": next_step["query"],
                "objective": next_step["objective"],
            },
        )

        react_thought = await self._build_react_thought(ctx.state, next_step)
        await self._emit(
            run_id=ctx.run_id,
            step=step_number,
            node="researcher",
            event_type="researcher_react_thought",
            payload={
                "step_id": next_step["id"],
                "thought": react_thought,
            },
        )

        await self._emit(
            run_id=ctx.run_id,
            step=step_number,
            node="researcher",
            event_type="researcher_react_action",
            payload={
                "step_id": next_step["id"],
                "action": "query_expansion",
            },
        )

        interrupted, expanded_queries = await self._await_interruptible(
            ctx=ctx,
            step=step_number,
            stage="query_expansion",
            work=self._expand_queries(ctx.state, next_step),
            timeout_seconds=STAGE_TIMEOUT_SECONDS.get("query_expansion"),
            timeout_fallback=[str(next_step.get("query", "")).strip() or str(ctx.state.get("goal", "")).strip()],
        )
        if interrupted:
            await self._pause_current_step(ctx, step_number, next_step, stage="query_expansion")
            return
        if not isinstance(expanded_queries, list):
            expanded_queries = []
        expanded_queries = [str(item).strip() for item in expanded_queries if str(item).strip()]
        if not expanded_queries:
            expanded_queries = [str(next_step.get("query", "")).strip() or str(ctx.state.get("goal", "")).strip()]

        await self._emit(
            run_id=ctx.run_id,
            step=step_number,
            node="researcher",
            event_type="researcher_query_expansion",
            payload={
                "step_id": next_step["id"],
                "queries": expanded_queries,
            },
        )

        await self._emit(
            run_id=ctx.run_id,
            step=step_number,
            node="researcher",
            event_type="researcher_react_action",
            payload={
                "step_id": next_step["id"],
                "action": "search_web",
            },
        )

        interrupted, search_results = await self._await_interruptible(
            ctx=ctx,
            step=step_number,
            stage="search_web",
            work=self.research_toolkit.search_many(
                queries=expanded_queries,
                required_domains=ctx.state["required_sources"],
                blocked_domains=ctx.state["blocked_sources"],
                max_results_per_query=self.settings.search_max_results_per_query,
                max_total_results=self.settings.max_sources_per_step,
            ),
            timeout_seconds=STAGE_TIMEOUT_SECONDS.get("search_web"),
            timeout_fallback=[],
        )
        if interrupted:
            await self._pause_current_step(ctx, step_number, next_step, stage="search_web")
            return
        if not isinstance(search_results, list):
            search_results = []

        await self._emit(
            run_id=ctx.run_id,
            step=step_number,
            node="researcher",
            event_type="researcher_search_results",
            payload={
                "step_id": next_step["id"],
                "count": len(search_results),
                "results": [
                    {
                        "provider": item.provider,
                        "title": item.title,
                        "url": item.url,
                        "domain": item.domain,
                        "snippet": item.snippet[:260],
                        "quality_score": round(item.quality_score, 3),
                        "rerank_score": round(item.rerank_score, 4),
                    }
                    for item in search_results[:8]
                ],
            },
        )

        await self._emit(
            run_id=ctx.run_id,
            step=step_number,
            node="researcher",
            event_type="researcher_react_observation",
            payload={
                "step_id": next_step["id"],
                "observation": f"search returned {len(search_results)} candidate sources",
            },
        )

        await self._emit(
            run_id=ctx.run_id,
            step=step_number,
            node="researcher",
            event_type="researcher_react_action",
            payload={
                "step_id": next_step["id"],
                "action": "read_selected_sources",
            },
        )

        interrupted, source_docs = await self._await_interruptible(
            ctx=ctx,
            step=step_number,
            stage="read_sources",
            work=self.research_toolkit.read_sources(
                items=search_results,
                max_read=max(len(search_results), int(self.settings.max_read_sources_per_step)),
            ),
            timeout_seconds=STAGE_TIMEOUT_SECONDS.get("read_sources"),
            timeout_fallback=[],
        )
        if interrupted:
            await self._pause_current_step(ctx, step_number, next_step, stage="read_sources")
            return
        if not isinstance(source_docs, list):
            source_docs = []

        await self._emit(
            run_id=ctx.run_id,
            step=step_number,
            node="researcher",
            event_type="researcher_read_results",
            payload={
                "step_id": next_step["id"],
                "count": len(source_docs),
                "sources": [
                    {
                        "title": doc.title,
                        "url": doc.url,
                        "domain": doc.domain,
                        "quality_score": round(doc.quality_score, 3),
                    }
                    for doc in source_docs
                ],
            },
        )

        await self._emit(
            run_id=ctx.run_id,
            step=step_number,
            node="researcher",
            event_type="researcher_react_observation",
            payload={
                "step_id": next_step["id"],
                "observation": f"read {len(source_docs)} documents",
            },
        )

        await self._emit(
            run_id=ctx.run_id,
            step=step_number,
            node="researcher",
            event_type="researcher_react_action",
            payload={
                "step_id": next_step["id"],
                "action": "synthesize_notes",
            },
        )

        interrupted, notes = await self._await_interruptible(
            ctx=ctx,
            step=step_number,
            stage="structured_notes",
            work=self._build_structured_notes(ctx.state, next_step, search_results, source_docs),
            timeout_seconds=STAGE_TIMEOUT_SECONDS.get("structured_notes"),
            timeout_fallback=[],
        )
        if interrupted:
            await self._pause_current_step(ctx, step_number, next_step, stage="structured_notes")
            return
        if not isinstance(notes, list):
            notes = []

        await self._emit(
            run_id=ctx.run_id,
            step=step_number,
            node="researcher",
            event_type="researcher_structured_notes",
            payload={
                "step_id": next_step["id"],
                "notes": notes,
            },
        )

        interrupted, outline = await self._await_interruptible(
            ctx=ctx,
            step=step_number,
            stage="outline",
            work=self._build_outline(ctx.state, next_step, notes),
            timeout_seconds=STAGE_TIMEOUT_SECONDS.get("outline"),
            timeout_fallback=self._lang_text(
                ctx.state,
                ["核心结论", "证据线索", "边界与下一步"],
                ["Core takeaways", "Evidence lines", "Boundaries and next step"],
            ),
        )
        if interrupted:
            await self._pause_current_step(ctx, step_number, next_step, stage="outline")
            return
        if not isinstance(outline, list):
            outline = self._lang_text(
                ctx.state,
                ["核心结论", "证据线索", "边界与下一步"],
                ["Core takeaways", "Evidence lines", "Boundaries and next step"],
            )
        ctx.state["outline"] = outline
        await self._emit(
            run_id=ctx.run_id,
            step=step_number,
            node="researcher",
            event_type="researcher_outline",
            payload={
                "step_id": next_step["id"],
                "outline": outline,
            },
        )

        interrupted, synthesis = await self._await_interruptible(
            ctx=ctx,
            step=step_number,
            stage="write_synthesis",
            work=self._write_synthesis(ctx.state, next_step, outline, notes),
            timeout_seconds=STAGE_TIMEOUT_SECONDS.get("write_synthesis"),
            timeout_fallback={
                "summary": self._lang_text(
                    ctx.state,
                    f"写作阶段超时，先输出“{next_step['title']}”的阶段性结论。",
                    f"Writing timed out; returning an interim summary for '{next_step['title']}'.",
                ),
                "report": self._lang_text(
                    ctx.state,
                    f"研究步骤：{next_step['title']}\n本轮已收集 {len(notes)} 条证据，先给出阶段摘要，后续轮次继续补充。",
                    f"Research step: {next_step['title']}\nCollected {len(notes)} evidence items in this round. Interim synthesis returned; later rounds will refine.",
                ),
                "paragraphs": [],
            },
        )
        if interrupted:
            await self._pause_current_step(ctx, step_number, next_step, stage="write_synthesis")
            return
        if not isinstance(synthesis, dict):
            synthesis = {
                "summary": self._lang_text(
                    ctx.state,
                    "本轮已收集证据，正在继续形成阶段结论。",
                    "Evidence collected in this round; interim synthesis is being consolidated.",
                ),
                "report": "",
                "paragraphs": [],
            }

        interrupted, self_check = await self._await_interruptible(
            ctx=ctx,
            step=step_number,
            stage="self_check",
            work=self._self_check(ctx.state, next_step, synthesis, notes),
            timeout_seconds=STAGE_TIMEOUT_SECONDS.get("self_check"),
            timeout_fallback=self._lang_text(
                ctx.state,
                {
                    "missing_points": ["本轮未完成完整自检，待后续补充。"],
                    "counterexamples": ["反例核查尚不完整，需下一轮继续。"],
                    "uncertainties": ["存在阶段性不确定性：本轮自检超时。"],
                },
                {
                    "missing_points": ["Self-check did not fully complete in this round."],
                    "counterexamples": ["Counterexample coverage is incomplete and needs follow-up."],
                    "uncertainties": ["Interim uncertainty remains because self-check timed out."],
                },
            ),
        )
        if interrupted:
            await self._pause_current_step(ctx, step_number, next_step, stage="self_check")
            return
        if not isinstance(self_check, dict):
            self_check = self._lang_text(
                ctx.state,
                {
                    "missing_points": ["本轮自检结果不完整，需后续补齐。"],
                    "counterexamples": ["反例核查仍需扩展。"],
                    "uncertainties": ["存在阶段性不确定性，下一轮继续补证。"],
                },
                {
                    "missing_points": ["Self-check output is incomplete in this round."],
                    "counterexamples": ["Counterexample checks need expansion."],
                    "uncertainties": ["Interim uncertainty remains and needs more evidence."],
                },
            )

        interrupted, step_summary = await self._await_interruptible(
            ctx=ctx,
            step=step_number,
            stage="step_summary",
            work=self._build_step_summary(ctx.state, next_step, notes, synthesis, self_check),
            timeout_seconds=STAGE_TIMEOUT_SECONDS.get("step_summary"),
            timeout_fallback=self._lang_text(
                ctx.state,
                (
                    "What we did\n"
                    f"- 完成步骤：{next_step.get('title', '')}\n"
                    f"- 收集证据：{len(notes)} 条\n\n"
                    "Key findings\n"
                    f"- {synthesis.get('summary', '本轮先形成阶段结论，后续继续补证。')}\n\n"
                    "Implications for MVP / decision\n"
                    "- 当前方向可继续推进，但需要补齐反例和边界。\n\n"
                    "Open questions & next step\n"
                    "- 下一轮优先补充关键争议点的交叉证据。"
                ),
                (
                    "What we did\n"
                    f"- Completed step: {next_step.get('title', '')}\n"
                    f"- Evidence items: {len(notes)}\n\n"
                    "Key findings\n"
                    f"- {synthesis.get('summary', 'Interim conclusion generated; more evidence needed.')}\n\n"
                    "Implications for MVP / decision\n"
                    "- Direction can continue but needs stronger boundary checks.\n\n"
                    "Open questions & next step\n"
                    "- Next round should close key controversy gaps with cross-verified evidence."
                ),
            ),
        )
        if interrupted:
            await self._pause_current_step(ctx, step_number, next_step, stage="step_summary")
            return
        if not isinstance(step_summary, str):
            step_summary = ""

        final_text = synthesis.get("report", "")
        for token in final_text.split():
            if ctx.interrupt_event.is_set():
                await self._pause_current_step(ctx, step_number, next_step, stage="stream_tokens")
                return
            await self._emit(
                run_id=ctx.run_id,
                step=step_number,
                node="researcher",
                event_type="token",
                payload={
                    "role": "researcher",
                    "step_id": next_step["id"],
                    "text": f"{token} ",
                },
            )
            await asyncio.sleep(TOKEN_DELAY_SECONDS)

        await self._emit(
            run_id=ctx.run_id,
            step=step_number,
            node="researcher",
            event_type="researcher_self_check",
            payload={
                "step_id": next_step["id"],
                "self_check": self_check,
            },
        )

        await self._emit(
            run_id=ctx.run_id,
            step=step_number,
            node="reporter",
            event_type="step_summary",
            payload={
                "step_id": next_step["id"],
                "summary": step_summary,
            },
        )

        output_payload = {
            "step_id": next_step["id"],
            "title": next_step["title"],
            "objective": next_step["objective"],
            "queries": expanded_queries,
            "top_sources": [
                {
                    "title": item.title,
                    "url": item.url,
                    "domain": item.domain,
                    "snippet": item.snippet,
                    "quality_score": round(item.quality_score, 3),
                    "rerank_score": round(item.rerank_score, 4),
                }
                for item in search_results[:6]
            ],
            "notes": notes,
            "outline": outline,
            "synthesis": synthesis,
            "self_check": self_check,
            "step_summary": step_summary,
            "timestamp": utc_now_iso(),
        }

        next_step["status"] = "completed"
        next_step["result"] = output_payload
        ctx.state["completed_steps"].append(output_payload)
        ctx.state["research_memory"].extend(notes)
        ctx.state["next_step_index"] = len(ctx.state["completed_steps"])
        ctx.state["flags"]["auto_replan_attempts"] = 0
        if ctx.state["next_step_index"] < int(ctx.state["max_steps"]):
            ctx.state["flags"]["replan"] = True

        await self._emit(
            run_id=ctx.run_id,
            step=ctx.state["next_step_index"],
            node="researcher",
            event_type="researcher_output",
            payload={
                "step_id": next_step["id"],
                "summary": synthesis.get("summary", ""),
                "report": synthesis.get("report", ""),
                "step_summary": step_summary,
                "source_count": len(output_payload["top_sources"]),
                "note_count": len(notes),
            },
        )

        await self._emit(
            run_id=ctx.run_id,
            step=ctx.state["next_step_index"],
            node="researcher",
            event_type="step_completed",
            payload={
                "step_id": next_step["id"],
                "title": next_step["title"],
                "status": "completed",
            },
        )

        await self._emit(
            run_id=ctx.run_id,
            step=ctx.state["next_step_index"],
            node="handoff",
            event_type="research_handoff_to_supervisor",
            payload={
                "step_id": next_step["id"],
                "latest_step_title": next_step["title"],
                "latest_summary": synthesis.get("summary", ""),
                "latest_uncertainties": self_check.get("uncertainties", []) if isinstance(self_check, dict) else [],
                "completed_steps": len(ctx.state["completed_steps"]),
            },
        )

    async def _pause_current_step(
        self,
        ctx: RunContext,
        step_number: int,
        step_data: dict[str, Any],
        stage: str,
    ) -> None:
        if step_data.get("status") == "running":
            step_data["status"] = "pending"
        ctx.state["flags"]["interrupt_requested"] = True
        ctx.state["flags"]["replan"] = True

        await self._emit(
            run_id=ctx.run_id,
            step=step_number,
            node="interruption_gateway",
            event_type="research_paused_for_steer",
            payload={
                "step_id": step_data.get("id"),
                "stage": stage,
                "message": self._lang_text(
                    ctx.state,
                    "研究已暂停，等待 supervisor 处理新干预。",
                    "research paused immediately for supervisor review",
                ),
            },
        )

    async def _await_interruptible(
        self,
        ctx: RunContext,
        step: int,
        stage: str,
        work: Any,
        timeout_seconds: float | None = None,
        timeout_fallback: Any = None,
    ) -> tuple[bool, Any]:
        if ctx.interrupt_event.is_set():
            return True, None

        work_task = asyncio.create_task(work)
        interrupt_task = asyncio.create_task(ctx.interrupt_event.wait())
        try:
            done, pending = await asyncio.wait(
                {work_task, interrupt_task},
                timeout=timeout_seconds if timeout_seconds and timeout_seconds > 0 else None,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if interrupt_task in done:
                work_task.cancel()
                await asyncio.gather(work_task, return_exceptions=True)
                return True, None

            if work_task in done:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                return False, work_task.result()

            # Stage timeout: degrade gracefully and continue the loop.
            work_task.cancel()
            await asyncio.gather(work_task, return_exceptions=True)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

            await self._emit(
                run_id=ctx.run_id,
                step=step,
                node="researcher",
                event_type="stage_timeout",
                payload={
                    "stage": stage,
                    "timeout_seconds": timeout_seconds,
                    "fallback_applied": timeout_fallback is not None,
                },
            )

            fallback_value = timeout_fallback() if callable(timeout_fallback) else timeout_fallback
            return False, fallback_value

        finally:
            if not interrupt_task.done():
                interrupt_task.cancel()
                await asyncio.gather(interrupt_task, return_exceptions=True)

    async def _build_react_thought(self, state: dict[str, Any], step: dict[str, Any]) -> str:
        fallback = self._lang_text(
            state,
            f"本轮将围绕“{step['title']}”检索权威来源，提取证据并标注不确定性。",
            f"Execute '{step['title']}' by searching authoritative sources, extracting evidence, and marking uncertainty.",
        )
        if not self.llm.enabled:
            return fallback

        prompt = self._lang_text(
            state,
            (
                "请写一条 researcher 在行动前的简短思考，最多 45 个字。"
                "不要 markdown，必须中文。\n"
                f"Goal: {state['goal']}\n"
                f"Step title: {step['title']}\n"
                f"Step query: {step['query']}\n"
                f"Constraints: {json.dumps(state['constraints'], ensure_ascii=False)}"
            ),
            (
                "Write one concise thought for a ReAct research agent before actions. "
                "No markdown, max 45 words.\n"
                f"Goal: {state['goal']}\n"
                f"Step title: {step['title']}\n"
                f"Step query: {step['query']}\n"
                f"Constraints: {json.dumps(state['constraints'], ensure_ascii=False)}"
            ),
        )
        response = await self.llm.chat(
            model=self.settings.llm_model_researcher,
            system_prompt=self._lang_text(
                state,
                "你是严谨的 ReAct researcher，始终用中文输出。",
                "You are a ReAct researcher.",
            ),
            user_prompt=prompt,
            max_tokens=120,
        )
        if response is None:
            return fallback
        text = response.text.strip()
        return text or fallback

    async def _expand_queries(self, state: dict[str, Any], step: dict[str, Any]) -> list[str]:
        base_query = step["query"]

        if self.llm.enabled:
            prompt = self._lang_text(
                state,
                (
                    "把当前检索扩展为 4 条高质量网页查询。"
                    "输出 ONLY JSON，键名为 queries。查询语句必须中文。"
                    "普通网站优先：先给普通但可靠、可读性强的解释型内容与案例来源。"
                    "官方来源只用于边界定义与关键口径核对；学术只在关键数字/结论必须严谨时再补充。"
                    "除非用户明确要论文，不要让学术关键词占主导。\n"
                    f"Goal: {state['goal']}\n"
                    f"Step query: {base_query}\n"
                    f"Constraints: {json.dumps(state['constraints'], ensure_ascii=False)}\n"
                    f"Preferred sources: {json.dumps(state['required_sources'], ensure_ascii=False)}"
                ),
                (
                    "Expand the query into exactly 4 high-quality web queries. "
                    "Output ONLY JSON with key 'queries'. "
                    "Ordinary reliable websites first: prefer readable explainers and case-oriented content. "
                    "Use official sources for boundary/definition checks, and use academic sources only when critical numbers/claims require rigor. "
                    "Unless user explicitly asks for papers, academic terms must not dominate.\n"
                    f"Goal: {state['goal']}\n"
                    f"Step query: {base_query}\n"
                    f"Constraints: {json.dumps(state['constraints'], ensure_ascii=False)}\n"
                    f"Preferred sources: {json.dumps(state['required_sources'], ensure_ascii=False)}"
                ),
            )
            response = await self.llm.chat(
                model=self.settings.llm_model_researcher,
                system_prompt=self._lang_text(
                    state,
                    "你是查询扩展助手，始终使用中文。",
                    "You are a query expansion assistant.",
                ),
                user_prompt=prompt,
                max_tokens=300,
            )
            if response is not None:
                data = parse_json_object(response.text)
                queries = data.get("queries") if isinstance(data, dict) else None
                if isinstance(queries, list):
                    cleaned = [str(item).strip() for item in queries if str(item).strip()]
                    if cleaned:
                        capped: list[str] = []
                        academic_count = 0
                        for query in cleaned:
                            if is_academic_query(query):
                                if academic_count >= 1:
                                    continue
                                academic_count += 1
                            capped.append(query)
                            if len(capped) >= 4:
                                break

                        if len(capped) < 4:
                            supplements = [
                                base_query,
                                self._lang_text(
                                    state,
                                    f"{state['goal']} 政策 官方指南 行业报告 最新",
                                    f"{state['goal']} policy official guidance industry report latest",
                                ),
                                self._lang_text(
                                    state,
                                    f"{state['goal']} 企业披露 财报 产品文档 案例",
                                    f"{state['goal']} company disclosure filing product docs case study",
                                ),
                                self._lang_text(
                                    state,
                                    f"{state['goal']} 反例 局限 争议 可信媒体",
                                    f"{state['goal']} counterexample limitation disagreement credible media",
                                ),
                            ]
                            for candidate in supplements:
                                value = str(candidate).strip()
                                if not value or value in capped:
                                    continue
                                if is_academic_query(value) and academic_count >= 1:
                                    continue
                                if is_academic_query(value):
                                    academic_count += 1
                                capped.append(value)
                                if len(capped) >= 4:
                                    break

                        return capped[:4]

        fallback = [
            base_query,
            self._lang_text(
                state,
                f"{state['goal']} 通俗解读 常见问题 真实案例",
                f"{state['goal']} plain-language guide common issues real cases",
            ),
            self._lang_text(
                state,
                f"{state['goal']} 政策 官方指南 行业报告 最新",
                f"{state['goal']} policy official guidance industry report latest",
            ),
            self._lang_text(
                state,
                f"{state['goal']} 反例 局限 争议 可信媒体",
                f"{state['goal']} counterexample limitation disagreement credible media",
            ),
        ]

        if state["required_sources"]:
            fallback.append(f"{base_query} site:{state['required_sources'][0]}")

        return fallback[:4]

    async def _build_structured_notes(
        self,
        state: dict[str, Any],
        step: dict[str, Any],
        search_results: list[SearchResult],
        source_docs: list[SourceDocument],
    ) -> list[dict[str, Any]]:
        source_preview = [
            {
                "title": doc.title,
                "url": doc.url,
                "domain": doc.domain,
                "excerpt": doc.excerpt[:350],
            }
            for doc in source_docs[:4]
        ]
        if not source_preview:
            source_preview = [
                {
                    "title": item.title,
                    "url": item.url,
                    "domain": item.domain,
                    "excerpt": item.snippet[:350],
                }
                for item in search_results[:4]
            ]

        min_evidence_count = 5
        allowed_source_types = {"academic", "industry", "news", "blog", "product_case", "interview", "official"}
        intent_text = f"{state.get('goal', '')} {' '.join(state.get('constraints', []))}".lower()
        academic_requested = is_academic_query(intent_text)
        product_intent = any(
            token in intent_text
            for token in (
                "mvp",
                "产品",
                "落地",
                "交互",
                "方案",
                "prototype",
                "ux",
                "ui",
                "go-to-market",
            )
        )
        blocked_marketing_domains = {
            "medium.com",
            "towardsdatascience.com",
            "jianshu.com",
            "51cto.com",
            "toutiao.com",
            "yidianzixun.com",
            "bilibili.com",
        }
        source_preview_by_title = {
            self._normalize_plan_text(item.get("title", ""), max_len=220).lower(): str(item.get("url", "")).strip()
            for item in source_preview
            if self._normalize_plan_text(item.get("title", ""), max_len=220)
        }

        if self.llm.enabled and source_preview:
            prompt = self._lang_text(
                state,
                (
                    "请输出结构化 evidence 列表，返回 ONLY JSON，格式："
                    '{"evidence":[{"claim":"...","evidence":"...","source_type":"academic|industry|official|news|blog|product_case|interview","source":"...","quote_or_note":"...","relevance":0,"confidence":0,"tags":["..."],"contradiction":"...","citations":["..."],"credibility_checks":{"author_identifiable":true,"has_citation":true,"cross_verifiable":true}}]}\n'
                    "硬性要求："
                    "1) 至少 5 条 evidence。"
                    "2) 不写长综述；每条要可被直接拼装进报告/PRD。"
                    "3) 若目标偏产品落地，至少 2 条 source_type 必须是 product_case 或 industry。"
                    "4) 普通网站优先：先给可读性强、能落地的解释与案例。官方仅做关键口径边界，学术仅在关键数字/结论必须严谨时补充。"
                    "5) 除非用户明确要求论文，academic 不得主导。"
                    "6) 可信度筛选必须满足：作者可识别、带引用、可交叉验证；避免内容农场和营销软文。"
                    "7) 输出内容必须中文。\n"
                    f"Goal: {state['goal']}\n"
                    f"Step objective: {step['objective']}\n"
                    f"Product intent: {product_intent}\n"
                    f"Academic requested: {academic_requested}\n"
                    f"Sources: {json.dumps(source_preview, ensure_ascii=False)}"
                ),
                (
                    "Output structured evidence as JSON ONLY with schema: "
                    '{"evidence":[{"claim":"...","evidence":"...","source_type":"academic|industry|official|news|blog|product_case|interview","source":"...","quote_or_note":"...","relevance":0,"confidence":0,"tags":["..."],"contradiction":"...","citations":["..."],"credibility_checks":{"author_identifiable":true,"has_citation":true,"cross_verifiable":true}}]}\n'
                    "Rules: at least 5 evidence items; no long prose; each item should be reusable in report/PRD. "
                    "If product intent is true, at least 2 items must be product_case or industry. "
                    "Prioritize ordinary reliable and readable websites first. "
                    "Use official sources for boundary/definition checks, and use academic sources only when critical numbers/claims need rigor. "
                    "Unless user explicitly requests papers, academic should be non-dominant. "
                    "Apply credibility filters: identifiable author, with citation, cross-verifiable; avoid content farms and marketing soft articles.\n"
                    f"Goal: {state['goal']}\n"
                    f"Step objective: {step['objective']}\n"
                    f"Product intent: {product_intent}\n"
                    f"Academic requested: {academic_requested}\n"
                    f"Sources: {json.dumps(source_preview, ensure_ascii=False)}"
                ),
            )

            response = await self.llm.chat(
                model=self.settings.llm_model_researcher,
                system_prompt=self._lang_text(
                    state,
                    (
                        "你是 researcher：证据收集与证据结构化专家。"
                        "你不写最终报告，只把可复用证据写入 evidence_store。"
                        "每条 evidence 都要短、清晰、可追溯、可对比。"
                        "普通网站优先，官方用于边界核对，学术仅在关键数字/结论必须严谨时使用。"
                        "除非用户明确要论文，学术不能占主要。"
                        "执行可信度筛选：作者可识别、带引用、可交叉验证，过滤内容农场与营销软文。"
                    ),
                    (
                        "You are the researcher: evidence collection and structuring specialist. "
                        "Do not write final report prose; output reusable evidence atoms only. "
                        "Prioritize ordinary reliable websites, use official sources for boundary checks, and use academic only for critical rigor checks unless explicitly requested. "
                        "Apply credibility filters: identifiable author, citation, cross-verification; filter content farms and advertorials."
                    ),
                ),
                user_prompt=prompt,
                max_tokens=1300,
            )
            if response is not None:
                data = parse_json_object(response.text)
                evidence_items = None
                if isinstance(data, dict):
                    evidence_items = data.get("evidence")
                    if not isinstance(evidence_items, list):
                        evidence_items = data.get("notes")
                if isinstance(evidence_items, list):
                    cleaned: list[dict[str, Any]] = []
                    product_case_count = 0
                    academic_count = 0
                    academic_cap = 3 if academic_requested else 1
                    for item in evidence_items[:12]:
                        if not isinstance(item, dict):
                            continue

                        claim = str(item.get("claim", "")).strip()
                        evidence = str(item.get("evidence", "")).strip()
                        if not claim or not evidence:
                            continue

                        source_type = str(item.get("source_type", "")).strip().lower()
                        if source_type not in allowed_source_types:
                            source_type = "industry" if product_intent else "news"
                        if source_type == "academic" and academic_count >= academic_cap:
                            continue

                        source_raw = item.get("source")
                        source_url = ""
                        author_name = ""
                        if isinstance(source_raw, dict):
                            source_text = " | ".join(
                                str(source_raw.get(key, "")).strip()
                                for key in ("title", "author", "year", "url")
                                if str(source_raw.get(key, "")).strip()
                            )
                            source_url = str(source_raw.get("url", "")).strip()
                            author_name = str(source_raw.get("author", "")).strip()
                        else:
                            source_text = str(source_raw or "").strip()

                        quote_or_note = str(item.get("quote_or_note", "")).strip() or evidence[:220]
                        relevance = self._normalize_score_0_3(item.get("relevance"), default=2)
                        confidence = self._normalize_score_0_3(item.get("confidence"), default=1)
                        contradiction = str(item.get("contradiction", "")).strip()

                        tags = item.get("tags")
                        if isinstance(tags, list):
                            tags_clean = [str(tag).strip() for tag in tags if str(tag).strip()]
                        else:
                            tags_clean = []

                        citations = item.get("citations")
                        if isinstance(citations, list):
                            citations_clean = [str(c).strip() for c in citations if str(c).strip()]
                        else:
                            citations_clean = []

                        if source_url and source_url not in citations_clean:
                            citations_clean.append(source_url)
                        if not citations_clean:
                            title_key = self._normalize_plan_text(source_text, max_len=220).lower()
                            inferred_url = source_preview_by_title.get(title_key, "")
                            if inferred_url:
                                citations_clean.append(inferred_url)

                        citation_domains = [extract_domain(citation) for citation in citations_clean if citation]
                        citation_domains = [domain for domain in citation_domains if domain]
                        main_domain = citation_domains[0] if citation_domains else extract_domain(source_url)
                        if not main_domain:
                            continue
                        if main_domain in blocked_marketing_domains or main_domain in LOW_QUALITY_DOMAINS:
                            continue

                        credibility_raw = item.get("credibility_checks")
                        credibility_dict = credibility_raw if isinstance(credibility_raw, dict) else {}
                        author_identifiable = bool(credibility_dict.get("author_identifiable")) or bool(author_name)
                        if not author_identifiable and source_type in {"official", "academic"}:
                            author_identifiable = True
                        has_citation = bool(credibility_dict.get("has_citation")) or bool(citations_clean)
                        if not (author_identifiable and has_citation):
                            continue

                        if source_type in {"product_case", "industry"}:
                            product_case_count += 1
                        if source_type == "academic":
                            academic_count += 1

                        cleaned.append(
                            {
                                "claim": claim,
                                "evidence": evidence,
                                "source_type": source_type,
                                "source": source_text,
                                "quote_or_note": quote_or_note,
                                "relevance": relevance,
                                "confidence": confidence,
                                "tags": tags_clean,
                                "contradiction": contradiction,
                                "citations": citations_clean,
                                "credibility_checks": {
                                    "author_identifiable": author_identifiable,
                                    "has_citation": has_citation,
                                    "cross_verifiable": bool(credibility_dict.get("cross_verifiable")),
                                },
                            }
                        )

                    for index, current in enumerate(cleaned):
                        current_domains = {
                            extract_domain(url) for url in current.get("citations", []) if isinstance(url, str) and url.strip()
                        }
                        current_domains = {domain for domain in current_domains if domain}
                        current_tags = {str(tag).strip().lower() for tag in current.get("tags", []) if str(tag).strip()}
                        cross_verifiable = False
                        for other_index, other in enumerate(cleaned):
                            if index == other_index:
                                continue
                            other_domains = {
                                extract_domain(url) for url in other.get("citations", []) if isinstance(url, str) and url.strip()
                            }
                            other_domains = {domain for domain in other_domains if domain}
                            if not current_domains or not other_domains:
                                continue
                            if current_domains & other_domains:
                                continue
                            other_tags = {str(tag).strip().lower() for tag in other.get("tags", []) if str(tag).strip()}
                            if current_tags and other_tags and current_tags & other_tags:
                                cross_verifiable = True
                                break
                        current_cred = current.get("credibility_checks", {})
                        if isinstance(current_cred, dict):
                            current_cred["cross_verifiable"] = bool(current_cred.get("cross_verifiable")) or cross_verifiable
                            current["credibility_checks"] = current_cred

                    strictly_verified = [
                        note
                        for note in cleaned
                        if isinstance(note.get("credibility_checks"), dict) and note["credibility_checks"].get("cross_verifiable")
                    ]
                    if len(strictly_verified) >= min_evidence_count:
                        cleaned = strictly_verified

                    # Fill up to minimum evidence count from source preview.
                    seed_index = 0
                    fill_attempts = 0
                    max_fill_attempts = max(12, len(source_preview) * 8) if source_preview else 12
                    while len(cleaned) < min_evidence_count:
                        fill_attempts += 1
                        if fill_attempts > max_fill_attempts:
                            break
                        src = source_preview[seed_index % len(source_preview)] if source_preview else {"title": "", "url": "", "excerpt": ""}
                        seed_index += 1
                        fallback_url = str(src.get("url", "")).strip()
                        fallback_domain = extract_domain(fallback_url)
                        if fallback_domain in blocked_marketing_domains or fallback_domain in LOW_QUALITY_DOMAINS:
                            continue
                        if fallback_domain.endswith(".gov") or fallback_domain.endswith(".int") or fallback_domain in {"gov.cn", "who.int"}:
                            fallback_type = "official"
                        else:
                            fallback_type = "industry" if (product_intent and product_case_count < 2) else "news"
                        if fallback_type in {"industry", "product_case"}:
                            product_case_count += 1
                        cleaned.append(
                            {
                                "claim": self._lang_text(
                                    state,
                                    f"{src.get('title') or state['goal']} 提供了与当前问题直接相关的线索。",
                                    f"{src.get('title') or state['goal']} offers directly relevant signals.",
                                ),
                                "evidence": str(src.get("excerpt", "")).strip()[:240]
                                or self._lang_text(
                                    state,
                                    "来源片段信息有限，但可作为后续验证线索。",
                                    "Snippet is limited but can be used as a follow-up lead.",
                                ),
                                "source_type": fallback_type,
                                "source": str(src.get("title", "")).strip(),
                                "quote_or_note": str(src.get("excerpt", "")).strip()[:180],
                                "relevance": 1,
                                "confidence": 1,
                                "tags": [self._lang_text(state, "补充证据", "supplementary-evidence")],
                                "contradiction": "",
                                "citations": [fallback_url] if fallback_url else [],
                                "credibility_checks": {
                                    "author_identifiable": True,
                                    "has_citation": bool(fallback_url),
                                    "cross_verifiable": False,
                                },
                            }
                        )

                    if cleaned:
                        return cleaned[:10]

        fallback_notes: list[dict[str, Any]] = []
        seed_sources = source_preview if source_preview else [{"title": "", "url": "", "excerpt": ""}]
        product_case_count = 0
        for idx in range(max(min_evidence_count, len(seed_sources))):
            item = seed_sources[idx % len(seed_sources)]
            domain = extract_domain(str(item.get("url", "")).strip())
            if domain in LOW_QUALITY_DOMAINS or domain in blocked_marketing_domains:
                continue
            if domain.endswith(".gov") or domain.endswith(".int") or domain in {"gov.cn", "who.int"}:
                source_type = "official"
            else:
                source_type = "industry" if (product_intent and product_case_count < 2) else "news"
            if source_type in {"industry", "product_case"}:
                product_case_count += 1
            fallback_notes.append(
                {
                    "claim": self._lang_text(
                        state,
                        f"{item.get('title') or state['goal']} 与研究目标“{state['goal']}”相关。",
                        f"{item.get('title') or state['goal']} is relevant to {state['goal']}.",
                    ),
                    "evidence": str(item.get("excerpt", "")).strip()[:240]
                    or self._lang_text(
                        state,
                        "来源片段显示其可能提供有效证据。",
                        "Source snippet indicates potentially useful evidence.",
                    ),
                    "source_type": source_type,
                    "source": str(item.get("title", "")).strip(),
                    "quote_or_note": str(item.get("excerpt", "")).strip()[:180],
                    "relevance": 1,
                    "confidence": 1,
                    "tags": [self._lang_text(state, "候选证据", "candidate-evidence")],
                    "contradiction": "",
                    "citations": [str(item.get("url", "")).strip()] if str(item.get("url", "")).strip() else [],
                    "credibility_checks": {
                        "author_identifiable": True,
                        "has_citation": bool(str(item.get("url", "")).strip()),
                        "cross_verifiable": False,
                    },
                }
            )

        return fallback_notes

    async def _build_outline(self, state: dict[str, Any], step: dict[str, Any], notes: list[dict[str, Any]]) -> list[str]:
        note_claims = [note.get("claim", "") for note in notes[:5]]

        if self.llm.enabled:
            prompt = self._lang_text(
                state,
                (
                    "请为研究备忘录生成 3-4 段提纲，输出 ONLY JSON，键名 outline。"
                    "提纲内容必须中文。\n"
                    f"Goal: {state['goal']}\n"
                    f"Step objective: {step['objective']}\n"
                    f"Claims: {json.dumps(note_claims, ensure_ascii=False)}"
                ),
                (
                    "Create a concise outline for a research memo with 3-4 sections. "
                    "Output ONLY JSON with key 'outline' as string array.\n"
                    f"Goal: {state['goal']}\n"
                    f"Step objective: {step['objective']}\n"
                    f"Claims: {json.dumps(note_claims, ensure_ascii=False)}"
                ),
            )
            response = await self.llm.chat(
                model=self.settings.llm_model_researcher,
                system_prompt=self._lang_text(
                    state,
                    "你是研究写作者，始终用中文输出。",
                    "You are a research writer.",
                ),
                user_prompt=prompt,
                max_tokens=220,
            )
            if response is not None:
                data = parse_json_object(response.text)
                outline = data.get("outline") if isinstance(data, dict) else None
                if isinstance(outline, list):
                    cleaned = [str(item).strip() for item in outline if str(item).strip()]
                    if cleaned:
                        return cleaned[:4]

        return self._lang_text(
            state,
            ["核心发现", "证据与引用", "反例与不确定性", "影响与下一步验证"],
            ["Core findings", "Evidence and citations", "Counterexamples and uncertainty", "Implications and next checks"],
        )

    async def _write_synthesis(
        self,
        state: dict[str, Any],
        step: dict[str, Any],
        outline: list[str],
        notes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if self.llm.enabled:
            prompt = self._lang_text(
                state,
                (
                    "请写出基于证据的阶段性总结，输出 ONLY JSON，键：summary, report, paragraphs。"
                    "paragraphs 为 {text, citations} 数组，内容必须中文。\n"
                    f"Goal: {state['goal']}\n"
                    f"Outline: {json.dumps(outline, ensure_ascii=False)}\n"
                    f"Notes: {json.dumps(notes, ensure_ascii=False)}"
                ),
                (
                    "Write a concise evidence-grounded synthesis. Output ONLY JSON with keys: "
                    "summary, report, paragraphs. "
                    "paragraphs should be array of {text, citations}.\n"
                    f"Goal: {state['goal']}\n"
                    f"Outline: {json.dumps(outline, ensure_ascii=False)}\n"
                    f"Notes: {json.dumps(notes, ensure_ascii=False)}"
                ),
            )
            response = await self.llm.chat(
                model=self.settings.llm_model_researcher,
                system_prompt=self._lang_text(
                    state,
                    "你负责输出严谨研究总结，始终用中文。",
                    "You produce careful deep-research summaries.",
                ),
                user_prompt=prompt,
                max_tokens=1400,
            )
            if response is not None:
                data = parse_json_object(response.text)
                if isinstance(data, dict) and isinstance(data.get("report"), str):
                    return {
                        "summary": str(data.get("summary", "")).strip(),
                        "report": str(data.get("report", "")).strip(),
                        "paragraphs": data.get("paragraphs", []),
                    }

        paragraphs = []
        report_lines = [self._lang_text(state, f"研究步骤：{step['title']}", f"Research step: {step['title']}")]
        for index, section in enumerate(outline, start=1):
            note = notes[(index - 1) % len(notes)] if notes else {"claim": "", "evidence": "", "citations": []}
            text = self._lang_text(
                state,
                f"{section}：{note.get('claim', '')}。证据：{note.get('evidence', '')}。",
                f"{section}: {note.get('claim', '')}. Evidence: {note.get('evidence', '')}.",
            )
            citations = note.get("citations", [])
            paragraphs.append({"text": text, "citations": citations})
            report_lines.append(
                self._lang_text(
                    state,
                    f"{index}. {text} 引用：{', '.join(citations) if citations else '无'}",
                    f"{index}. {text} Citations: {', '.join(citations) if citations else 'none'}",
                )
            )

        return {
            "summary": self._lang_text(
                state,
                f"已完成“{step['title']}”，产出 {len(notes)} 条结构化证据笔记。",
                f"Completed {step['title']} with {len(notes)} evidence notes.",
            ),
            "report": "\n".join(report_lines),
            "paragraphs": paragraphs,
        }

    async def _build_step_summary(
        self,
        state: dict[str, Any],
        step: dict[str, Any],
        notes: list[dict[str, Any]],
        synthesis: dict[str, Any],
        self_check: dict[str, Any],
    ) -> str:
        if self.llm.enabled:
            prompt = self._lang_text(
                state,
                (
                    "请输出 step_summary，用于给用户中途校准方向。"
                    "严格按以下结构输出（保留标题）：\n"
                    "- What we did (1-2 bullets)\n"
                    "- Key findings (3-7 bullets，必须对应 evidence claims/tags)\n"
                    "- Implications for MVP / decision (2-4 bullets)\n"
                    "- Open questions & next step (1-3 bullets)\n"
                    "不能凭空扩写，只能使用给定 evidence 与当前 step 信息。输出中文。\n"
                    f"Step: {json.dumps(step, ensure_ascii=False)}\n"
                    f"Evidence notes: {json.dumps(notes, ensure_ascii=False)}\n"
                    f"Synthesis: {json.dumps(synthesis, ensure_ascii=False)}\n"
                    f"Self check: {json.dumps(self_check, ensure_ascii=False)}"
                ),
                (
                    "Generate step_summary for in-progress user calibration using strict structure:\n"
                    "- What we did (1-2 bullets)\n"
                    "- Key findings (3-7 bullets, tied to evidence claims/tags)\n"
                    "- Implications for MVP / decision (2-4 bullets)\n"
                    "- Open questions & next step (1-3 bullets)\n"
                    "Use only provided evidence and step context. No invented facts.\n"
                    f"Step: {json.dumps(step, ensure_ascii=False)}\n"
                    f"Evidence notes: {json.dumps(notes, ensure_ascii=False)}\n"
                    f"Synthesis: {json.dumps(synthesis, ensure_ascii=False)}\n"
                    f"Self check: {json.dumps(self_check, ensure_ascii=False)}"
                ),
            )

            response = await self.llm.chat(
                model=self.settings.llm_model_researcher,
                system_prompt=self._lang_text(
                    state,
                    (
                        "你是 reporter：把 evidence 转成用户可用交付物。"
                        "你只能使用 evidence_store 中证据，不允许凭空扩写。"
                        "语言要面向“做事的人”，给可执行建议。"
                    ),
                    (
                        "You are the reporter: convert evidence into actionable deliverables. "
                        "Use evidence only and avoid unsupported expansion."
                    ),
                ),
                user_prompt=prompt,
                max_tokens=1200,
            )
            if response is not None and response.text.strip():
                return response.text.strip()

        key_findings = [str(item.get("claim", "")).strip() for item in notes[:5] if isinstance(item, dict) and str(item.get("claim", "")).strip()]
        open_questions = self_check.get("uncertainties", []) if isinstance(self_check, dict) else []
        key_findings_text = "\n".join(f"- {item}" for item in key_findings[:4]) or f"- {self._lang_text(state, '暂无稳定结论，需补充证据。', 'No stable conclusion yet; more evidence needed.')}"
        open_questions_text = "\n".join(f"- {str(item).strip()}" for item in open_questions[:3] if str(item).strip()) or f"- {self._lang_text(state, '下一步补齐关键证据缺口。', 'Next: fill critical evidence gaps.')}"
        return self._lang_text(
            state,
            (
                "What we did\n"
                f"- 完成步骤：{step.get('title', '')}\n"
                f"- 形成结构化证据条目：{len(notes)} 条\n\n"
                "Key findings\n"
                f"{key_findings_text}\n\n"
                "Implications for MVP / decision\n"
                f"- {synthesis.get('summary', '当前结论需结合后续证据再收敛。')}\n"
                "- 建议保持当前方向并针对争议点继续补证。\n\n"
                "Open questions & next step\n"
                f"{open_questions_text}"
            ),
            (
                "What we did\n"
                f"- Completed step: {step.get('title', '')}\n"
                f"- Structured evidence items: {len(notes)}\n\n"
                "Key findings\n"
                f"{key_findings_text}\n\n"
                "Implications for MVP / decision\n"
                f"- {synthesis.get('summary', 'Current conclusions need more evidence.')}\n"
                "- Continue with this direction while closing evidence disputes.\n\n"
                "Open questions & next step\n"
                f"{open_questions_text}"
            ),
        )

    async def _self_check(
        self,
        state: dict[str, Any],
        step: dict[str, Any],
        synthesis: dict[str, Any],
        notes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if self.llm.enabled:
            prompt = self._lang_text(
                state,
                (
                    "请做研究质量自检，输出 ONLY JSON，键：missing_points, counterexamples, uncertainties。"
                    "输出内容必须中文。\n"
                    f"Goal: {state['goal']}\n"
                    f"Step: {step['title']}\n"
                    f"Synthesis: {json.dumps(synthesis, ensure_ascii=False)}\n"
                    f"Notes: {json.dumps(notes, ensure_ascii=False)}"
                ),
                (
                    "Perform self-check for research quality. Output ONLY JSON with keys: "
                    "missing_points (array), counterexamples (array), uncertainties (array).\n"
                    f"Goal: {state['goal']}\n"
                    f"Step: {step['title']}\n"
                    f"Synthesis: {json.dumps(synthesis, ensure_ascii=False)}\n"
                    f"Notes: {json.dumps(notes, ensure_ascii=False)}"
                ),
            )
            response = await self.llm.chat(
                model=self.settings.llm_model_researcher,
                system_prompt=self._lang_text(
                    state,
                    "你是严苛的研究评估者，始终用中文输出。",
                    "You are a strict evaluator for deep research.",
                ),
                user_prompt=prompt,
                max_tokens=500,
            )
            if response is not None:
                data = parse_json_object(response.text)
                if isinstance(data, dict):
                    return {
                        "missing_points": data.get("missing_points", []),
                        "counterexamples": data.get("counterexamples", []),
                        "uncertainties": data.get("uncertainties", []),
                    }

        return self._lang_text(
            state,
            {
                "missing_points": ["如为全球议题，仍需补充更多区域性来源。"],
                "counterexamples": ["不同样本窗口下，部分来源可能得出相反趋势。"],
                "uncertainties": ["当前来源在时效性与方法质量上存在差异。"],
            },
            {
                "missing_points": ["Need broader non-English or regional sources if topic is global."],
                "counterexamples": ["Some reports may contradict trend conclusions depending on sample window."],
                "uncertainties": ["Source recency and methodology quality vary across retrieved documents."],
            },
        )

    async def _finalize_report(self, ctx: RunContext) -> None:
        step = int(ctx.state["next_step_index"])
        completed_steps: list[dict[str, Any]] = ctx.state.get("completed_steps", [])
        source_map = self._build_report_source_map(completed_steps)
        try:
            final_payload = await asyncio.wait_for(
                self._build_final_report(ctx.state, completed_steps, ctx.status),
                timeout=95.0,
            )
        except Exception:
            fallback_citations: list[str] = []
            fallback_findings: list[str] = []
            fallback_uncertainties: list[str] = []
            for step_item in completed_steps:
                if not isinstance(step_item, dict):
                    continue
                synthesis = step_item.get("synthesis", {})
                summary = str(synthesis.get("summary", "")).strip() if isinstance(synthesis, dict) else ""
                if summary:
                    fallback_findings.append(summary)
                for src in step_item.get("top_sources", []):
                    if not isinstance(src, dict):
                        continue
                    url = str(src.get("url", "")).strip()
                    if url:
                        fallback_citations.append(url)
                self_check = step_item.get("self_check", {})
                if isinstance(self_check, dict):
                    for value in self_check.get("uncertainties", []):
                        text = str(value).strip()
                        if text:
                            fallback_uncertainties.append(text)

            final_payload = self._fallback_final_report(
                state=ctx.state,
                goal=str(ctx.state.get("goal", "")),
                status=ctx.status,
                completed_steps=completed_steps,
                findings=list(dict.fromkeys(fallback_findings)),
                uncertainties=list(dict.fromkeys(fallback_uncertainties)),
                citations=list(dict.fromkeys(fallback_citations)),
                source_map=source_map,
                clarification_summary=self._extract_clarification_summary(ctx.state),
            )
        ctx.state["final_report"] = final_payload

        await self._emit(
            run_id=ctx.run_id,
            step=step,
            node="finalizer",
            event_type="final_report",
            payload=final_payload,
        )

    async def _build_final_report(
        self,
        state: dict[str, Any],
        completed_steps: list[dict[str, Any]],
        status: str,
    ) -> dict[str, Any]:
        goal = state["goal"]
        source_map = self._build_report_source_map(completed_steps)
        source_catalog_text = self._build_source_catalog_text(source_map)
        clarification_summary = self._extract_clarification_summary(state)

        findings: list[str] = []
        uncertainties: list[str] = []
        for step_item in completed_steps:
            if not isinstance(step_item, dict):
                continue
            synthesis = step_item.get("synthesis", {})
            if isinstance(synthesis, dict):
                summary = str(synthesis.get("summary", "")).strip()
                if summary:
                    findings.append(summary)
            self_check = step_item.get("self_check", {})
            if isinstance(self_check, dict):
                for value in self_check.get("uncertainties", []):
                    text = str(value).strip()
                    if text:
                        uncertainties.append(text)

        dedup_uncertainties = list(dict.fromkeys(uncertainties))
        dedup_citations = [
            str(source.get("url", "")).strip()
            for source in source_map.values()
            if isinstance(source, dict) and str(source.get("url", "")).strip()
        ]

        if self.llm.enabled and completed_steps and source_map:
            prompt = self._lang_text(
                state,
                (
                    "你是 reporter：输出 final_report。把 evidence_store（completed_steps 的 notes/top_sources/claims）整理成一篇可直接交付阅读的报告正文。\n\n"
                    "优先级：\n"
                    "- 第一优先：严格围绕用户的 query 来组织内容，按“用户在问什么”来写。\n"
                    "- 第二优先：参考澄清阶段 summary 来确定范围与语气。\n"
                    "- 事实与论据只能来自 evidence_store；禁止凭空补充或编造。证据不足就自然说明“目前证据不足/仍需补充”。\n\n"
                    "写作方式（非常重要）：\n"
                    "- 只写报告体连续自然段，允许少量小标题帮助阅读。\n"
                    "- 禁止“关键结论/不确定性/下一步”等标签式写法；禁止项目符号、编号、What we did/Key findings 碎片格式。\n"
                    "- 结构要自然服务于用户提问，把“是什么/怎么分/为什么/影响/接下来怎么办”说清楚。\n"
                    "- 语言克制、专业，不要论文腔。\n\n"
                    "证据可追溯（轻量）：\n"
                    "- 在关键句末尾附证据标记，格式固定：🔎[Sxx]。Sxx 必须来自 source_catalog。\n"
                    "- 没有证据就不要加标记，并在文中自然说明证据缺口。\n"
                    "- 正文里禁止出现长链接。\n\n"
                    "字数要求：正文 2000–4000 字（不含参考来源列表）。\n"
                    "输出 ONLY JSON，键：summary, report, cited_source_ids, key_findings, uncertainties, next_actions。\n"
                    "现在开始生成 final_report。\n\n"
                    f"User query: {goal}\n"
                    f"Clarification summary: {clarification_summary or '无'}\n"
                    f"Run status: {status}\n"
                    f"source_catalog:\n{source_catalog_text}\n\n"
                    f"evidence_store:\n{json.dumps(completed_steps, ensure_ascii=False)[:26000]}"
                ),
                (
                    "You are reporter. Generate final_report from evidence_store only, focused on user query and clarification scope. "
                    "Write coherent report prose; no list-style template. "
                    "Use evidence marker format 🔎[Sxx] where Sxx exists in source_catalog. No long links in report body. "
                    "Output JSON only with keys: summary, report, cited_source_ids, key_findings, uncertainties, next_actions.\n"
                    f"User query: {goal}\n"
                    f"Clarification summary: {clarification_summary or 'none'}\n"
                    f"Run status: {status}\n"
                    f"source_catalog:\n{source_catalog_text}\n\n"
                    f"evidence_store:\n{json.dumps(completed_steps, ensure_ascii=False)[:26000]}"
                ),
            )
            response = await self.llm.chat(
                model=self.settings.llm_model_researcher,
                system_prompt=self._lang_text(
                    state,
                    (
                        "你是 reporter：输出 final_report。"
                        "按用户问题组织连贯报告正文，不写模板化分点答案。"
                        "事实只能来自 evidence_store，关键句可使用 🔎[Sxx] 证据标记。"
                    ),
                    "You are reporter. Write coherent final report prose from evidence only.",
                ),
                user_prompt=prompt,
                max_tokens=4200,
            )
            if response is not None:
                data = parse_json_object(response.text)
                if isinstance(data, dict):
                    summary = self._strip_markdown_markers(str(data.get("summary", "")).strip())
                    report = self._strip_markdown_markers(str(data.get("report", "")).strip()).replace("\r\n", "\n")

                    def _keep_known_marker(match: re.Match[str]) -> str:
                        source_id = str(match.group(1) or "").strip()
                        return f"🔎[{source_id}]" if source_id in source_map else ""

                    report = re.sub(r"🔎\[(S\d+)\]", _keep_known_marker, report)
                    if source_map and "🔎[" not in report:
                        first_id = next(iter(source_map.keys()))
                        report = f"{report.rstrip()} 🔎[{first_id}]".strip()

                    is_zh = self._is_zh(state)
                    min_len = 2000 if is_zh else 1400
                    max_len = 4000 if is_zh else 3200
                    if len(report) > max_len:
                        report = report[:max_len].rstrip()

                    raw_ids = data.get("cited_source_ids", [])
                    cited_source_ids: list[str] = []
                    if isinstance(raw_ids, list):
                        cited_source_ids = [
                            str(item).strip()
                            for item in raw_ids
                            if str(item).strip() in source_map
                        ]
                    if not cited_source_ids:
                        cited_source_ids = list(dict.fromkeys(re.findall(r"🔎\[(S\d+)\]", report)))

                    key_findings_raw = data.get("key_findings", [])
                    if isinstance(key_findings_raw, list):
                        parsed_findings = [self._strip_markdown_markers(str(item).strip()) for item in key_findings_raw if str(item).strip()]
                    else:
                        parsed_findings = []

                    uncertainties_raw = data.get("uncertainties", [])
                    if isinstance(uncertainties_raw, list):
                        parsed_uncertainties = [self._strip_markdown_markers(str(item).strip()) for item in uncertainties_raw if str(item).strip()]
                    else:
                        parsed_uncertainties = []

                    next_actions_raw = data.get("next_actions", [])
                    if isinstance(next_actions_raw, list):
                        parsed_next_actions = [self._strip_markdown_markers(str(item).strip()) for item in next_actions_raw if str(item).strip()]
                    else:
                        parsed_next_actions = []

                    if not summary and report:
                        summary = self._normalize_plan_text(report.split("\n", 1)[0], max_len=180)

                    if summary and report and len(report) >= min_len:
                        return {
                            "goal": goal,
                            "status": status,
                            "completed_steps": len(completed_steps),
                            "summary": summary,
                            "report": report,
                            "key_findings": parsed_findings[:8] or findings[:8],
                            "uncertainties": parsed_uncertainties[:8] or dedup_uncertainties[:8],
                            "next_actions": parsed_next_actions[:8],
                            "cited_source_ids": cited_source_ids[:60],
                            "source_map": source_map,
                            "citations": dedup_citations[:60],
                            "generated_at": utc_now_iso(),
                        }

        return self._fallback_final_report(
            state=state,
            goal=goal,
            status=status,
            completed_steps=completed_steps,
            findings=findings,
            uncertainties=dedup_uncertainties,
            citations=dedup_citations,
            source_map=source_map,
            clarification_summary=clarification_summary,
        )

    def _fallback_final_report(
        self,
        state: dict[str, Any],
        goal: str,
        status: str,
        completed_steps: list[dict[str, Any]],
        findings: list[str],
        uncertainties: list[str],
        citations: list[str],
        source_map: dict[str, dict[str, Any]],
        clarification_summary: str,
    ) -> dict[str, Any]:
        if not completed_steps:
            plan_items = state.get("plan", [])
            plan_lines: list[str] = []
            if isinstance(plan_items, list):
                for item in plan_items[:3]:
                    if not isinstance(item, dict):
                        continue
                    title = str(item.get("title", "")).strip()
                    think = str(item.get("narrative_plan", "") or item.get("round_think", "")).strip()
                    merged = " ".join(part for part in [title, think] if part).strip()
                    if merged:
                        plan_lines.append(merged)

            summary = self._lang_text(
                state,
                "当前任务已结束，但尚未形成可入库的完成步骤，报告仅能给出问题边界与证据缺口。",
                "Run ended before any completed step entered evidence_store.",
            )
            report = self._lang_text(
                state,
                (
                    f"这次任务围绕“{goal}”展开，但在运行状态为 {status} 时结束，当前还没有完成步骤进入证据库，因此无法给出可追溯的结论。"
                    + (
                        f"澄清阶段已确认的范围是：{clarification_summary}。"
                        if clarification_summary
                        else "澄清阶段的范围尚不充分。"
                    )
                    + (
                        f"已有计划方向主要集中在：{'；'.join(plan_lines)}。"
                        if plan_lines
                        else "目前也没有稳定的计划链条可复盘。"
                    )
                    + "现阶段最重要的缺口不是观点，而是可核验的来源与完成步骤。建议继续当前任务，先完成至少一个研究轮次并写入证据，再生成最终报告，才能对用户问题给出有根据的回答。"
                ),
                (
                    f"The run for '{goal}' ended at status {status} before completed evidence steps were produced. "
                    + (f"Clarification scope: {clarification_summary}. " if clarification_summary else "")
                    + ("Current plan focus: " + "; ".join(plan_lines) + ". " if plan_lines else "")
                    + "The immediate gap is verifiable evidence. Continue the same task and complete at least one step before final reporting."
                ),
            )
            return {
                "goal": goal,
                "status": status,
                "completed_steps": 0,
                "summary": summary,
                "report": report,
                "key_findings": plan_lines[:3],
                "uncertainties": self._lang_text(
                    state,
                    ["由于没有完成步骤，暂无可综合证据。"],
                    ["No evidence synthesized due to zero completed steps."],
                ),
                "next_actions": self._lang_text(
                    state,
                    ["继续当前任务并至少完成 1 个步骤后再生成最终报告。"],
                    ["Continue this run and complete at least one step before final report."],
                ),
                "cited_source_ids": [],
                "source_map": source_map,
                "citations": citations[:60],
                "generated_at": utc_now_iso(),
            }

        key_findings = findings[:6] if findings else self._lang_text(
            state,
            ["已完成步骤中包含可汇总结论。"],
            ["Step-level synthesis available in completed steps."],
        )
        uncertainty_items = uncertainties[:6] if uncertainties else self._lang_text(
            state,
            ["不同来源的证据质量存在差异。"],
            ["Evidence quality varies across sources."],
        )
        next_actions = self._lang_text(
            state,
            [
                "优先回查核心结论对应的原始方法与样本边界。",
                "若涉及区域差异，补充本地化来源进行复核。",
                "按更严格来源约束重跑以提升结论稳健性。",
            ],
            [
                "Validate top claims against primary-source methodology sections.",
                "Add region-specific sources if decision scope is geographic.",
                "Re-run with tighter constraints for domains and timeframe.",
            ],
        )

        url_to_source_id = {
            str(item.get("url", "")).strip(): source_id
            for source_id, item in source_map.items()
            if isinstance(item, dict) and str(item.get("url", "")).strip()
        }
        all_notes: list[dict[str, Any]] = []
        for step_item in completed_steps:
            if not isinstance(step_item, dict):
                continue
            notes = step_item.get("notes", [])
            if isinstance(notes, list):
                for note in notes:
                    if isinstance(note, dict):
                        all_notes.append(note)

        evidence_sentences: list[str] = []
        for note in all_notes:
            claim = str(note.get("claim", "")).strip()
            evidence = str(note.get("evidence", "")).strip()
            if not claim and not evidence:
                continue
            marker = ""
            citations_in_note = note.get("citations", [])
            if isinstance(citations_in_note, list):
                for citation in citations_in_note:
                    source_id = url_to_source_id.get(str(citation).strip())
                    if source_id:
                        marker = f" 🔎[{source_id}]"
                        break
            sentence_text = "，".join(part for part in [claim, evidence[:120]] if part).strip("，")
            if sentence_text:
                evidence_sentences.append(f"{sentence_text}{marker}。")

        report_chunks: list[str] = []
        report_chunks.append(
            f"这份报告围绕“{goal}”组织，严格使用当前 evidence_store 的已完成步骤证据来回答用户问题。"
            + (f"澄清阶段确认范围为：{clarification_summary}。" if clarification_summary else "")
            + "报告将先给出核心判断，再说明边界和后续行动。"
        )

        if evidence_sentences:
            pointer = 0
            while pointer < len(evidence_sentences):
                block = " ".join(evidence_sentences[pointer:pointer + 4]).strip()
                if block:
                    report_chunks.append(block)
                pointer += 4
        else:
            report_chunks.append(
                self._lang_text(
                    state,
                    "当前证据条目不足以支撑细分结论，只能先给出方向性判断，并把补证需求前置。",
                    "Current evidence atoms are not yet enough for detailed conclusions.",
                )
            )

        report_chunks.append(
            self._lang_text(
                state,
                "现有材料显示，结论在不同人群、场景与时间窗口下可能产生明显偏移，偏移的主要来源是样本结构、记录口径和来源时效不一致。",
                "Current materials indicate boundary shifts across cohorts, contexts, and time windows.",
            )
        )
        if uncertainty_items:
            uncertainty_text = "；".join(str(item).strip() for item in uncertainty_items if str(item).strip())
            if uncertainty_text:
                report_chunks.append(
                    self._lang_text(
                        state,
                        f"当前仍需补充的关键证据缺口包括：{uncertainty_text}。这些缺口会直接影响结论的适用边界和优先级。",
                        f"Open evidence gaps remain: {uncertainty_text}. These gaps affect applicability and prioritization.",
                    )
                )

        report_chunks.append(" ".join(str(item).strip() for item in next_actions if str(item).strip()))

        report_text = "\n\n".join(chunk for chunk in report_chunks if chunk.strip())
        is_zh = self._is_zh(state)
        min_len = 2000 if is_zh else 1400
        max_len = 4000 if is_zh else 3200
        if len(report_text) < min_len and evidence_sentences:
            ptr = 0
            while len(report_text) < min_len and evidence_sentences:
                block = " ".join(evidence_sentences[ptr:ptr + 5]).strip()
                if block:
                    report_text = f"{report_text}\n\n{block}"
                ptr += 5
                if ptr >= len(evidence_sentences):
                    ptr = 0
                    if len(evidence_sentences) < 2:
                        break
        if len(report_text) > max_len:
            report_text = report_text[:max_len].rstrip()
        if source_map and "🔎[" not in report_text:
            first_id = next(iter(source_map.keys()))
            report_text = f"{report_text.rstrip()} 🔎[{first_id}]".strip()

        cited_source_ids = list(dict.fromkeys(re.findall(r"🔎\[(S\d+)\]", report_text)))
        summary_text = self._normalize_plan_text(report_text.split("\n", 1)[0], max_len=180)
        if not summary_text:
            summary_text = self._lang_text(
                state,
                f"共完成 {len(completed_steps)} 轮研究，已输出基于证据库的报告正文。",
                f"Completed {len(completed_steps)} steps with evidence-grounded report body.",
            )

        return {
            "goal": goal,
            "status": status,
            "completed_steps": len(completed_steps),
            "summary": summary_text,
            "report": report_text,
            "key_findings": key_findings,
            "uncertainties": uncertainty_items,
            "next_actions": next_actions,
            "cited_source_ids": cited_source_ids[:60],
            "source_map": source_map,
            "citations": citations[:60],
            "generated_at": utc_now_iso(),
        }

    async def _decide_next(self, ctx: RunContext) -> None:
        step = int(ctx.state["next_step_index"])
        max_steps = int(ctx.state["max_steps"])
        has_pending = any(item["status"] == "pending" for item in ctx.state["plan"])
        replan_requested = bool(ctx.state["flags"].get("replan"))
        auto_replan_attempts = int(ctx.state["flags"].get("auto_replan_attempts", 0))

        if ctx.state["flags"].get("interrupt_requested"):
            ctx.status = "running"
            ctx.state["flags"]["replan"] = True
            ctx.state["flags"]["auto_replan_attempts"] = 0
            decision = "interrupt_to_supervisor"
        elif ctx.state["flags"]["stop_requested"]:
            ctx.status = "stopped"
            decision = "stop_requested_by_steer"
        elif step >= max_steps:
            ctx.status = "completed"
            decision = "max_steps_reached"
        elif has_pending:
            ctx.status = "running"
            ctx.state["flags"]["auto_replan_attempts"] = 0
            decision = "continue_with_existing_plan"
        elif replan_requested and step < max_steps:
            ctx.status = "running"
            ctx.state["flags"]["auto_replan_attempts"] = 0
            decision = "replan"
        elif step < max_steps:
            auto_replan_attempts += 1
            ctx.state["flags"]["auto_replan_attempts"] = auto_replan_attempts
            if auto_replan_attempts <= 2:
                ctx.status = "running"
                ctx.state["flags"]["replan"] = True
                decision = "auto_replan_guard"
            else:
                ctx.status = "completed"
                decision = "plan_exhausted_after_guard"
        else:
            ctx.status = "completed"
            decision = "plan_exhausted"

        await self._emit(
            run_id=ctx.run_id,
            step=step,
            node="decide_next",
            event_type="decision",
            payload={
                "decision": decision,
                "status": ctx.status,
                "completed_steps": step,
                "max_steps": ctx.state["max_steps"],
                "auto_replan_attempts": ctx.state["flags"].get("auto_replan_attempts", 0),
            },
        )
