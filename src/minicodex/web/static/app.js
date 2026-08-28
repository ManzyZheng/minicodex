"use strict";

const $ = (selector) => document.querySelector(selector);
const timeline = $("#timeline");
const promptInput = $("#prompt-input");
const sendButton = $("#send-button");
const statusNode = $("#session-status");
const approvalDialog = $("#approval-dialog");
const modeSelect = $("#mode-select");
let pendingApprovalId = null;
let currentMode = "act";
let latestEventId = 0;
let currentTurn = null;
const MAX_TIMELINE_CARDS = 500;
const sessionToken = new URLSearchParams(window.location.search).get("token") || "";
const withToken = (path) => `${path}${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(sessionToken)}`;

function setStatus(value) {
  statusNode.textContent = value;
  statusNode.dataset.status = value;
  const busy = value !== "IDLE";
  promptInput.disabled = busy;
  sendButton.disabled = busy;
  modeSelect.disabled = busy;
}

function removeEmptyState() {
  const empty = $("#empty-state");
  if (empty) empty.remove();
}

function trimTimeline() {
  const items = timeline.querySelectorAll(".event-card, .event-line");
  if (items.length > MAX_TIMELINE_CARDS) items[0].remove();
}

function updateProcessSummary() {
  if (currentTurn) currentTurn.processSummary.textContent = `执行过程 · ${currentTurn.eventCount} 条记录`;
}

function appendItem(item, target, {countProcess = true} = {}) {
  const destination = target || (currentTurn && !currentTurn.completed ? currentTurn.processItems : timeline);
  destination.append(item);
  if (countProcess && currentTurn && destination === currentTurn.processItems) {
    currentTurn.eventCount += 1;
    updateProcessSummary();
  }
  trimTimeline();
  item.scrollIntoView({behavior:"smooth", block:"nearest"});
}

function beginTurn(data) {
  removeEmptyState();
  if (currentTurn) currentTurn.root.open = false;

  const root = document.createElement("details");
  root.className = "turn-group";
  root.open = true;
  root.dataset.status = "running";

  const summary = document.createElement("summary");
  summary.className = "turn-summary";
  const title = document.createElement("strong");
  title.className = "turn-summary-title";
  title.textContent = `PROMPT ${data.prompt_index || "—"} · RUNNING`;
  const prompt = document.createElement("span");
  prompt.className = "turn-summary-prompt";
  prompt.textContent = data.text || "";
  summary.append(title, prompt);

  const body = document.createElement("div");
  body.className = "turn-body";
  const promptItems = document.createElement("div");
  promptItems.className = "turn-prompt";

  const process = document.createElement("details");
  process.className = "turn-process";
  process.open = true;
  const processSummary = document.createElement("summary");
  processSummary.className = "process-summary";
  processSummary.textContent = "执行过程 · 0 条记录";
  const processItems = document.createElement("div");
  processItems.className = "turn-process-items";
  process.append(processSummary, promptItems, processItems);

  const final = document.createElement("section");
  final.className = "turn-final";
  body.append(process, final);
  root.append(summary, body);
  timeline.append(root);

  currentTurn = {
    root, title, promptItems, process, processSummary, processItems, final,
    promptIndex: data.prompt_index || "—", eventCount: 0, completed: false,
  };
  addCard("user_prompt", `USER · PROMPT ${data.prompt_index || ""}`, data.text, "text", promptItems, false);
}

function completeTurn(data) {
  if (!currentTurn) beginTurn({prompt_index: "—", text: "已恢复的会话输出"});
  currentTurn.completed = true;
  currentTurn.root.dataset.status = "completed";
  currentTurn.root.open = true;
  currentTurn.process.open = false;
  currentTurn.title.textContent = `PROMPT ${currentTurn.promptIndex} · COMPLETED · FINAL TURN ${data.turns || "—"}`;
  addCard("turn_completed", `AGENT · FINAL · TURN ${data.turns || "—"}`, data.text || "任务结束", "markdown", currentTurn.final, false);
  if (currentMode === "plan") addPlanActions(currentTurn.final);
  currentTurn.root.scrollIntoView({behavior:"smooth", block:"nearest"});
}

function setModeUI(mode) {
  currentMode = mode || "act";
  modeSelect.value = currentMode;
  modeSelect.dataset.mode = currentMode;
}

