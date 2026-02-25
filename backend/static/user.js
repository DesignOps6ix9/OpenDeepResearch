const historyListEl = document.getElementById("historyList");
const newTaskBtn = document.getElementById("newTaskBtn");
const feedEl = document.getElementById("feed");
const queryForm = document.getElementById("queryForm");
const queryInput = document.getElementById("queryInput");
const sendBtn = document.getElementById("sendBtn");
const sourceDrawerEl = document.getElementById("sourceDrawer");
const sourceDrawerMaskEl = document.getElementById("sourceDrawerMask");
const sourceDrawerCloseEl = document.getElementById("sourceDrawerClose");
const sourceDrawerIdEl = document.getElementById("sourceDrawerId");
const sourceDrawerTitleEl = document.getElementById("sourceDrawerTitle");
const sourceDrawerDomainEl = document.getElementById("sourceDrawerDomain");
const sourceDrawerSnippetEl = document.getElementById("sourceDrawerSnippet");
const sourceDrawerLinkEl = document.getElementById("sourceDrawerLink");

const HISTORY_REFRESH_MS = 7000;
const DEFAULT_MAX_STEPS = 4;
const RUNNING_STAGES = [
  { key: "scanning", zh: "scanning", en: "scanning" },
  { key: "reading", zh: "reading", en: "reading" },
  { key: "extracting", zh: "extracting", en: "extracting" },
  { key: "updating", zh: "updating", en: "updating" },
];
const EVENT_STAGE_MAP = {
  researcher_query_expansion: "scanning",
  researcher_search_results: "scanning",
  researcher_read_results: "reading",
  researcher_structured_notes: "extracting",
  researcher_outline: "updating",
  researcher_self_check: "updating",
  step_summary: "updating",
  researcher_output: "updating",
};

const state = {
  runs: [],
  activeRunId: null,
  activeGoal: "",
  activeStatus: "idle",
  outputLang: "zh",
  socket: null,
  seenEventIds: new Set(),
  rounds: new Map(),
  finalReport: null,
  clarifier: null,
  pendingSteerNotes: [],
  userInputs: [],
  seenUserInputKeys: new Set(),
  runningStageIndex: 0,
  runningStageKey: "scanning",
  runningTicker: null,
  sourceDrawerOpen: false,
};

