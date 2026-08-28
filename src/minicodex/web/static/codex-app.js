"use strict";

const $ = (selector) => document.querySelector(selector);
const conversation = $("#conversation");
const layout = $("#app-layout");
const promptInput = $("#prompt-input");
const sendButton = $("#send-button");
const permissionSelect = $("#permission-select");
const modelSelect = $("#model-select");
const statusNode = $("#session-status");
const verificationNode = $("#verification-status");
const reviewPanel = $("#review-panel");
const approvalDialog = $("#approval-dialog");
const token = new URLSearchParams(window.location.search).get("token") || "";
const withToken = (path) => `${path}${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}`;

const state = {
  turns: new Map(),
  current: null,
  changes: new Map(),
  selectedPath: null,
  pendingPlan: null,
  pendingApprovalId: null,
  latestEventId: 0,
  executionMode: "act",
};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function setStatus(value) {
  const status = value || "IDLE";
  statusNode.textContent = status;
  statusNode.dataset.status = status;
  const busy = ["CONNECTING", "RUNNING", "WAITING_APPROVAL", "RECONNECTING", "CLOSING"].includes(status);
  promptInput.disabled = busy;
  sendButton.disabled = busy;
  permissionSelect.disabled = busy;
  modelSelect.disabled = busy || modelSelect.children.length < 2;
}

function setVerification(value) {
  verificationNode.textContent = value || "NOT_RUN";
  verificationNode.dataset.status = value || "NOT_RUN";
}

function removeEmpty() {
  const empty = $("#empty-state");
  if (empty) empty.remove();
}

function renderMarkdown(target, text) {
  window.MiniCodexMarkdown.renderMarkdown(target, text || "");
}

function createTurn(data) {
  removeEmpty();
  if (state.current) state.current.root.open = false;
  const root = element("details", "turn");
  root.open = true;
  const summary = element("summary", "turn-summary");
  summary.append(element("span", "turn-index", `对话 ${data.prompt_index || "—"}`), element("strong", "", data.text || ""));
  const body = element("div", "turn-body");
  const user = element("div", "user-message", data.text || "");
  const process = element("details", "process");
  process.open = true;
  const processSummary = element("summary", "", "执行过程 · 0 项");
  const processItems = element("div", "process-items");
  process.append(processSummary, processItems);
  const finalBlock = element("section", "final-block");
  const finalLabel = element("div", "final-label", "MiniCodex");
  const finalAnswer = element("div", "final-answer");
  const changes = element("section", "changes-slot");
  const plan = element("section", "plan-slot");
  finalBlock.append(finalLabel, finalAnswer, changes, plan);
  body.append(user, process, finalBlock);
  root.append(summary, body);
  conversation.append(root);
  const turn = {root, summary, process, processSummary, processItems, finalLabel, finalAnswer, changes, plan, promptIndex: Number(data.prompt_index || state.turns.size + 1), activityCount: 0, completed: false, hasFinal: false};
  state.turns.set(turn.promptIndex, turn);
  state.current = turn;
  root.scrollIntoView({behavior: "smooth", block: "nearest"});
  return turn;
}

function currentTurn() {
  return state.current || createTurn({prompt_index: state.turns.size + 1, text: "已恢复的会话"});
}

function updateProcess(turn) {
  turn.processSummary.textContent = `执行过程 · ${turn.activityCount} 项`;
}

function addProgress(data) {
  if (!data.text) return;
  const turn = currentTurn();
  turn.processItems.append(element("p", "progress-line", data.text));
  turn.activityCount += 1;
  updateProcess(turn);
}

function addActivity(data) {
  const turn = currentTurn();
  const item = element("details", "activity-item");
  item.dataset.ok = String(data.ok !== false);
  item.append(element("summary", "", data.text || "工具已完成"));
  if (data.detail) item.append(element("pre", "", JSON.stringify(data.detail, null, 2)));
  turn.processItems.append(item);
  turn.activityCount += 1;
  updateProcess(turn);
}

function renderFinal(data) {
  const turn = currentTurn();
  turn.finalLabel.textContent = `最终结果 · Turn ${data.turns || "—"}`;
  renderMarkdown(turn.finalAnswer, data.text || "");
  turn.hasFinal = true;
  setVerification(data.verification_status);
}

function completeTurn(data) {
  const turn = currentTurn();
  if (!turn.hasFinal) renderFinal(data);
  turn.completed = true;
  turn.process.open = false;
  turn.root.open = true;
  turn.root.dataset.status = "completed";
  renderChanges(turn);
}

function changesForTurn(promptIndex) {
  return [...state.changes.values()].filter((change) => Number(change.prompt_index) === Number(promptIndex));
}