function addPlanActions(target) {
  const actions = document.createElement("section");
  actions.className = "plan-actions";
  const note = document.createElement("p");
  note.textContent = "方案已生成。选择审查每个副作用，或授权本会话在工作区内自主实施。";
  const act = document.createElement("button");
  act.type = "button";
  act.className = "secondary";
  act.textContent = "在 ACT 中实施";
  act.addEventListener("click", () => approvePlan("act").catch(reportError));
  const auto = document.createElement("button");
  auto.type = "button";
  auto.textContent = "在 AUTO-ACT 中实施 →";
  auto.addEventListener("click", () => approvePlan("auto-act").catch(reportError));
  actions.append(note, act, auto);
  target.append(actions);
}

function addCard(kind, title, body, mode = "text", target = null, countProcess = true) {
  removeEmptyState();
  const card = document.createElement("article");
  card.className = "event-card";
  card.dataset.kind = kind;
  const meta = document.createElement("header");
  meta.className = "event-meta";
  const label = document.createElement("span");
  label.textContent = title;
  const stamp = document.createElement("time");
  stamp.textContent = new Date().toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", second:"2-digit"});
  meta.append(label, stamp);
  card.append(meta);
  if (mode === "diff") {
    const diff = document.createElement("div");
    diff.className = "diff-view";
    String(body).split("\n").forEach((line) => {
      const row = document.createElement("span");
      row.className = "diff-line";
      if (line.startsWith("+") && !line.startsWith("+++")) row.classList.add("add");
      else if (line.startsWith("-") && !line.startsWith("---")) row.classList.add("remove");
      else if (line.startsWith("@@") || line.startsWith("---") || line.startsWith("+++")) row.classList.add("header");
      row.textContent = line || " ";
      diff.append(row);
    });
    card.append(diff);
  } else {
    const content = document.createElement(mode === "code" ? "pre" : mode === "markdown" || mode === "summary" ? "div" : "p");
    content.className = mode === "code" ? "event-code" : mode === "markdown" || mode === "summary" ? "markdown-body" : "event-body";
    if (mode === "summary") content.classList.add("model-summary");
    if (mode === "markdown" || mode === "summary") window.MiniCodexMarkdown.renderMarkdown(content, body);
    else content.textContent = typeof body === "string" ? body : JSON.stringify(body, null, 2);
    card.append(content);
  }
  appendItem(card, target, {countProcess});
}

function addCompactLine(kind, text, target = null) {
  removeEmptyState();
  const line = document.createElement("article");
  line.className = "event-line";
  line.dataset.kind = kind;
  const message = document.createElement("code");
  message.textContent = text;
  const stamp = document.createElement("time");
  stamp.textContent = new Date().toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", second:"2-digit"});
  line.append(message, stamp);
  appendItem(line, target);
}

function commandOutput(data) {
  const argv = JSON.stringify(data.argv || [], null, 2);
  const output = [`argv = ${argv}`, data.stdout || "", data.stderr ? `[stderr]\n${data.stderr}` : "", `exit code: ${data.exit_code}`].filter(Boolean).join("\n");
  addCard(data.exit_code === 0 ? "command_output" : "error", "COMMAND OUTPUT", output, "code");
}

function showApproval(data) {
  pendingApprovalId = data.request_id;
  setStatus("WAITING_APPROVAL");
  const details = data.details || {};
  $("#approval-title").textContent = data.kind === "file_change" ? "允许 Agent 写入这个 Diff？" : "允许 Agent 执行这个命令？";
  $("#approval-purpose").textContent = `${data.summary || "Agent 请求执行操作"} · ${String(data.risk || "medium").toUpperCase()} · ${data.reason || "需要用户审批"}`;
  $("#approval-command").textContent = data.kind === "file_change" ? String(details.diff || "") : JSON.stringify(details.argv || [], null, 2);
  $("#approval-timeout").textContent = data.kind === "file_change" ? `路径 ${details.path || "—"} · 审批等待 ${data.approval_timeout_sec}s` : `第 ${Number(details.index || 0) + 1}/${details.count || 1} 步 · 命令上限 ${details.timeout_sec || 30}s · 审批等待 ${data.approval_timeout_sec}s`;
  if (!approvalDialog.open) approvalDialog.showModal();
}

