"use strict";

const $ = (selector) => document.querySelector(selector);
const conversation = $("#conversation");
const conversationPane = $("#conversation-scroll");
const conversationNav = $("#conversation-nav");
const scrollToBottomButton = $("#scroll-to-bottom");
const layout = $("#app-layout");
const promptInput = $("#prompt-input");
const sendButton = $("#send-button");
const permissionSelect = $("#permission-select");
const modelSelect = $("#model-select");
const statusNode = $("#session-status");
const verificationNode = $("#verification-status");
const reviewPanel = $("#review-panel");
const approvalDialog = $("#approval-dialog");
const sessionReferences = $("#session-references");
const referenceSummary = $("#reference-summary");
const referenceList = $("#reference-list");
const projectList = $("#project-list");
const memoryView = $("#memory-view");
const memoryList = $("#memory-list");
const memoryToast = $("#memory-toast");
const token = new URLSearchParams(window.location.search).get("token") || "";
const withToken = (path) => `${path}${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}`;
const BUSY_STATUSES = new Set(["CONNECTING", "RUNNING", "STOPPING", "WAITING_APPROVAL", "RECONNECTING", "CLOSING"]);
const INTERRUPTIBLE_STATUSES = new Set(["RUNNING", "WAITING_APPROVAL"]);
const DEFAULT_VERIFICATION = "NOT_RUN";
const JSON_HEADERS = {"Content-Type": "application/json"};

const state = {
  turns: new Map(),
  current: null,
  changes: new Map(),
  selectedPath: null,
  pendingPlan: null,
  pendingApprovalId: null,
  latestEventId: 0,
  executionMode: "act",
  references: new Map(),
  pendingTurnReferences: [],
  status: "CONNECTING",
  projects: [],
  activeProjectId: null,
  activeSessionId: null,
  memoryScope: null,
};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function setStatus(value) {
  const status = value || "IDLE";
  state.status = status;
  statusNode.textContent = status;
  statusNode.dataset.status = status;
  const busy = BUSY_STATUSES.has(status);
  promptInput.disabled = busy;
  const interruptible = INTERRUPTIBLE_STATUSES.has(status);
  sendButton.textContent = interruptible || status === "STOPPING" ? "■" : "↑";
  sendButton.dataset.action = interruptible ? "stop" : (status === "STOPPING" ? "stopping" : "send");
  sendButton.disabled = status === "STOPPING" || (busy && !interruptible);
  permissionSelect.disabled = busy;
  modelSelect.disabled = busy || modelSelect.children.length < 2;
}

function setVerification(value) {
  const verification = value || DEFAULT_VERIFICATION;
  verificationNode.textContent = verification;
  verificationNode.dataset.status = verification;
}

function removeEmpty() {
  const empty = $("#empty-state");
  if (empty) empty.remove();
}

function resetConversation() {
  state.turns.forEach((turn) => { if (turn.timerId !== null) clearInterval(turn.timerId); });
  state.turns.clear();
  state.current = null;
  state.changes.clear();
  state.selectedPath = null;
  state.pendingPlan = null;
  conversation.replaceChildren();
  conversationNav.replaceChildren();
  const empty = element("article", "empty-state");
  empty.id = "empty-state";
  empty.append(element("div", "empty-mark", "M"), element("h1", "", "开始一个代码任务"), element("p", "", "让 MiniCodex 检查代码、修改文件并运行验证。"));
  conversation.append(empty);
  closeReview();
}

function hydrateHistory(history, verification) {
  if (state.turns.size || !Array.isArray(history)) return;
  let turn = null;
  history.forEach((message) => {
    if (message.role === "user") turn = createTurn({text: message.content, prompt_index: state.turns.size + 1});
    if (message.role === "assistant" && turn) {
      state.current = turn;
      renderFinal({text: message.content, turns: "—", verification_status: verification || "NOT_RUN"});
      completeTurn({text: message.content, turns: "—", verification_status: verification || "NOT_RUN"});
      turn = null;
    }
  });
}

function renderMarkdown(target, text) {
  window.MiniCodexMarkdown.renderMarkdown(target, text || "");
}

function isNearBottom() {
  return conversationPane.scrollHeight - conversationPane.scrollTop - conversationPane.clientHeight <= 80;
}