function renderChanges(turn) {
  const changes = changesForTurn(turn.promptIndex);
  turn.changes.replaceChildren();
  if (!changes.length) return;
  const card = element("section", "changes-card");
  const additions = changes.reduce((sum, item) => sum + Number(item.additions || 0), 0);
  const deletions = changes.reduce((sum, item) => sum + Number(item.deletions || 0), 0);
  const head = element("header", "changes-head");
  head.append(element("strong", "", `已编辑 ${changes.length} 个文件`));
  const totals = element("span", "change-totals");
  totals.append(element("span", "plus", `+${additions}`), element("span", "", "  "), element("span", "minus", `-${deletions}`));
  head.append(totals);
  card.append(head);
  changes.forEach((change) => {
    const button = element("button", "change-file");
    button.type = "button";
    button.append(element("code", "", change.path), element("span", "plus", `+${change.additions || 0}`), element("span", "minus", `-${change.deletions || 0}`));
    button.addEventListener("click", () => openReview(change.path));
    card.append(button);
  });
  turn.changes.append(card);
}

function rememberChange(data) {
  const key = `${data.prompt_index}:${data.path}`;
  state.changes.set(key, data);
  const turn = state.turns.get(Number(data.prompt_index));
  if (turn) renderChanges(turn);
  if (state.selectedPath === data.path) renderReview(data.path);
}

function latestChangesByPath() {
  const latest = new Map();
  state.changes.forEach((change) => latest.set(change.path, change));
  return latest;
}

function renderReview(path) {
  const latest = latestChangesByPath();
  const change = latest.get(path);
  if (!change) return;
  state.selectedPath = path;
  $("#review-title").textContent = path;
  const fileList = $("#review-file-list");
  fileList.replaceChildren();
  latest.forEach((item, itemPath) => {
    const button = element("button", itemPath === path ? "selected" : "", itemPath);
    button.type = "button";
    button.addEventListener("click", () => renderReview(itemPath));
    fileList.append(button);
  });
  const diff = $("#review-diff");
  diff.replaceChildren();
  String(change.diff || "没有可预览的文本 Diff").split("\n").forEach((line) => {
    const row = element("span", "diff-row", line || " ");
    if (line.startsWith("+") && !line.startsWith("+++")) row.classList.add("add");
    else if (line.startsWith("-") && !line.startsWith("---")) row.classList.add("remove");
    else if (line.startsWith("@@") || line.startsWith("---") || line.startsWith("+++")) row.classList.add("header");
    diff.append(row);
  });
}

function openReview(path) {
  reviewPanel.hidden = false;
  layout.classList.add("review-open");
  renderReview(path);
}

function closeReview() {
  reviewPanel.hidden = true;
  layout.classList.remove("review-open");
  state.selectedPath = null;
}

function renderPlan(data) {
  state.pendingPlan = data;
  const turn = currentTurn();
  if (!turn.hasFinal && data.text) {
    turn.finalLabel.textContent = "规划结果";
    renderMarkdown(turn.finalAnswer, data.text);
    turn.hasFinal = true;
  }
  turn.process.open = false;
  const card = element("section", "plan-card");
  card.append(element("strong", "", "方案已完成"), element("p", "", `批准后将使用 ${String(data.execution_mode || state.executionMode).toUpperCase()} 执行。`));
  const actions = element("div", "plan-actions");
  const execute = element("button", "", "执行方案");
  execute.type = "button";
  execute.addEventListener("click", () => resolvePlan("execute").catch(reportError));
  const cancel = element("button", "secondary", "取消方案");
  cancel.type = "button";
  cancel.addEventListener("click", () => resolvePlan("cancel").catch(reportError));
  actions.append(execute, cancel);
  card.append(actions);
  turn.plan.replaceChildren(card);
  setStatus("WAITING_PLAN_APPROVAL");
}

function showApproval(data) {
  state.pendingApprovalId = data.request_id;
  const details = data.details || {};
  $("#approval-title").textContent = data.kind === "file_change" ? "允许写入这个 Diff？" : "允许执行这个命令？";
  $("#approval-purpose").textContent = `${data.summary || "Agent 请求执行操作"} · ${data.reason || "需要用户审批"}`;
  $("#approval-command").textContent = data.kind === "file_change" ? String(details.diff || "") : JSON.stringify(details.argv || [], null, 2);
  $("#approval-timeout").textContent = `风险 ${String(data.risk || "medium").toUpperCase()} · 等待 ${data.approval_timeout_sec || 300}s`;
  setStatus("WAITING_APPROVAL");
  if (!approvalDialog.open) approvalDialog.showModal();
}

