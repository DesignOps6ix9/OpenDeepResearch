const goalInput = document.getElementById("goalInput");
const maxStepsInput = document.getElementById("maxStepsInput");
const startBtn = document.getElementById("startBtn");
const runIdEl = document.getElementById("runId");
const connStateEl = document.getElementById("connState");
const tokenStreamEl = document.getElementById("tokenStream");
const timelineListEl = document.getElementById("timelineList");
const steerInput = document.getElementById("steerInput");
const scopeInput = document.getElementById("scopeInput");
const sendSteerBtn = document.getElementById("sendSteerBtn");
const finalReportViewEl = document.getElementById("finalReportView");
const finalReportStateEl = document.getElementById("finalReportState");

const rolePanels = {
  supervisor: document.getElementById("roleSupervisor"),
  planner: document.getElementById("rolePlanner"),
  researcher: document.getElementById("roleResearcher"),
};

const roleTabs = Array.from(document.querySelectorAll(".role-tab"));

const uiState = {
  runId: null,
  socket: null,
  roleLogs: {
    supervisor: [],
    planner: [],
    researcher: [],
  },
  hasFinalReport: false,
};

function setConnectionState(text) {
  connStateEl.textContent = text;
}

function setActiveRole(role) {
  roleTabs.forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.roleTab === role);
  });

  Object.entries(rolePanels).forEach(([name, panel]) => {
    panel.classList.toggle("active", name === role);
  });
}

function formatPayload(payload, maxLength = 1800) {
  const text = JSON.stringify(payload, null, 2);
  if (text.length <= maxLength) {
    return text;
  }
  return `${text.slice(0, maxLength)}\n... (truncated)`;
}

function appendRoleEvent(role, eventType, payload) {
  if (!rolePanels[role]) {
    return;
  }

  const line = [
    `[${new Date().toLocaleTimeString()}] ${eventType}`,
    formatPayload(payload),
  ].join("\n");

  uiState.roleLogs[role].push(line);
  if (uiState.roleLogs[role].length > 16) {
    uiState.roleLogs[role].shift();
  }

  rolePanels[role].textContent = uiState.roleLogs[role].join("\n\n");
  rolePanels[role].scrollTop = rolePanels[role].scrollHeight;
}

function addTimeline(event, extraClass = "") {
  const item = document.createElement("li");
  item.className = `timeline-item ${extraClass}`.trim();

  const head = document.createElement("div");
  head.innerHTML = `<code>${event.node}</code> · <strong>${event.event_type}</strong> · step ${event.step}`;

  const body = document.createElement("div");
  body.textContent = summarizePayload(event.payload);

  const time = document.createElement("small");
  time.textContent = new Date(event.created_at).toLocaleTimeString();

  item.appendChild(head);
  item.appendChild(body);
  item.appendChild(time);

  timelineListEl.prepend(item);
}

function summarizePayload(payload) {
  if (!payload) {
    return "-";
  }

  if (payload.summary) {
    return payload.summary;
  }

  if (payload.message) {
    return payload.message;
  }

  if (payload.decision) {
    return `decision=${payload.decision}, status=${payload.status}`;
  }

  if (payload.goal) {
    return `goal=${payload.goal}`;
  }

  if (payload.query) {
    return payload.query;
  }

  if (payload.count) {
    return `count=${payload.count}`;
  }

  if (payload.content) {
    return payload.content;
  }

  return JSON.stringify(payload);
}

function appendToken(text) {
  tokenStreamEl.textContent += text;
  tokenStreamEl.scrollTop = tokenStreamEl.scrollHeight;
}

function renderFinalReport(payload) {
  const summary = payload.summary || "";
  const report = payload.report || "";
  const findings = Array.isArray(payload.key_findings) ? payload.key_findings : [];
  const uncertainties = Array.isArray(payload.uncertainties) ? payload.uncertainties : [];
  const actions = Array.isArray(payload.next_actions) ? payload.next_actions : [];
  const citations = Array.isArray(payload.citations) ? payload.citations : [];

  const lines = [];
  lines.push(`Goal: ${payload.goal || "-"}`);
  lines.push(`Status: ${payload.status || "-"}`);
  lines.push(`Completed Steps: ${payload.completed_steps ?? "-"}`);
  lines.push("");
  lines.push("Summary:");
  lines.push(summary || "-");
  lines.push("");
  lines.push("Report:");
  lines.push(report || "-");
  if (findings.length) {
    lines.push("");
    lines.push("Key Findings:");
    findings.forEach((item, idx) => lines.push(`${idx + 1}. ${item}`));
  }
  if (uncertainties.length) {
    lines.push("");
    lines.push("Uncertainties:");
    uncertainties.forEach((item, idx) => lines.push(`${idx + 1}. ${item}`));
  }
  if (actions.length) {
    lines.push("");
    lines.push("Next Actions:");
    actions.forEach((item, idx) => lines.push(`${idx + 1}. ${item}`));
  }
  if (citations.length) {
    lines.push("");
    lines.push("Citations:");
    citations.slice(0, 20).forEach((item, idx) => lines.push(`${idx + 1}. ${item}`));
  }

  finalReportViewEl.textContent = lines.join("\n");
  finalReportStateEl.textContent = "已生成";
  uiState.hasFinalReport = true;
}