function shorten(text, maxLen = 44) {
  const value = String(text || "").trim();
  if (value.length <= maxLen) return value;
  return `${value.slice(0, maxLen)}...`;
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function stripAsteriskMarkup(value) {
  return String(value || "")
    .replace(/\*{2,}/g, "")
    .trim();
}

function toLocalTime(iso) {
  if (!iso) return "-";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function normalizeStatus(status) {
  const s = String(status || "").toLowerCase();
  if (s.includes("failed")) return "failed";
  if (s.includes("running") || s.includes("queued") || s.includes("finalizing")) return "running";
  if (s.includes("completed") || s.includes("stopped")) return "completed";
  return "idle";
}

function detectLanguage(text) {
  return /[\u4e00-\u9fff]/.test(String(text || "")) ? "zh" : "en";
}

function isZh() {
  return String(state.outputLang || "zh").toLowerCase().startsWith("zh");
}

function t(zhText, enText) {
  return isZh() ? zhText : enText;
}

function runningStageLabel(key) {
  const hit = RUNNING_STAGES.find((item) => item.key === key) || RUNNING_STAGES[0];
  return t(hit.zh, hit.en);
}

function isFresh(updatedAt, windowMs = 4200) {
  if (!updatedAt) return false;
  const time = new Date(updatedAt).getTime();
  if (!Number.isFinite(time)) return false;
  return Date.now() - time <= windowMs;
}

function renderRunStatus(status) {
  const raw = String(status || "").toLowerCase();
  if (raw.includes("finalizing")) return t("报告生成中", "finalizing report");
  if (raw.includes("stopped")) return t("✓ 已停止", "✓ stopped");
  const normalized = normalizeStatus(status);
  if (normalized === "running") return t("进行中", "running");
  if (normalized === "completed") return t("✓ 已完成", "✓ completed");
  if (normalized === "failed") return t("失败", "failed");
  return t("待开始", "idle");
}

function renderRoundStatus(status) {
  const s = String(status || "").toLowerCase();
  if (s === "running") return t("研究中", "researching");
  if (s === "completed") return t("✓ 完成", "✓ completed");
  if (s === "paused") return t("已暂停", "paused");
  if (s === "planned" || s === "pending") return t("待执行", "planned");
  if (s === "failed") return t("失败", "failed");
  return status || t("待执行", "planned");
}

function roundStatusClass(status) {
  const s = String(status || "").toLowerCase();
  if (s === "running") return "running";
  if (s === "completed") return "completed";
  if (s === "paused") return "paused";
  if (s === "failed") return "failed";
  return "planned";
}

function resetRunView() {
  state.seenEventIds = new Set();
  state.rounds = new Map();
  state.finalReport = null;
  state.clarifier = null;
  state.pendingSteerNotes = [];
  state.userInputs = [];
  state.seenUserInputKeys = new Set();
  state.runningStageIndex = 0;
  state.runningStageKey = "scanning";
  closeSourceDrawer();
  feedEl.innerHTML = "";
}

function pushUserInput(kind, text, at, key) {
  const content = String(text || "").trim();
  if (!content) return;
  const dedupeKey = String(key || `${kind}|${content}|${at || ""}`);
  if (state.seenUserInputKeys.has(dedupeKey)) return;
  state.seenUserInputKeys.add(dedupeKey);
  state.userInputs.push({
    kind: String(kind || "input"),
    text: content,
    at: at || null,
  });
}

function userInputKindLabel(kind) {
  if (kind === "query") return t("初始问题", "Initial query");
  if (kind === "clarification") return t("澄清确认", "Clarification");
  if (kind === "steer") return t("研究纠偏", "Steer");
  return t("用户输入", "User input");
}

function normalizeListField(value) {
  if (Array.isArray(value)) {
    return value.map((item) => String(item || "").trim()).filter(Boolean);
  }
  const text = String(value || "").trim();
  if (!text) return [];
  return [text];
}

const PLAN_NOISE_PATTERNS = [
  /^[-*\s]*(user input timeline|user-input timeline|用户输入时间线|输入时间线|timeline)\b/i,
  /^[-*\s]*(steering update|干预更新|纠偏更新|supervisor decision|监督结论)\b/i,
  /^[-*\s]*(clarification confirmed|澄清确认)\b/i,
  /^[-*\s]*\[(initial query|clarification|steer)\]/i,
  /^[-*\s]*(initial query|clarification|steer)\b/i,
];

function isPlanNoiseLine(line) {
  const value = String(line || "").trim();
  if (!value) return false;
  return PLAN_NOISE_PATTERNS.some((pattern) => pattern.test(value));
}

function sanitizePlanText(value) {
  const lines = stripAsteriskMarkup(value)
    .split(/\r?\n/g)
    .map((line) => line.trim())
    .filter(Boolean)
    .filter((line) => !isPlanNoiseLine(line));
  return lines.join(" ").replace(/\s+/g, " ").trim();
}

function sanitizePlanMarkdown(value) {
  const lines = stripAsteriskMarkup(value)
    .split(/\r?\n/g)
    .map((line) => line.trimEnd())
    .filter((line) => line.trim())
    .filter((line) => !isPlanNoiseLine(line.trim()));
  return lines.join("\n").trim();
}

function sanitizePlanList(value, maxLen = 180) {
  return normalizeListField(value)
    .map((item) => sanitizePlanText(item))
    .map((item) => (item.length > maxLen ? `${item.slice(0, maxLen)}...` : item))
    .filter(Boolean);
}

function normalizeSourceMap(rawMap) {
  const normalized = {};
  if (!rawMap || typeof rawMap !== "object" || Array.isArray(rawMap)) return normalized;
  for (const [sourceIdRaw, entryRaw] of Object.entries(rawMap)) {
    const sourceId = String(sourceIdRaw || "").trim();
    if (!sourceId) continue;
    const entry = entryRaw && typeof entryRaw === "object" ? entryRaw : {};
    const url = String(entry.url || "").trim();
    if (!url) continue;
    normalized[sourceId] = {
      source_id: sourceId,
      title: String(entry.title || sourceId).trim() || sourceId,
      url,
      domain: String(entry.domain || "").trim(),
      snippet: String(entry.snippet || "").trim(),
    };
  }
  return normalized;
}

function getFinalSourceMap() {
  if (!state.finalReport || typeof state.finalReport !== "object") return {};
  return normalizeSourceMap(state.finalReport.source_map || {});
}

function renderReportBody(reportText, sourceMap) {
  const content = String(reportText || "").trim();
  if (!content) return `<p>${escapeHtml("-")}</p>`;
  const paragraphs = content.split(/\n{2,}/g).map((item) => item.trim()).filter(Boolean);
  const markerRegex = /🔎\[(S\d+)\]/g;

  const paragraphHtml = paragraphs.map((paragraph) => {
    let html = "";
    let lastIndex = 0;
    let match;
    markerRegex.lastIndex = 0;
    while ((match = markerRegex.exec(paragraph)) !== null) {
      const sourceId = String(match[1] || "").trim();
      const prefix = paragraph.slice(lastIndex, match.index);
      html += escapeHtml(prefix);
      const source = sourceMap[sourceId];
      const title = source ? source.title : t("来源未匹配", "Source not mapped");
      html += `<button class="evidence-mark" type="button" data-source-id="${escapeHtml(sourceId)}" title="${escapeHtml(title)}" aria-label="${escapeHtml(t(`查看来源 ${sourceId}`, `View source ${sourceId}`))}">🔎</button>`;
      lastIndex = markerRegex.lastIndex;
    }
    html += escapeHtml(paragraph.slice(lastIndex));
    html = html.replace(/\n/g, "<br>");
    return `<p>${html}</p>`;
  });
  return paragraphHtml.join("");
}

function closeSourceDrawer() {
  state.sourceDrawerOpen = false;
  if (sourceDrawerEl) {
    sourceDrawerEl.classList.remove("open");
    sourceDrawerEl.setAttribute("aria-hidden", "true");
  }
  if (sourceDrawerMaskEl) {
    sourceDrawerMaskEl.classList.remove("open");
    sourceDrawerMaskEl.setAttribute("aria-hidden", "true");
  }
}

function openSourceDrawer(sourceId) {
  const sourceMap = getFinalSourceMap();
  const source = sourceMap[sourceId];
  if (!source) return;
  if (sourceDrawerIdEl) sourceDrawerIdEl.textContent = source.source_id || sourceId;
  if (sourceDrawerTitleEl) sourceDrawerTitleEl.textContent = source.title || sourceId;
  if (sourceDrawerDomainEl) sourceDrawerDomainEl.textContent = source.domain || "-";
  if (sourceDrawerSnippetEl) sourceDrawerSnippetEl.textContent = source.snippet || t("暂无摘要。", "No snippet available.");
  if (sourceDrawerLinkEl) {
    sourceDrawerLinkEl.href = source.url;
    sourceDrawerLinkEl.textContent = t("Open link", "Open link");
  }
  state.sourceDrawerOpen = true;
  if (sourceDrawerEl) {
    sourceDrawerEl.classList.add("open");
    sourceDrawerEl.setAttribute("aria-hidden", "false");
  }
  if (sourceDrawerMaskEl) {
    sourceDrawerMaskEl.classList.add("open");
    sourceDrawerMaskEl.setAttribute("aria-hidden", "false");
  }
}

function syncComposerMode() {
  const running = Boolean(state.activeRunId) && normalizeStatus(state.activeStatus) === "running";
  const finalizing = String(state.activeStatus || "").toLowerCase().includes("finalizing");
  const clarifierWaiting = running && state.clarifier && state.clarifier.status === "awaiting_user";

  queryInput.disabled = false;
  sendBtn.disabled = false;

  if (finalizing) {
    queryInput.placeholder = t("报告正在生成中，请稍候...", "Final report is being generated...");
    sendBtn.textContent = t("生成中", "Finalizing");
    queryInput.disabled = true;
    sendBtn.disabled = true;
    return;
  }

  if (clarifierWaiting) {
    queryInput.placeholder = t("请回复澄清确认（例如：选B，输出对比表+行动建议）", "Reply to clarification (e.g., choose B + output format)");
    sendBtn.textContent = t("确认", "Confirm");
    return;
  }

  if (running) {
    queryInput.placeholder = t("发送纠偏指令（当前任务内继续研究，不会新建任务）", "Send steering to this running task (no new task)");
    sendBtn.textContent = t("纠偏", "Steer");
    return;
  }

  queryInput.placeholder = t("输入你的研究问题...", "Enter your research query...");
  sendBtn.textContent = t("发送", "Send");
}

function ensureRound(roundNumber) {
  if (!state.rounds.has(roundNumber)) {
    state.rounds.set(roundNumber, {
      roundNumber,
      stepId: `step-${roundNumber}`,
      title: t(`第${roundNumber}轮`, `Round ${roundNumber}`),
      status: "planned",
      cotText: "",
      links: [],
      updatedAt: null,
    });
  }
  return state.rounds.get(roundNumber);
}

function parseRoundFromStepId(stepId) {
  if (!stepId) return null;
  const match = String(stepId).match(/step-(\d+)/i);
  if (!match) return null;
  return Number(match[1]);
}

function parseRoundFromEvent(event) {
  const payload = event.payload || {};
  const fromStepId = parseRoundFromStepId(payload.step_id);
  if (fromStepId !== null) return fromStepId;
  return null;
}

function mergeLinks(round, links) {
  const map = new Map(round.links.map((item) => [item.url, item]));
  for (const item of links) {
    if (!item.url) continue;
    if (!map.has(item.url)) {
      map.set(item.url, item);
      continue;
    }
    const existing = map.get(item.url);
    if (existing && !existing.snippet && item.snippet) {
      existing.snippet = item.snippet;
    }
  }
  round.links = Array.from(map.values());
}

function applyPlannerSteps(steps, createdAt) {
  for (const step of steps) {
    if (!step || typeof step !== "object") continue;
    const roundNumber = parseRoundFromStepId(step.id) || 1;
    const round = ensureRound(roundNumber);

    round.stepId = String(step.id || round.stepId);
    round.title = String(step.title || t(`第${roundNumber}轮`, `Round ${roundNumber}`));
    const narrativePlan = sanitizePlanMarkdown(
      step.narrative_plan
      || step.plan_note
      || step.round_think
      || "",
    );
    const normalizedNarrative = sanitizePlanText(
      narrativePlan
        .replace(/【掌握的信息】/g, "")
        .replace(/【下一步要查什么】/g, "")
        .replace(/what we know/gi, "")
        .replace(/what to check next/gi, ""),
    );

    if (normalizedNarrative) {
      round.cotText = normalizedNarrative;
      round.updatedAt = createdAt;
      if (round.status !== "completed") {
        round.status = "planned";
      }
      continue;
    }

    const progressSummary = sanitizePlanList(step.progress_summary || step.progress || step.key_findings, 220).slice(0, 4);
    const knownFacts = progressSummary.length
      ? progressSummary.join("；")
      : sanitizePlanText(step.step_summary || step.round_think || step.intent || "")
        || t("目前还没有可靠材料，缺口是缺少可交叉验证的事实证据", "Reliable material is still missing, and the gap is cross-verifiable evidence");

    const nextDirectionTexts = [];
    const rawNextDirections = Array.isArray(step.next_directions) ? step.next_directions : [];
    for (const item of rawNextDirections.slice(0, 4)) {
      if (item && typeof item === "object") {
        const direction = sanitizePlanText(item.direction || item.task || "");
        const evidence = sanitizePlanText(item.evidence_to_find || "");
        const where = sanitizePlanText(item.where_to_check || "");
        const deliverable = sanitizePlanText(item.deliverable || "");
        const merged = [direction, evidence, where, deliverable].filter(Boolean).join("；");
        if (merged) nextDirectionTexts.push(merged);
      } else if (typeof item === "string") {
        const text = sanitizePlanText(item);
        if (text) nextDirectionTexts.push(text);
      }
    }

    const nextFocus = sanitizePlanText(step.next_focus || step.next_direction || step.query || "");
    const nextPlan = [nextFocus, ...nextDirectionTexts].filter(Boolean).join(" ");
    round.cotText = t(
      `目前已确认：${knownFacts}。接下来会重点查：${nextPlan || "先补齐最关键证据，再收敛争议点"}。`,
      `Confirmed so far: ${knownFacts}. Next we will check: ${nextPlan || "close key evidence gaps and converge disputed points"}.`,
    );
    round.updatedAt = createdAt;
    if (round.status !== "completed") {
      round.status = "planned";
    }
  }
}

function processEvent(event) {
  if (!event || typeof event.id !== "number") return;
  if (state.seenEventIds.has(event.id)) return;
  state.seenEventIds.add(event.id);

  const payload = event.payload || {};

  if (event.event_type === "run_started" && payload.goal) {
    state.activeGoal = String(payload.goal);
    state.activeStatus = "running";
    state.runningStageKey = "scanning";
    state.runningStageIndex = 0;
    state.outputLang = String(payload.language || detectLanguage(payload.goal || state.activeGoal || ""));
    pushUserInput("query", payload.goal, event.created_at || null, `run:${state.activeRunId || "active"}:query`);
  }

  if (event.event_type === "run_finished") {
    state.activeStatus = String(payload.status || "completed");
    if (normalizeStatus(state.activeStatus) === "completed") {
      state.runningStageKey = "updating";
    }
  }

  if (event.event_type === "run_failed") {
    state.activeStatus = "failed";
  }

  const stageKey = EVENT_STAGE_MAP[event.event_type];
  if (stageKey) {
    state.runningStageKey = stageKey;
    const stageIndex = RUNNING_STAGES.findIndex((item) => item.key === stageKey);
    if (stageIndex >= 0) {
      state.runningStageIndex = stageIndex;
    }
  }

  if (event.event_type === "planner_output") {
    const steps = Array.isArray(payload.steps) ? payload.steps : [];
    applyPlannerSteps(steps, event.created_at || null);
    if (steps.length && state.pendingSteerNotes.length) {
      state.pendingSteerNotes = [];
    }
  }

  const roundNumber = parseRoundFromEvent(event);
  const round = roundNumber === null ? null : ensureRound(roundNumber);
  if (round) {
    round.updatedAt = event.created_at || round.updatedAt;
  }

  switch (event.event_type) {
    case "step_started": {
      if (event.node === "researcher" && round) {
        round.status = "running";
        if (payload.title) {
          round.title = String(payload.title);
        }
      }
      break;
    }
    case "researcher_search_results": {
      if (!round) break;
      const results = Array.isArray(payload.results) ? payload.results : [];
      const links = results
        .map((item) => ({
          title: String(item.title || "Untitled"),
          url: String(item.url || "").trim(),
          domain: String(item.domain || ""),
          snippet: String(item.snippet || "").trim(),
        }))
        .filter((item) => item.url);
      mergeLinks(round, links);
      break;
    }
    case "research_paused_for_steer": {
      if (round) {
        round.status = "paused";
      }
      break;
    }
    case "step_completed": {
      if (event.node === "researcher" && round) {
        round.status = "completed";
      }
      break;
    }
    case "final_report": {
      state.finalReport = payload;
      break;
    }
    case "clarifier_question": {
      state.clarifier = {
        status: "awaiting_user",
        question: String(payload.text || ""),
        message: String(payload.message || ""),
        response: "",
        updatedAt: event.created_at || null,
      };
      state.activeStatus = "running";
      break;
    }
    case "clarifier_waiting": {
      if (!state.clarifier) {
        state.clarifier = {
          status: "awaiting_user",
          question: "",
          message: String(payload.message || ""),
          response: "",
          updatedAt: event.created_at || null,
        };
      } else {
        state.clarifier.status = "awaiting_user";
        state.clarifier.message = String(payload.message || state.clarifier.message || "");
        state.clarifier.updatedAt = event.created_at || state.clarifier.updatedAt || null;
      }
      break;
    }
    case "clarification_confirmed": {
      const content = String(payload.content || "").trim();
      state.clarifier = {
        status: "done",
        question: state.clarifier ? String(state.clarifier.question || "") : "",
        message: state.clarifier ? String(state.clarifier.message || "") : "",
        response: content,
        updatedAt: event.created_at || null,
      };
      if (content) {
        pushUserInput("clarification", content, event.created_at || null, `run:${state.activeRunId || "active"}:clarification`);
        state.pendingSteerNotes.push(`${t("澄清确认", "Clarification confirmed")}: ${content}`);
      }
      break;
    }
    case "steer_received": {
      const content = String(payload.content || "").trim();
      if (content) {
        pushUserInput("steer", content, event.created_at || null, `steer:${event.id}`);
        state.pendingSteerNotes.push(`${t("用户纠偏", "User steer")}: ${content}`);
      }
      break;
    }
    case "supervisor_output": {
      if (String(payload.mode || "") === "steer_review") {
        const summary = String(payload.summary || "").trim();
        if (summary) {
          state.pendingSteerNotes.push(`${t("监督结论", "Supervisor decision")}: ${summary}`);
        }
      }
      break;
    }
    case "run_finished":
    case "run_failed": {
      void hydrateFinalReportFromRun();
      break;
    }
    default:
      break;
  }

  renderFeed();
  syncComposerMode();
}

function renderLinks(links) {
  if (!links.length) {
    return `<p class="block-text">${escapeHtml(t("暂无阅读网页。", "No websites read yet."))}</p>`;
  }

  return `
    <div class="tag-row">
      ${links
        .slice(0, 40)
        .map(
          (item) => `
          <a class="source-tag" href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">
            <span class="source-domain">${escapeHtml(item.domain || "source")}</span>
            <span class="source-title">${escapeHtml(item.title)}</span>
            <span class="source-preview">${escapeHtml(shorten(item.snippet || t("暂无摘要", "No preview available"), 220))}</span>
          </a>
        `,
        )
        .join("")}
    </div>
  `;
}

function renderRound(round) {
  const cotText = stripAsteriskMarkup(round.cotText || "") || t("等待计划输出...", "Waiting for planning output...");
  const status = round.status || "planned";
  const statusKey = roundStatusClass(status);
  const freshClass = isFresh(round.updatedAt) ? "is-fresh" : "";
  const liveClass = statusKey === "running" ? "live-glow live-glow-active" : "";

  return `
    <details class="cot-round fade-in status-${escapeHtml(statusKey)} ${escapeHtml(freshClass)} ${escapeHtml(liveClass)}">
      <summary>
        <span class="summary-main">
          <span class="round-title">${escapeHtml(t(`第${round.roundNumber}轮`, `Round ${round.roundNumber}`))} · ${escapeHtml(round.title)}</span>
        </span>
        <span class="round-status-wrap">
          <span class="status-badge ${escapeHtml(statusKey)}">${escapeHtml(renderRoundStatus(status))}</span>
          <span class="round-status-time">${escapeHtml(toLocalTime(round.updatedAt))}</span>
        </span>
      </summary>
      <div class="round-body">
        <pre class="block-text">${escapeHtml(cotText)}</pre>

        <div class="block-title" style="margin-top:12px">${escapeHtml(t("来源", "Sources"))}</div>
        ${renderLinks(round.links || [])}
      </div>
    </details>
  `;
}

function renderFinalReport() {
  if (!state.finalReport) return "";

  const summary = stripAsteriskMarkup(state.finalReport.summary || "");
  const report = stripAsteriskMarkup(state.finalReport.report || "");
  const sourceMap = getFinalSourceMap();
  const preview = shorten(summary || report, 180);
  const freshClass = isFresh(state.finalReport.generated_at, 9000) ? "is-fresh" : "";

  return `
    <details class="card final-report fade-in ${escapeHtml(freshClass)}">
      <summary>
        <span class="summary-main">
          <span class="round-title">${escapeHtml(t("最终报告", "Final Report"))}</span>
          <span class="round-preview">${escapeHtml(preview || "-")}</span>
        </span>
        <span class="round-status-wrap">
          <span class="status-badge completed">${escapeHtml(t("✓ 已完成", "✓ completed"))}</span>
          <span class="round-status-time">${escapeHtml(toLocalTime(state.finalReport.generated_at))}</span>
        </span>
      </summary>
      <div class="round-body">
        <div class="block-title">${escapeHtml(t("摘要", "Summary"))}</div>
        <pre class="block-text">${escapeHtml(summary || "-")}</pre>
        <div class="block-title" style="margin-top:12px">${escapeHtml(t("正文", "Report"))}</div>
        <div class="report-rich">${renderReportBody(report, sourceMap)}</div>
      </div>
    </details>
  `;
}

function renderResearchingCard() {
  if (normalizeStatus(state.activeStatus) !== "running") return "";
  const stage = runningStageLabel(state.runningStageKey);
  return `
    <section class="card running-card fade-in is-breathing live-glow live-glow-active">
      <div class="live-strip">
        <span class="status-badge running pulse">${escapeHtml(t("研究中", "Running"))}</span>
        <div class="live-bar"><span class="live-bar-fill"></span></div>
        <span class="running-stage-text">${escapeHtml(stage)}</span>
      </div>
    </section>
  `;
}

function renderClarifierCard() {
  if (!state.clarifier) return "";
  const status = String(state.clarifier.status || "");
  const question = stripAsteriskMarkup(state.clarifier.question || "");
  const message = stripAsteriskMarkup(state.clarifier.message || "");
  const response = stripAsteriskMarkup(state.clarifier.response || "");
  const updatedAt = state.clarifier.updatedAt || null;

  if (status === "awaiting_user") {
    return `
      <section class="card clarifier-card fade-in">
        <h3>${escapeHtml(t("开始前澄清", "Pre-Research Clarification"))}</h3>
        <div class="round-status-wrap">
          <span class="status-badge paused">${escapeHtml(t("待确认", "Awaiting confirmation"))}</span>
          <span class="round-status-time">${escapeHtml(toLocalTime(updatedAt))}</span>
        </div>
        ${question ? `<pre class="block-text" style="margin-top:10px">${escapeHtml(question)}</pre>` : ""}
        ${message ? `<p class="clarifier-hint">${escapeHtml(message)}</p>` : ""}
      </section>
    `;
  }

  if (status === "done" && (question || response)) {
    const preview = shorten(response || question, 180);
    return `
      <details class="card clarifier-card fade-in">
        <summary>
          <span class="summary-main">
            <span class="round-title">${escapeHtml(t("澄清记录", "Clarification Record"))}</span>
            <span class="round-preview">${escapeHtml(preview)}</span>
          </span>
          <span class="round-status-wrap">
            <span class="status-badge completed">${escapeHtml(t("已确认", "Confirmed"))}</span>
            <span class="round-status-time">${escapeHtml(toLocalTime(updatedAt))}</span>
          </span>
        </summary>
        <div class="round-body">
          ${
            question
              ? `
            <div class="block-title">${escapeHtml(t("澄清问题", "Clarifier Question"))}</div>
            <pre class="block-text">${escapeHtml(question)}</pre>
          `
              : ""
          }
          <div class="block-title" style="margin-top:10px">${escapeHtml(t("你的确认", "Your Confirmation"))}</div>
          <pre class="block-text">${escapeHtml(response || "-")}</pre>
        </div>
      </details>
    `;
  }

  return "";
}

function renderFeed() {
  const rounds = Array.from(state.rounds.values()).sort((a, b) => a.roundNumber - b.roundNumber);

  const parts = [];

  if (!state.activeRunId) {
    feedEl.innerHTML = "";
    syncComposerMode();
    return;
  }

  const clarifierCard = renderClarifierCard();
  if (clarifierCard) {
    parts.push(clarifierCard);
  }

  const runningCard = renderResearchingCard();
  if (runningCard) {
    parts.push(runningCard);
  }

  for (const round of rounds) {
    parts.push(renderRound(round));
  }

  const finalCard = renderFinalReport();
  if (finalCard) {
    parts.push(finalCard);
  } else {
    closeSourceDrawer();
  }

  feedEl.innerHTML = parts.join("\n");
  syncComposerMode();
}

function renderHistory() {
  if (!state.runs.length) {
    historyListEl.innerHTML = `<li class="history-item"><div class="history-goal">暂无任务</div></li>`;
    return;
  }

  const renderRunItem = (run) => {
    const normalized = normalizeStatus(run.status);
    const active = run.run_id === state.activeRunId ? "active" : "";
    const running = normalized === "running" ? "running" : "";
    const title = shorten(run.goal || "-", 46);
    return `
      <li class="history-item ${active} ${running}" data-run-id="${escapeHtml(run.run_id)}">
        <div class="history-goal">${escapeHtml(title)}</div>
        <div class="history-meta">
          <span class="status-badge ${escapeHtml(normalized)}">${escapeHtml(renderRunStatus(run.status || "-"))}</span>
          <span>${escapeHtml(toLocalTime(run.updated_at))}</span>
        </div>
      </li>
    `;
  };

  const activeRun = state.runs.find((run) => run.run_id === state.activeRunId) || null;
  const activeCompletedId = activeRun && normalizeStatus(activeRun.status) === "completed" ? activeRun.run_id : "";
  const completedRuns = state.runs.filter((run) => normalizeStatus(run.status) === "completed" && run.run_id !== activeCompletedId);
  const visibleRuns = state.runs.filter((run) => normalizeStatus(run.status) !== "completed");
  if (activeRun && activeCompletedId && !visibleRuns.find((item) => item.run_id === activeRun.run_id)) {
    visibleRuns.unshift(activeRun);
  }

  historyListEl.innerHTML = [
    ...visibleRuns.map(renderRunItem),
    completedRuns.length
      ? `
        <li class="history-group">
          <details ${activeCompletedId ? "open" : ""}>
            <summary>${escapeHtml(t(`历史（${completedRuns.length}）`, `History (${completedRuns.length})`))}</summary>
            <ul class="history-sublist">
              ${completedRuns.map(renderRunItem).join("")}
            </ul>
          </details>
        </li>
      `
      : "",
  ].join("");

  for (const item of historyListEl.querySelectorAll(".history-item[data-run-id]")) {
    item.addEventListener("click", () => {
      const runId = item.getAttribute("data-run-id");
      if (runId) {
        openRun(runId);
      }
    });
  }
}

async function fetchJSON(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    throw new Error(`Request failed ${response.status}: ${url}`);
  }
  return response.json();
}

async function refreshHistory() {
  try {
    const data = await fetchJSON("/api/runs?limit=80");
    state.runs = Array.isArray(data.runs) ? data.runs : [];
    renderHistory();
    if (state.activeRunId) {
      await syncActiveRunSnapshot();
    }
  } catch (error) {
    console.error(error);
  }
}

async function syncActiveRunSnapshot() {
  if (!state.activeRunId) return;
  try {
    const run = await fetchJSON(`/api/runs/${state.activeRunId}`);
    let shouldRender = false;

    const nextStatus = String(run.status || state.activeStatus || "idle");
    if (nextStatus !== state.activeStatus) {
      state.activeStatus = nextStatus;
      shouldRender = true;
    }

    const finalReport = run && run.state ? run.state.final_report : null;
    if (finalReport && typeof finalReport === "object") {
      const prevStamp = state.finalReport ? String(state.finalReport.generated_at || "") : "";
      const nextStamp = String(finalReport.generated_at || "");
      if (!state.finalReport || prevStamp !== nextStamp) {
        state.finalReport = finalReport;
        shouldRender = true;
      }
    }

    if (shouldRender) {
      renderFeed();
    }
  } catch (error) {
    console.error(error);
  }
}

function closeSocket() {
  if (state.socket) {
    state.socket.close();
    state.socket = null;
  }
}

function connectSocket(runId) {
  closeSocket();

  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(`${protocol}//${location.host}/ws/runs/${runId}`);
  state.socket = socket;

  socket.onmessage = (message) => {
    let data;
    try {
      data = JSON.parse(message.data);
    } catch {
      return;
    }
    if (data.type !== "event") return;

    processEvent(data.event);

    if (data.event.event_type === "run_finished" || data.event.event_type === "run_failed") {
      refreshHistory();
    }
  };

  socket.onclose = () => {
    if (state.socket === socket) {
      state.socket = null;
    }
  };
}

async function openRun(runId) {
  try {
    const run = await fetchJSON(`/api/runs/${runId}`);

    state.activeRunId = run.run_id;
    state.activeGoal = String(run.goal || "");
    state.activeStatus = String(run.status || "idle");
    state.outputLang = String((run.state && run.state.language) || detectLanguage(state.activeGoal));
    const clarification = run && run.state ? run.state.clarification : null;

    resetRunView();
    pushUserInput("query", run.goal || "", run.created_at || null, `run:${run.run_id}:query`);
    if (clarification && typeof clarification === "object") {
      state.clarifier = {
        status: String(clarification.status || "done"),
        question: String(clarification.prompt || ""),
        message: clarification.status === "awaiting_user" ? t("请先完成澄清确认。", "Please complete clarification first.") : "",
        response: String(clarification.response || ""),
        updatedAt: String(clarification.responded_at || run.updated_at || ""),
      };
      if (String(clarification.response || "").trim()) {
        pushUserInput(
          "clarification",
          clarification.response,
          clarification.responded_at || run.updated_at || null,
          `run:${run.run_id}:clarification`,
        );
      }
    }
    const runFinalReport = run && run.state ? run.state.final_report : null;
    if (runFinalReport && typeof runFinalReport === "object") {
      state.finalReport = runFinalReport;
    }
    renderFeed();
    connectSocket(run.run_id);
    renderHistory();
  } catch (error) {
    console.error(error);
  }
}

async function createRun(goal) {
  return fetchJSON("/api/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ goal, max_steps: DEFAULT_MAX_STEPS }),
  });
}