const handlers = {
  session_started(data) { $("#workspace-path").textContent = data.workspace || "—"; $("#model-name").textContent = data.model || "—"; if (data.mode) setModeUI(data.mode); },
  mode_changed(data) { setModeUI(data.to); addCompactLine("mode_changed", `[mode] ${data.from || "—"} → ${data.to || "—"}`); },
  status(data) { setStatus(data.value); },
  user_prompt(data) { beginTurn(data); },
  model_reasoning(data) { if (data.content) addCard("model_reasoning", `THINKING · TURN ${data.turn || "—"}`, data.content, "summary"); },
  model_message(data) { addCard("model_message", `AGENT · TURN ${data.turn || "—"}`, data.content, "summary"); },
  tool_result(data) {
    if (data.ok) {
      addCompactLine("tool_result", `[tool:ok] ${data.tool || "tool"}: ${data.summary || "completed"}`);
      return;
    }
    addCard("error", `TOOL FAILED · ${data.tool || "tool"}`, data.error || data, "code");
  },
  diff(data) { addCard("diff", `DIFF · ${data.path || "file"}`, data.diff || "", "diff"); },
  command_output: commandOutput,
  verification(data) { $("#verification-status").textContent = data.status || "NOT_RUN"; },
  approval_required: showApproval,
  approval_resolved(_data) { if (approvalDialog.open) approvalDialog.close(); pendingApprovalId = null; setStatus("RUNNING"); },
  turn_completed(data) { $("#verification-status").textContent = data.verification_status || "NOT_RUN"; completeTurn(data); },
  error(data) { addCard("error", `ERROR · ${data.code || "UNKNOWN"}`, data.message || data, "code"); },
};

async function loadSnapshot() {
  const response = await fetch(withToken("/api/session"));
  if (!response.ok) throw new Error(`session snapshot failed: ${response.status}`);
  const data = await response.json();
  if (Number(data.event_id || 0) < latestEventId) return data;
  latestEventId = Math.max(latestEventId, Number(data.event_id || 0));
  $("#workspace-path").textContent = data.workspace;
  $("#model-name").textContent = data.model;
  $("#verification-status").textContent = data.verification_status;
  setModeUI(data.mode || "act");
  setStatus(data.status);
  if (data.pending_approval) {
    showApproval(data.pending_approval);
  } else {
    pendingApprovalId = null;
    if (approvalDialog.open) approvalDialog.close();
  }
  return data;
}

function connectEvents(afterId) {
  const source = new EventSource(withToken(`/api/events?after=${afterId}`));
  Object.entries(handlers).forEach(([name, handler]) => source.addEventListener(name, (event) => {
    latestEventId = Math.max(latestEventId, Number(event.lastEventId || 0));
    const data = JSON.parse(event.data);
    if (data._truncated) {
      addCard("tool_result", `${name.toUpperCase()} · TRUNCATED`, data.preview || "Event exceeded the Web preview budget.", "code");
      return;
    }
    handler(data);
  }));
  source.onerror = () => setStatus("RECONNECTING");
  source.onopen = () => loadSnapshot().catch(reportError);
}

async function submitPrompt() {
  const text = promptInput.value.trim();
  if (!text) return;
  setStatus("RUNNING");
  const response = await fetch(withToken("/api/prompts"), {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({text})});
  if (!response.ok) { addCard("error", `REQUEST FAILED · ${response.status}`, await response.text(), "code"); await loadSnapshot(); return; }
  promptInput.value = "";
}

async function decideApproval(allow) {
  if (!pendingApprovalId) return;
  $("#allow-command").disabled = true;
  $("#reject-command").disabled = true;
  const response = await fetch(withToken(`/api/approvals/${encodeURIComponent(pendingApprovalId)}`), {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({allow})});
  if (!response.ok) addCard("error", "APPROVAL FAILED", await response.text(), "code");
  $("#allow-command").disabled = false;
  $("#reject-command").disabled = false;
}

async function changeMode(mode) {
  const previous = currentMode;
  const response = await fetch(withToken("/api/mode"), {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({mode})});
  if (!response.ok) { setModeUI(previous); addCard("error", "MODE CHANGE FAILED", await response.text(), "code"); return; }
  setModeUI((await response.json()).mode);
}

async function approvePlan(mode) {
  setStatus("RUNNING");
  const response = await fetch(withToken("/api/plans/approve"), {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({mode})});
  if (!response.ok) { addCard("error", "PLAN APPROVAL FAILED", await response.text(), "code"); await loadSnapshot(); }
}

function reportError(error) { setStatus("ERROR"); addCard("error", "CONNECTION ERROR", error.message || String(error), "code"); }
$("#prompt-form").addEventListener("submit", (event) => { event.preventDefault(); submitPrompt().catch(reportError); });
promptInput.addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); submitPrompt().catch(reportError); } });
$("#allow-command").addEventListener("click", () => decideApproval(true).catch(reportError));
$("#reject-command").addEventListener("click", () => decideApproval(false).catch(reportError));
modeSelect.addEventListener("change", () => changeMode(modeSelect.value).catch(reportError));
approvalDialog.addEventListener("cancel", (event) => { event.preventDefault(); decideApproval(false).catch(reportError); });
loadSnapshot().then(() => connectEvents(0)).catch(reportError);
