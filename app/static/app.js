const state = {
  sessionId: null,
  stream: null,
  eventIds: new Set(),
  tools: 0,
  blocked: 0,
};

const elements = {
  connection: document.querySelector("#connection"),
  pulse: document.querySelector(".pulse"),
  sessionId: document.querySelector("#session-id"),
  form: document.querySelector("#prompt-form"),
  prompt: document.querySelector("#prompt"),
  send: document.querySelector("#send-button"),
  conversation: document.querySelector("#conversation"),
  timeline: document.querySelector("#timeline"),
  approvalEmpty: document.querySelector("#approval-empty"),
  approvalCard: document.querySelector("#approval-card"),
  approvalTool: document.querySelector("#approval-tool"),
  approvalArgs: document.querySelector("#approval-args"),
  approvalReason: document.querySelector("#approval-reason"),
  remember: document.querySelector("#remember"),
  metricTools: document.querySelector("#metric-tools"),
  metricBlocked: document.querySelector("#metric-blocked"),
  metricEvents: document.querySelector("#metric-events"),
  metricAudit: document.querySelector("#metric-audit"),
  runtimeSummary: document.querySelector("#runtime-summary"),
};

const eventLabels = {
  session_created: "会话已创建",
  run_started: "任务开始",
  model_started: "模型推理",
  model_output: "模型输出",
  role_started: "角色开始执行",
  plan_created: "计划已生成",
  review_completed: "审查已完成",
  approval_required: "等待人工审批",
  approval_resolved: "审批已处理",
  approval_expired: "审批已过期",
  tool_started: "工具开始执行",
  tool_retry: "工具重试",
  tool_finished: "工具执行完成",
  tool_blocked: "策略已拦截",
  run_completed: "任务完成",
  run_failed: "任务失败",
};

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
  return payload;
}

function addMessage(text, role = "agent") {
  const message = document.createElement("div");
  message.className = `message ${role}`;
  const label = document.createElement("span");
  label.className = "message-label";
  label.textContent = role === "user" ? "YOU" : role === "error" ? "ERROR" : "AGENT";
  message.append(label, document.createTextNode(text));
  elements.conversation.append(message);
  elements.conversation.scrollTop = elements.conversation.scrollHeight;
}

function renderEvent(event) {
  if (state.eventIds.has(event.id)) return;
  state.eventIds.add(event.id);
  const item = document.createElement("li");
  if (["approval_required", "tool_blocked", "run_failed"].includes(event.kind)) item.className = "risk";
  const title = document.createElement("strong");
  title.textContent = eventLabels[event.kind] || event.kind;
  const detail = document.createElement("span");
  const time = new Date(event.created_at).toLocaleTimeString("zh-CN", { hour12: false });
  detail.textContent = `${time} · #${String(event.id).padStart(3, "0")}`;
  item.append(title, detail);
  elements.timeline.prepend(item);
  elements.metricEvents.textContent = state.eventIds.size;

  if (event.kind === "tool_started") {
    state.tools += 1;
    elements.metricTools.textContent = state.tools;
  }
  if (["tool_blocked", "approval_required", "approval_expired"].includes(event.kind)) {
    state.blocked += 1;
    elements.metricBlocked.textContent = state.blocked;
  }
  if (event.kind === "approval_required") showApproval(event.payload);
}

function showApproval(pending) {
  elements.approvalEmpty.classList.add("hidden");
  elements.approvalCard.classList.remove("hidden");
  elements.approvalTool.textContent = pending.tool_name;
  elements.approvalArgs.textContent = JSON.stringify(pending.arguments, null, 2);
  elements.approvalReason.textContent = pending.reason;
}

function clearApproval() {
  elements.approvalCard.classList.add("hidden");
  elements.approvalEmpty.classList.remove("hidden");
  elements.remember.checked = false;
}

function connectEvents() {
  if (state.stream) state.stream.close();
  state.stream = new EventSource(`/v1/sessions/${state.sessionId}/events/stream`);
  state.stream.onopen = () => {
    elements.connection.textContent = "SSE 实时连接";
    elements.pulse.classList.remove("offline");
  };
  state.stream.onerror = () => {
    elements.connection.textContent = "连接重试中";
    elements.pulse.classList.add("offline");
  };
  Object.keys(eventLabels).forEach((kind) => {
    state.stream.addEventListener(kind, (message) => renderEvent(JSON.parse(message.data)));
  });
}

async function runPrompt(prompt) {
  if (!state.sessionId || !prompt.trim()) return;
  addMessage(prompt.trim(), "user");
  elements.send.disabled = true;
  elements.send.textContent = "运行中…";
  try {
    const result = await request(`/v1/sessions/${state.sessionId}/runs`, {
      method: "POST",
      body: JSON.stringify({ prompt: prompt.trim() }),
    });
    if (result.status === "completed") addMessage(result.response || "任务完成。");
    if (result.status === "awaiting_approval") showApproval(result.pending_approval);
  } catch (error) {
    addMessage(error.message, "error");
  } finally {
    elements.send.disabled = false;
    elements.send.innerHTML = "运行任务 <span>↗</span>";
  }
}

async function resolveApproval(approved) {
  try {
    const result = await request(`/v1/sessions/${state.sessionId}/approvals`, {
      method: "POST",
      body: JSON.stringify({ approved, remember_for_run: elements.remember.checked }),
    });
    clearApproval();
    if (result.status === "completed") addMessage(result.response || "任务完成。");
    if (result.status === "awaiting_approval") showApproval(result.pending_approval);
  } catch (error) {
    clearApproval();
    addMessage(error.message, "error");
  }
}

async function initialize() {
  try {
    const [health, session] = await Promise.all([
      request("/health"),
      request("/v1/sessions", { method: "POST", body: "{}" }),
    ]);
    elements.runtimeSummary.textContent =
      `${health.provider} / ${health.model} 已准备好。` +
      "选择上方任务，或在下方输入指令。";
    state.sessionId = session.id;
    elements.sessionId.textContent = session.id;
    elements.sessionId.title = session.id;
    connectEvents();
    const events = await request(`/v1/sessions/${session.id}/events`);
    events.forEach(renderEvent);
    if (new URLSearchParams(window.location.search).get("demo") === "approval") {
      await runPrompt("演示删除目录的危险操作");
    }
  } catch (error) {
    elements.connection.textContent = "服务不可用";
    elements.pulse.classList.add("offline");
    addMessage(error.message, "error");
  }
}

elements.form.addEventListener("submit", (event) => {
  event.preventDefault();
  const prompt = elements.prompt.value;
  elements.prompt.value = "";
  runPrompt(prompt);
});

document.querySelectorAll("[data-prompt]").forEach((button) => {
  button.addEventListener("click", () => runPrompt(button.dataset.prompt));
});

document.querySelector("#approve-button").addEventListener("click", () => resolveApproval(true));
document.querySelector("#deny-button").addEventListener("click", () => resolveApproval(false));
document.querySelector("#verify-audit").addEventListener("click", async () => {
  try {
    const result = await request("/v1/audit/verify");
    elements.metricAudit.textContent = result.records;
    addMessage(`审计链验证通过，共 ${result.records} 条记录。`);
  } catch (error) {
    addMessage(error.message, "error");
  }
});

window.addEventListener("beforeunload", () => state.stream?.close());
initialize();