function syncScrollButton() {
  scrollToBottomButton.hidden = isNearBottom();
}

function scrollToBottom(behavior = "smooth") {
  conversationPane.scrollTo({top: conversationPane.scrollHeight, behavior});
  scrollToBottomButton.hidden = true;
}

function setActiveConversationTurn(promptIndex) {
  state.turns.forEach((turn) => {
    if (turn.navMarker) turn.navMarker.classList.toggle("active", turn.promptIndex === promptIndex);
  });
}

function syncActiveConversationTurn() {
  if (!state.turns.size || typeof conversationPane.getBoundingClientRect !== "function") return;
  const paneRect = conversationPane.getBoundingClientRect();
  const activationLine = paneRect.top + Math.min(180, paneRect.height * 0.28);
  let active = [...state.turns.values()][0];
  state.turns.forEach((turn) => {
    if (typeof turn.root.getBoundingClientRect === "function" && turn.root.getBoundingClientRect().top <= activationLine) active = turn;
  });
  if (active) setActiveConversationTurn(active.promptIndex);
}

function createConversationMarker(turn, promptText) {
  if (!conversationNav) return null;
  const marker = element("button", "conversation-nav-marker");
  marker.type = "button";
  marker.ariaLabel = `跳转到对话 ${turn.promptIndex}`;
  const line = element("span", "conversation-nav-line");
  const preview = element("span", "conversation-nav-preview");
  const label = element("span", "conversation-nav-label", `对话 ${turn.promptIndex}`);
  const normalized = String(promptText || "（空指令）").replace(/\s+/g, " ").trim();
  const excerpt = normalized.length > 180 ? `${normalized.slice(0, 180)}…` : normalized;
  preview.append(label, element("span", "conversation-nav-text", excerpt));
  marker.append(line, preview);
  marker.addEventListener("click", () => {
    setActiveConversationTurn(turn.promptIndex);
    turn.root.scrollIntoView({behavior: "smooth", block: "start"});
    turn.user.classList.add("conversation-nav-target");
    setTimeout(() => turn.user.classList.remove("conversation-nav-target"), 900);
  });
  conversationNav.append(marker);
  return marker;
}

function createTurn(data) {
  removeEmpty();
  const root = element("article", "turn");
  const body = element("div", "turn-body");
  const user = element("div", "user-message", data.text || "");
  if (state.pendingTurnReferences.length) {
    const chips = element("div", "reference-chips");
    state.pendingTurnReferences.forEach((reference) => {
      const chip = element("span", "reference-chip", `↗ ${reference.name} · ${reference.scope === "external" ? "外部只读" : "工作区"}`);
      chips.append(chip);
    });
    user.append(chips);
    state.pendingTurnReferences = [];
  }
  const process = element("details", "process");
  process.open = true;
  const processSummary = element("summary", "", "执行过程 · 0 个执行阶段");
  const processItems = element("div", "process-items");
  const modelTurnsToggle = element("button", "model-turns-toggle", "");
  modelTurnsToggle.type = "button";
  modelTurnsToggle.hidden = true;
  processItems.append(modelTurnsToggle);
  process.append(processSummary, processItems);
  const finalBlock = element("section", "final-block");
  const finalLabel = element("div", "final-label", "MiniCodex");
  const finalAnswer = element("div", "final-answer");
  const changes = element("section", "changes-slot");
  const plan = element("section", "plan-slot");
  finalBlock.append(finalLabel, finalAnswer, changes, plan);
  body.append(user, process, finalBlock);
  root.append(body);
  conversation.append(root);
  const turn = {
    root,
    user,
    process,
    processSummary,
    processItems,
    finalLabel,
    finalAnswer,
    changes,
    plan,
    promptIndex: Number(data.prompt_index || state.turns.size + 1),
    modelSteps: new Map(),
    modelTurnsToggle,
    showAllModelSteps: false,
    changesExpanded: false,
    startedAt: parseEventTime(data.event_timestamp) || Date.now(),
    completed: false,
    timerId: null,
    hasFinal: false,
    navMarker: null,
  };
  modelTurnsToggle.addEventListener("click", () => {
    turn.showAllModelSteps = !turn.showAllModelSteps;
    syncModelStepVisibility(turn);
  });
  state.turns.set(turn.promptIndex, turn);
  state.current = turn;
  turn.navMarker = createConversationMarker(turn, data.text);
  setActiveConversationTurn(turn.promptIndex);
  updateProcess(turn);
  turn.timerId = setInterval(() => {
    if (!turn.completed) updateProcess(turn);
  }, 1000);
  if (turn.timerId && typeof turn.timerId.unref === "function") turn.timerId.unref();
  return turn;
}