function clearRunView() {
  tokenStreamEl.textContent = "";
  timelineListEl.innerHTML = "";
  finalReportViewEl.textContent = "";
  finalReportStateEl.textContent = "等待运行完成";
  uiState.roleLogs = {
    supervisor: [],
    planner: [],
    researcher: [],
  };
  uiState.hasFinalReport = false;
  rolePanels.supervisor.textContent = "";
  rolePanels.planner.textContent = "";
  rolePanels.researcher.textContent = "";
}

function wsURL(path) {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${location.host}${path}`;
}

function openSocket(path) {
  if (uiState.socket) {
    uiState.socket.close();
  }

  const socket = new WebSocket(wsURL(path));
  uiState.socket = socket;

  socket.onopen = () => {
    setConnectionState("已连接");
  };

  socket.onclose = () => {
    setConnectionState("已断开");
  };

  socket.onerror = () => {
    setConnectionState("连接错误");
  };

  socket.onmessage = (message) => {
    let data;
    try {
      data = JSON.parse(message.data);
    } catch {
      return;
    }

    if (data.type === "ack") {
      addTimeline(
        {
          node: "client",
          event_type: "steer_ack",
          step: "-",
          created_at: new Date().toISOString(),
          payload: { content: `steer v${data.steer.version} accepted` },
        },
        "good",
      );
      return;
    }

    if (data.type !== "event") {
      return;
    }

    const event = data.event;

    if (event.event_type === "token" && event.payload && event.payload.text) {
      appendToken(event.payload.text);
    }
    if (event.event_type === "final_report" && event.payload) {
      renderFinalReport(event.payload);
    }
    if (event.event_type === "run_finished") {
      const hasFinal = event.payload && event.payload.has_final_report;
      if (hasFinal) {
        finalReportStateEl.textContent = "运行结束（含最终报告）";
      } else if (!uiState.hasFinalReport) {
        finalReportStateEl.textContent = "运行结束（未生成最终报告）";
      }
    }
    if (event.event_type === "run_failed") {
      finalReportStateEl.textContent = "运行失败";
    }

    if (event.node === "supervisor") {
      appendRoleEvent("supervisor", event.event_type, event.payload);
    }
    if (event.node === "planner") {
      appendRoleEvent("planner", event.event_type, event.payload);
    }
    if (event.node === "researcher") {
      appendRoleEvent("researcher", event.event_type, event.payload);
    }

    let style = "";
    if (event.event_type === "run_failed") {
      style = "warn";
    } else if (event.event_type === "step_completed" || event.event_type === "run_finished") {
      style = "good";
    }

    addTimeline(event, style);
  };
}

async function createRun(goal, maxSteps) {
  const response = await fetch("/api/runs", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ goal, max_steps: Number(maxSteps) }),
  });

  if (!response.ok) {
    throw new Error(`create run failed: ${response.status}`);
  }

  return response.json();
}

async function postSteer(runId, content, scope) {
  const response = await fetch(`/api/runs/${runId}/steer`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ content, scope }),
  });

  if (!response.ok) {
    throw new Error(`steer failed: ${response.status}`);
  }

  return response.json();
}

startBtn.addEventListener("click", async () => {
  const goal = goalInput.value.trim();
  const maxSteps = maxStepsInput.value.trim();

  if (!goal) {
    alert("请先输入研究目标");
    return;
  }

  startBtn.disabled = true;
  clearRunView();
  setConnectionState("启动中");
  finalReportStateEl.textContent = "运行中，等待汇总";

  try {
    const created = await createRun(goal, maxSteps);
    uiState.runId = created.run_id;
    runIdEl.textContent = created.run_id;
    openSocket(created.ws_path);
  } catch (error) {
    setConnectionState("启动失败");
    addTimeline(
      {
        node: "client",
        event_type: "error",
        step: "-",
        created_at: new Date().toISOString(),
        payload: { message: String(error) },
      },
      "warn",
    );
  } finally {
    startBtn.disabled = false;
  }
});

sendSteerBtn.addEventListener("click", async () => {
  const content = steerInput.value.trim();
  const scope = scopeInput.value;

  if (!uiState.runId) {
    alert("请先启动 Run");
    return;
  }

  if (!content) {
    alert("请输入 steer 内容");
    return;
  }

  try {
    if (uiState.socket && uiState.socket.readyState === WebSocket.OPEN) {
      uiState.socket.send(JSON.stringify({ type: "steer", content, scope }));
    } else {
      await postSteer(uiState.runId, content, scope);
      addTimeline(
        {
          node: "client",
          event_type: "steer_sent_http",
          step: "-",
          created_at: new Date().toISOString(),
          payload: { content },
        },
        "good",
      );
    }
    steerInput.value = "";
  } catch (error) {
    addTimeline(
      {
        node: "client",
        event_type: "steer_error",
        step: "-",
        created_at: new Date().toISOString(),
        payload: { message: String(error) },
      },
      "warn",
    );
  }
});

roleTabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    setActiveRole(tab.dataset.roleTab);
  });
});

setActiveRole("supervisor");