const handlers = {
  session_started(data) { if (data.workspace) setWorkspace(data.workspace); if (data.execution_mode) setPermission(data.execution_mode); },
  status(data) { setStatus(data.value); },
  verification(data) { setVerification(data.status); },
  user_prompt(data) { createTurn(data); },
  progress: addProgress,
  tool_summary: addActivity,
  command_summary: addActivity,
  file_changed: rememberChange,
  final_answer: renderFinal,
  turn_completed: completeTurn,
  plan_started() { addProgress({text: `已进入只读 PLAN，执行时仍使用 ${state.executionMode.toUpperCase()}`}); },
  plan_ready: renderPlan,
  plan_resolved() { state.pendingPlan = null; if (state.current) state.current.plan.replaceChildren(); },
  approval_required: showApproval,
  approval_resolved() { state.pendingApprovalId = null; if (approvalDialog.open) approvalDialog.close(); setStatus("RUNNING"); },
  error(data) { addActivity({ok: false, text: `错误 · ${data.code || "UNKNOWN"}`, detail: data}); },
};

function handleEvent(type, data) {
  const handler = handlers[type];
  if (handler) handler(data || {});
}

function setWorkspace(path) {
  $("#workspace-path").textContent = path || "—";
  const parts = String(path || "").replace(/\\/g, "/").split("/").filter(Boolean);
  $("#workspace-name").textContent = parts[parts.length - 1] || "本地工作区";
}

function setPermission(mode) {
  if (!['act', 'auto-act'].includes(mode)) return;
  state.executionMode = mode;
  permissionSelect.value = mode;
}

function setModels(models, selected) {
  modelSelect.replaceChildren();
  (models && models.length ? models : [selected]).filter(Boolean).forEach((model) => {
    const option = element("option", "", model);
    option.value = model;
    modelSelect.append(option);
  });
  modelSelect.value = selected || "";
}

async function loadSnapshot() {
  const response = await fetch(withToken("/api/session"));
  if (!response.ok) throw new Error(`session snapshot failed: ${response.status}`);
  const data = await response.json();
  state.latestEventId = Math.max(state.latestEventId, Number(data.event_id || 0));
  setWorkspace(data.workspace);
  setPermission(data.execution_mode || (data.mode === "auto-act" ? "auto-act" : "act"));
  setModels(data.allowed_models, data.model);
  setVerification(data.verification_status);
  (data.file_changes || []).forEach(rememberChange);
  if (data.pending_plan) renderPlan(data.pending_plan);
  else setStatus(data.status);
  if (data.pending_approval) showApproval(data.pending_approval);
  return data;
}

function connectEvents(after) {
  const source = new EventSource(withToken(`/api/events?after=${after}`));
  Object.keys(handlers).forEach((name) => source.addEventListener(name, (event) => {
    state.latestEventId = Math.max(state.latestEventId, Number(event.lastEventId || 0));
    handleEvent(name, JSON.parse(event.data));
  }));
  source.onerror = () => setStatus("RECONNECTING");
  source.onopen = () => loadSnapshot().catch(reportError);
}

async function submitPrompt() {
  const text = promptInput.value.trim();
  if (!text) return;
  setStatus("RUNNING");
  const response = await fetch(withToken("/api/prompts"), {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({text, permission: permissionSelect.value, model: modelSelect.value})});
  if (!response.ok) { reportError(new Error(await response.text())); await loadSnapshot(); return; }
  promptInput.value = "";
}

async function changePermission() {
  const response = await fetch(withToken("/api/mode"), {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({mode: permissionSelect.value})});
  if (!response.ok) { await loadSnapshot(); throw new Error(await response.text()); }
  setPermission((await response.json()).mode);
}

async function resolvePlan(action) {
  if (!state.pendingPlan) return;
  setStatus("RUNNING");
  const response = await fetch(withToken(`/api/plans/${encodeURIComponent(state.pendingPlan.id)}/resolve`), {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({action})});
  if (!response.ok) { await loadSnapshot(); throw new Error(await response.text()); }
}

async function decideApproval(allow) {
  if (!state.pendingApprovalId) return;
  const response = await fetch(withToken(`/api/approvals/${encodeURIComponent(state.pendingApprovalId)}`), {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({allow})});
  if (!response.ok) throw new Error(await response.text());
}

function reportError(error) {
  setStatus("ERROR");
  addActivity({ok: false, text: "连接或请求失败", detail: {message: error.message || String(error)}});
}

$("#prompt-form").addEventListener("submit", (event) => { event.preventDefault(); submitPrompt().catch(reportError); });
promptInput.addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); submitPrompt().catch(reportError); } });
permissionSelect.addEventListener("change", () => changePermission().catch(reportError));
$("#close-review").addEventListener("click", closeReview);
$("#allow-command").addEventListener("click", () => decideApproval(true).catch(reportError));
$("#reject-command").addEventListener("click", () => decideApproval(false).catch(reportError));
approvalDialog.addEventListener("cancel", (event) => { event.preventDefault(); decideApproval(false).catch(reportError); });

window.MiniCodexApp = {handleEvent, setStatus, openReview, closeReview};
loadSnapshot().then(() => connectEvents(0)).catch(reportError);