function currentTurn() {
  return state.current || createTurn({prompt_index: state.turns.size + 1, text: "已恢复的会话"});
}

function updateProcess(turn) {
  if (turn.completed) return;
  const duration = formatDuration(Math.max(0, Date.now() - turn.startedAt));
  turn.processSummary.textContent = `已用时 ${duration} · ${turn.modelSteps.size} 个执行阶段`;
}

function syncModelStepVisibility(turn) {
  const steps = [...turn.modelSteps.values()];
  const hiddenCount = Math.max(0, steps.length - 2);
  steps.forEach((step, index) => { step.root.hidden = !turn.showAllModelSteps && index < hiddenCount; });
  turn.modelTurnsToggle.hidden = hiddenCount === 0;
  turn.modelTurnsToggle.textContent = turn.showAllModelSteps
    ? `收起较早 ${hiddenCount} 个执行阶段`
    : `显示更早 ${hiddenCount} 个执行阶段`;
}

function ensureModelStep(turn, value) {
  const parsed = Number(value);
  const modelTurn = Number.isFinite(parsed) && parsed > 0
    ? parsed
    : Math.max(1, ...turn.modelSteps.keys(), 0);
  if (turn.modelSteps.has(modelTurn)) return turn.modelSteps.get(modelTurn);
  const root = element("section", "model-step");
  const header = element("div", "model-step-header");
  const number = element("span", "model-step-number", `Turn ${modelTurn}`);
  const progress = element("p", "model-step-progress");
  const tools = element("div", "model-step-tools");
  header.append(number, progress);
  root.append(header, tools);
  turn.processItems.append(root);
  const step = {root, progress, tools, activityGroups: new Map(), lastProgress: ""};
  turn.modelSteps.set(modelTurn, step);
  syncModelStepVisibility(turn);
  updateProcess(turn);
  return step;
}

function addProgress(data) {
  const text = String(data.text || "").trim();
  if (!text) return;
  const turn = currentTurn();
  const step = ensureModelStep(turn, data.turn);
  if (text === step.lastProgress) return;
  step.lastProgress = text;
  step.progress.textContent = text;
}

const activityLabels = {
  read_file: "读取文件",
  list_files: "浏览文件",
  search_text: "搜索代码",
  write_file: "修改文件",
  edit_file: "修改文件",
  run_shell: "运行 Shell",
};

function addActivity(data) {
  const turn = currentTurn();
  const step = ensureModelStep(turn, data.turn);
  const rawKey = data.group || data.tool || "tool";
  const key = data.ok === false ? `error:${rawKey}:${data.text || "unknown"}` : rawKey;
  let group = step.activityGroups.get(key);
  if (!group) {
    const item = element("details", "activity-item");
    const summary = element("summary");
    const detail = element("pre");
    item.dataset.ok = String(data.ok !== false);
    item.append(summary, detail);
    step.tools.append(item);
    group = {item, summary, detail, count: 0, label: data.label || activityLabels[rawKey] || data.text || "工具已完成"};
    step.activityGroups.set(key, group);
  }
  group.count += 1;
  group.summary.textContent = `${group.label} · ${group.count}`;
  group.detail.textContent = data.detail ? JSON.stringify(data.detail, null, 2) : "完整事件已保存在 JSONL Session Trace。";
}

function renderFinal(data) {
  const turn = currentTurn();
  const verification = data.verification_status || verificationNode.textContent || "NOT_RUN";
  turn.finalLabel.textContent = `Model Turn ${data.turns || "—"} · ${verification}`;
  renderMarkdown(turn.finalAnswer, data.text || "");
  turn.hasFinal = true;
  setVerification(data.verification_status);
}

function parseEventTime(value) {
  const parsed = Date.parse(String(value || ""));
  return Number.isFinite(parsed) ? parsed : null;
}