async function submitSteer(runId, content, scope = "global") {
  return fetchJSON(`/api/runs/${runId}/steer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, scope }),
  });
}

async function hydrateFinalReportFromRun() {
  if (!state.activeRunId) return;
  if (state.finalReport) return;
  try {
    const run = await fetchJSON(`/api/runs/${state.activeRunId}`);
    const finalReport = run && run.state ? run.state.final_report : null;
    if (finalReport && typeof finalReport === "object") {
      state.finalReport = finalReport;
      renderFeed();
    }
  } catch (error) {
    console.error(error);
  }
}

function autoResize() {
  queryInput.style.height = "auto";
  queryInput.style.height = `${Math.min(queryInput.scrollHeight, 220)}px`;
}

function rotateRunningStage() {
  const running = Boolean(state.activeRunId) && normalizeStatus(state.activeStatus) === "running";
  if (!running) return;
  state.runningStageIndex = (state.runningStageIndex + 1) % RUNNING_STAGES.length;
  state.runningStageKey = RUNNING_STAGES[state.runningStageIndex].key;
  const stageEl = document.querySelector(".running-stage-text");
  if (stageEl) {
    stageEl.textContent = runningStageLabel(state.runningStageKey);
  }
}

function ensureRunningTicker() {
  if (state.runningTicker) return;
  state.runningTicker = window.setInterval(rotateRunningStage, 2200);
}

queryInput.addEventListener("input", autoResize);
queryInput.addEventListener("focus", () => {
  queryForm.classList.add("focused");
});
queryInput.addEventListener("blur", () => {
  if (!queryInput.value.trim()) {
    queryForm.classList.remove("focused");
  }
});
queryInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    queryForm.requestSubmit();
  }
});

queryForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  const goal = queryInput.value.trim();
  if (!goal) return;
  state.outputLang = detectLanguage(goal);

  sendBtn.disabled = true;
  try {
    if (String(state.activeStatus || "").toLowerCase().includes("finalizing")) {
      return;
    }
    const activeIsRunning = Boolean(state.activeRunId) && normalizeStatus(state.activeStatus) === "running";
    if (activeIsRunning && state.activeRunId) {
      await submitSteer(state.activeRunId, goal, "global");
      queryInput.value = "";
      autoResize();
      return;
    }

    const created = await createRun(goal);
    queryInput.value = "";
    queryForm.classList.remove("focused");
    autoResize();

    await refreshHistory();
    await openRun(created.run_id);
  } catch (error) {
    console.error(error);
  } finally {
    sendBtn.disabled = false;
    syncComposerMode();
  }
});

newTaskBtn.addEventListener("click", () => {
  closeSocket();
  state.activeRunId = null;
  state.activeGoal = "";
  state.activeStatus = "idle";
  state.outputLang = "zh";
  resetRunView();
  queryForm.classList.remove("focused");
  renderFeed();
  renderHistory();
  syncComposerMode();
  queryInput.focus();
});

feedEl.addEventListener("click", (event) => {
  const target = event.target instanceof HTMLElement ? event.target : null;
  if (!target) return;
  const mark = target.closest(".evidence-mark");
  if (!mark) return;
  const sourceId = String(mark.getAttribute("data-source-id") || "").trim();
  if (!sourceId) return;
  openSourceDrawer(sourceId);
});

if (sourceDrawerCloseEl) {
  sourceDrawerCloseEl.addEventListener("click", closeSourceDrawer);
}
if (sourceDrawerMaskEl) {
  sourceDrawerMaskEl.addEventListener("click", closeSourceDrawer);
}
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.sourceDrawerOpen) {
    closeSourceDrawer();
  }
});

async function bootstrap() {
  autoResize();
  await refreshHistory();
  renderFeed();
  syncComposerMode();

  ensureRunningTicker();
  window.setInterval(refreshHistory, HISTORY_REFRESH_MS);
}

bootstrap();