function formatDuration(milliseconds) {
  const seconds = Math.max(1, Math.round(milliseconds / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  if (minutes < 60) return `${minutes}分${remainingSeconds}秒`;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return `${hours}小时${String(remainingMinutes).padStart(2, "0")}分`;
}

function completeTurn(data) {
  const turn = currentTurn();
  if (!turn.hasFinal) renderFinal(data);
  turn.completed = true;
  if (turn.timerId !== null) {
    clearInterval(turn.timerId);
    turn.timerId = null;
  }
  const completedAt = parseEventTime(data.event_timestamp) || Date.now();
  const duration = formatDuration(Math.max(0, completedAt - turn.startedAt));
  turn.processSummary.textContent = `用时 ${duration} · ${turn.modelSteps.size} 个执行阶段`;
  turn.process.open = false;
  turn.root.dataset.status = "completed";
  renderChanges(turn);
}

function changesForTurn(promptIndex) {
  return [...state.changes.values()].filter((change) => Number(change.prompt_index) === Number(promptIndex));
}

function renderChanges(turn) {
  const changes = changesForTurn(turn.promptIndex);
  turn.changes.replaceChildren();
  if (!changes.length) {
    if (turn.completed) turn.changes.append(element("p", "no-changes", "本轮未修改文件"));
    return;
  }
  const card = element("section", "changes-card");
  const additions = changes.reduce((sum, item) => sum + Number(item.additions || 0), 0);
  const deletions = changes.reduce((sum, item) => sum + Number(item.deletions || 0), 0);
  const head = element("header", "changes-head");
  head.append(element("strong", "", `已编辑 ${changes.length} 个文件`));
  const totals = element("span", "change-totals");
  totals.append(element("span", "plus", `+${additions}`), element("span", "", "  "), element("span", "minus", `-${deletions}`));
  head.append(totals);
  card.append(head);
  const visibleChanges = turn.changesExpanded ? changes : changes.slice(0, 3);
  visibleChanges.forEach((change) => {
    const button = element("button", "change-file");
    button.type = "button";
    button.append(element("code", "", change.path), element("span", "plus", `+${change.additions || 0}`), element("span", "minus", `-${change.deletions || 0}`));
    button.addEventListener("click", () => openReview(change.path));
    card.append(button);
  });
  if (changes.length > 3) {
    const hiddenCount = changes.length - 3;
    const toggle = element("button", "changes-toggle", turn.changesExpanded ? "收起文件" : `再显示 ${hiddenCount} 个文件`);
    toggle.type = "button";
    toggle.addEventListener("click", () => {
      turn.changesExpanded = !turn.changesExpanded;
      renderChanges(turn);
    });
    card.append(toggle);
  }
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
    row.classList.add(window.MiniCodexMarkdown.classifyDiffLine(line));
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
  const reviewReason = details.review && details.review.reason ? ` · Reviewer：${details.review.reason}` : "";
  $("#approval-purpose").textContent = `${data.summary || "Agent 请求执行操作"} · ${data.reason || "需要用户审批"}${reviewReason}`;
  $("#approval-command").textContent = data.kind === "file_change" ? String(details.diff || "") : String(details.command || "");
  $("#approval-timeout").textContent = `风险 ${String(data.risk || "medium").toUpperCase()} · 等待 ${data.approval_timeout_sec || 300}s`;
  setStatus("WAITING_APPROVAL");
  if (!approvalDialog.open) approvalDialog.showModal();
}

function referenceMetadata(data) {
  return {
    id: String(data.id || ""),
    name: String(data.name || "文件"),
    path: String(data.path || ""),
    scope: data.scope === "workspace" ? "workspace" : "external",
    size: Number(data.size || 0),
    access: "read-only-session-snapshot",
  };
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KiB`;
}

function renderReferences() {
  const references = [...state.references.values()];
  sessionReferences.hidden = references.length === 0;
  referenceSummary.textContent = `本会话参考 · ${references.length}`;
  referenceList.replaceChildren();
  references.forEach((reference) => {
    const row = element("div", "reference-row");
    const info = element("div", "reference-info");
    info.append(
      element("strong", "", reference.name),
      element("span", "", `${reference.scope === "external" ? "外部只读" : "工作区"} · ${formatBytes(reference.size)}`),
      element("code", "", reference.path),
    );
    const remove = element("button", "reference-remove", "移除");
    remove.type = "button";
    remove.addEventListener("click", () => removeReference(reference.id).catch(reportError));
    row.append(info, remove);
    referenceList.append(row);
  });
}

function contextLoaded(data) {
  const reference = referenceMetadata(data);
  if (!reference.id) return;
  state.references.set(reference.id, reference);
  state.pendingTurnReferences.push(reference);
  renderReferences();
}

function contextRemoved(data) {
  if (data.id) state.references.delete(String(data.id));
  renderReferences();
}

function showMemoryToast(data) {
  if (!memoryToast) return;
  const scope = data.scope === "global" ? "全局" : "项目";
  memoryToast.textContent = `已自动记住 · ${scope}：${data.title || data.content || "新记忆"}`;
  memoryToast.hidden = false;
  setTimeout(() => { memoryToast.hidden = true; }, 5000);
}

const handlers = {
  session_started(data) { if (data.workspace) setWorkspace(data.workspace); if (data.execution_mode) setPermission(data.execution_mode); },
  status(data) { setStatus(data.value); },
  verification(data) { setVerification(data.status); },
  user_prompt(data) { createTurn(data); },
  progress: addProgress,
  tool_summary: addActivity,
  command_summary(data) { addActivity({...data, group: "command", label: data.text || "运行命令"}); },
  file_changed: rememberChange,
  final_answer: renderFinal,
  turn_completed: completeTurn,
  plan_started() { addProgress({text: `已进入只读 PLAN，执行时仍使用 ${state.executionMode.toUpperCase()}`}); },
  plan_ready: renderPlan,
  plan_resolved() { state.pendingPlan = null; if (state.current) state.current.plan.replaceChildren(); },
  approval_required: showApproval,
  approval_resolved(data) {
    state.pendingApprovalId = null;
    if (approvalDialog.open) approvalDialog.close();
    if (data.reason !== "interrupted" && state.status !== "STOPPING") setStatus("RUNNING");
  },
  context_loaded: contextLoaded,
  context_removed: contextRemoved,
  session_reset() { resetConversation(); },
  memory_created(data) { showMemoryToast(data); if (state.memoryScope === data.scope) loadMemories().catch(reportError); },
  memory_forgotten() { if (state.memoryScope) loadMemories().catch(reportError); },
  context_error(data) { addActivity({ok: false, group: "context-error", label: `参考文件失败 · ${data.code || "UNKNOWN"}`, detail: data}); },
  context_compacted(data) { addActivity({ok: true, group: "context-compact", label: `上下文已压缩 · ${formatCompactChars(data.before_chars)} → ${formatCompactChars(data.after_chars)} 字符`, detail: data}); },
  interrupt_requested() { setStatus("STOPPING"); },
  error(data) { addActivity({ok: false, group: "error", label: `错误 · ${data.code || "UNKNOWN"}`, text: `错误 · ${data.code || "UNKNOWN"}`, detail: data}); },
};

function handleEvent(type, data) {
  const followOutput = isNearBottom();
  const handler = handlers[type];
  if (handler) handler(data || {});
  if (followOutput) scrollToBottom("auto");
  else syncScrollButton();
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

function renderProjects(data) {
  state.projects = Array.isArray(data.projects) ? data.projects : [];
  state.activeProjectId = data.active_project_id || state.activeProjectId;
  state.activeSessionId = data.active_session_id || state.activeSessionId;
  if (!projectList) return;
  projectList.replaceChildren();
  state.projects.forEach((project) => {
    const group = element("section", "project-group");
    const title = element("div", "project-title");
    title.append(element("span", "", `▾ ${project.name}`));
    const memory = element("button", "project-memory", "记忆");
    memory.type = "button";
    memory.addEventListener("click", (event) => { event.stopPropagation(); openMemory("project", project.id).catch(reportError); });
    title.append(memory);
    const sessions = element("div", "session-list");
    (project.sessions || []).forEach((session) => {
      const button = element("button", `session-link${session.id === state.activeSessionId ? " active" : ""}`, session.title || "新会话");
      button.type = "button";
      button.title = session.title || "新会话";
      button.append(element("small", "", `${String(session.status || "idle").toUpperCase()} · ${session.verification || "NOT_RUN"}`));
      button.addEventListener("click", () => activateSession(project.id, session.id).catch(reportError));
      sessions.append(button);
    });
    const add = element("button", "new-session", "＋ 新建会话");
    add.type = "button";
    add.disabled = BUSY_STATUSES.has(state.status);
    add.addEventListener("click", () => createSession(project.id).catch(reportError));
    sessions.append(add);
    group.append(title, sessions);
    projectList.append(group);
  });
}

async function createSession(projectId) {
  const data = await postJson(`/api/projects/${encodeURIComponent(projectId)}/sessions`, {title: "新会话"});
  resetConversation();
  state.activeProjectId = projectId;
  state.activeSessionId = data.id;
  await loadSnapshot();
}

async function activateSession(projectId, sessionId) {
  if (sessionId === state.activeSessionId) return;
  await postJson(`/api/projects/${encodeURIComponent(projectId)}/sessions/${encodeURIComponent(sessionId)}/activate`, {});
  resetConversation();
  state.activeProjectId = projectId;
  state.activeSessionId = sessionId;
  await loadSnapshot();
}

async function registerProject() {
  const workspace = window.prompt("输入本地项目绝对路径");
  if (!workspace || !workspace.trim()) return;
  await postJson("/api/projects", {workspace: workspace.trim()});
  resetConversation();
  await loadSnapshot();
}

async function openMemory(scope, projectId = null) {
  state.memoryScope = scope;
  if (projectId) state.activeProjectId = projectId;
  $("#memory-scope-label").textContent = scope === "global" ? "GLOBAL" : "PROJECT";
  $("#memory-title").textContent = scope === "global" ? "全局记忆" : "项目记忆";
  memoryView.hidden = false;
  await loadMemories();
}

function closeMemory() {
  memoryView.hidden = true;
  state.memoryScope = null;
}

async function loadMemories() {
  if (!state.memoryScope) return;
  const suffix = state.memoryScope === "project" ? `&project_id=${encodeURIComponent(state.activeProjectId || "")}` : "";
  const response = await fetch(withToken(`/api/memories?scope=${state.memoryScope}${suffix}`));
  if (!response.ok) throw new Error(await response.text());
  const items = await response.json();
  memoryList.replaceChildren();
  if (!items.length) {
    memoryList.append(element("p", "memory-empty", "还没有长期记忆。"));
    return;
  }
  items.forEach((item) => {
    const card = element("article", "memory-card");
    const header = element("header");
    header.append(element("strong", "", item.title), element("span", "memory-badge", item.kind));
    const remove = element("button", "memory-forget", "忘记");
    remove.type = "button";
    remove.addEventListener("click", () => forgetMemory(item.id).catch(reportError));
    card.append(header, element("p", "", item.content), element("small", "", `${item.source === "auto" ? "自动提取" : "手动记住"} · ${item.updated_at || ""}`), remove);
    memoryList.append(card);
  });
}

async function forgetMemory(memoryId) {
  const projectQuery = state.memoryScope === "project" ? `project_id=${encodeURIComponent(state.activeProjectId || "")}` : "";
  const response = await fetch(withToken(`/api/memories/${encodeURIComponent(memoryId)}${projectQuery ? `?${projectQuery}` : ""}`), {method: "DELETE"});
  if (!response.ok) throw new Error(await response.text());
  await loadMemories();
}

async function submitMemory(event) {
  event.preventDefault();
  const title = $("#memory-item-title").value.trim();
  const content = $("#memory-content").value.trim();
  if (!title || !content || !state.memoryScope) return;
  await postJson("/api/memories", {
    scope: state.memoryScope,
    project_id: state.memoryScope === "project" ? state.activeProjectId : null,
    kind: $("#memory-kind").value,
    title,
    content,
  });
  $("#memory-item-title").value = "";
  $("#memory-content").value = "";
  await loadMemories();
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
  renderProjects(data);
  hydrateHistory(data.history, data.verification_status);
  (data.file_changes || []).forEach(rememberChange);
  state.references.clear();
  (data.references || []).map(referenceMetadata).forEach((reference) => state.references.set(reference.id, reference));
  renderReferences();
  if (data.pending_plan) {
    state.pendingPlan = data.pending_plan;
    if (state.current) renderPlan(data.pending_plan);
    else setStatus("WAITING_PLAN_APPROVAL");
  } else {
    setStatus(data.status);
  }
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

async function postJson(path, body) {
  const response = await fetch(withToken(path), {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const error = new Error(await response.text());
    error.status = response.status;
    throw error;
  }
  return response.json();
}

async function removeReference(referenceId) {
  const response = await fetch(withToken(`/api/references/${encodeURIComponent(referenceId)}`), {method: "DELETE"});
  if (!response.ok) throw new Error(await response.text());
  state.references.delete(referenceId);
  renderReferences();
  return response.json();
}

async function submitPrompt() {
  const text = promptInput.value.trim();
  if (!text) return;
  setStatus("RUNNING");
  try {
    await postJson("/api/prompts", {text, permission: permissionSelect.value, model: modelSelect.value});
  } catch (error) {
    reportError(error);
    await loadSnapshot();
    return;
  }
  promptInput.value = "";
}

function formatCompactChars(value) {
  const amount = Number(value);
  if (!Number.isFinite(amount)) return "?";
  if (amount >= 1_000_000) return `${(amount / 1_000_000).toFixed(1)}M`;
  if (amount >= 1_000) return `${(amount / 1_000).toFixed(1)}K`;
  return String(Math.round(amount));
}

async function interruptPrompt() {
  if (!INTERRUPTIBLE_STATUSES.has(state.status)) return;
  setStatus("STOPPING");
  try {
    await postJson("/api/interrupt", {});
  } catch (error) {
    await loadSnapshot();
    if (error.status === 409) return;
    throw error;
  }
}

async function primaryAction() {
  if (INTERRUPTIBLE_STATUSES.has(state.status)) return interruptPrompt();
  return submitPrompt();
}

async function changePermission() {
  try {
    const data = await postJson("/api/mode", {mode: permissionSelect.value});
    setPermission(data.mode);
  } catch (error) {
    await loadSnapshot();
    throw error;
  }
}

async function resolvePlan(action) {
  if (!state.pendingPlan) return;
  setStatus("RUNNING");
  try {
    await postJson(`/api/plans/${encodeURIComponent(state.pendingPlan.id)}/resolve`, {action});
  } catch (error) {
    await loadSnapshot();
    throw error;
  }
}

async function decideApproval(allow) {
  if (!state.pendingApprovalId) return;
  await postJson(`/api/approvals/${encodeURIComponent(state.pendingApprovalId)}`, {allow});
}

function reportError(error) {
  setStatus("ERROR");
  addActivity({ok: false, text: "连接或请求失败", detail: {message: error.message || String(error)}});
}

$("#prompt-form").addEventListener("submit", (event) => { event.preventDefault(); submitPrompt().catch(reportError); });
sendButton.addEventListener("click", () => primaryAction().catch(reportError));
promptInput.addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); submitPrompt().catch(reportError); } });
permissionSelect.addEventListener("change", () => changePermission().catch(reportError));
$("#close-review").addEventListener("click", closeReview);
$("#allow-command").addEventListener("click", () => decideApproval(true).catch(reportError));
$("#reject-command").addEventListener("click", () => decideApproval(false).catch(reportError));
approvalDialog.addEventListener("cancel", (event) => { event.preventDefault(); decideApproval(false).catch(reportError); });
conversationPane.addEventListener("scroll", () => {
  syncScrollButton();
  syncActiveConversationTurn();
});
scrollToBottomButton.addEventListener("click", () => scrollToBottom());
$("#new-project")?.addEventListener("click", () => registerProject().catch(reportError));
$("#global-memory")?.addEventListener("click", () => openMemory("global").catch(reportError));
$("#close-memory")?.addEventListener("click", closeMemory);
$("#memory-form")?.addEventListener("submit", (event) => submitMemory(event).catch(reportError));

window.MiniCodexApp = {handleEvent, setStatus, openReview, closeReview, isNearBottom, syncScrollButton};
loadSnapshot().then(() => connectEvents(0)).catch(reportError);
