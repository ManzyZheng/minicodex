"use strict";

const $ = (selector) => document.querySelector(selector);
const timeline = $("#timeline");
const promptInput = $("#prompt-input");
const sendButton = $("#send-button");
const statusNode = $("#session-status");
const approvalDialog = $("#approval-dialog");
let pendingApprovalId = null;
const sessionToken = new URLSearchParams(window.location.search).get("token") || "";
const withToken = (path) => `${path}${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(sessionToken)}`;

function setStatus(value) {
  statusNode.textContent = value;
  statusNode.dataset.status = value;
  const busy = value !== "IDLE";
  promptInput.disabled = busy;
  sendButton.disabled = busy;
}

function addCard(kind, title, body, mode = "text") {
  const empty = $("#empty-state");
  if (empty) empty.remove();
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
    const content = document.createElement(mode === "code" ? "pre" : "p");
    content.className = mode === "code" ? "event-code" : "event-body";
    content.textContent = typeof body === "string" ? body : JSON.stringify(body, null, 2);
    card.append(content);
  }
  timeline.append(card);
  card.scrollIntoView({behavior:"smooth", block:"nearest"});
}

function commandOutput(data) {
  const argv = JSON.stringify(data.argv || [], null, 2);
  const output = [`argv = ${argv}`, data.stdout || "", data.stderr ? `[stderr]\n${data.stderr}` : "", `exit code: ${data.exit_code}`].filter(Boolean).join("\n");
  addCard(data.exit_code === 0 ? "command_output" : "error", "COMMAND OUTPUT", output, "code");
}

function showApproval(data, recordEvent = true) {
  pendingApprovalId = data.request_id;
  setStatus("WAITING_APPROVAL");
  $("#approval-purpose").textContent = data.purpose || "Agent 请求执行命令";
  $("#approval-command").textContent = JSON.stringify(data.argv || [], null, 2);
  $("#approval-timeout").textContent = `命令上限 ${data.timeout_sec}s · 审批等待 ${data.approval_timeout_sec}s`;
  if (!approvalDialog.open) approvalDialog.showModal();
  if (recordEvent) addCard("approval_required", "APPROVAL REQUIRED", JSON.stringify(data.argv || [], null, 2), "code");
}

const handlers = {
  session_started(data) { $("#workspace-path").textContent = data.workspace || "—"; $("#model-name").textContent = data.model || "—"; },
  status(data) { setStatus(data.value); },
  user_prompt(data) { addCard("user_prompt", `USER · PROMPT ${data.prompt_index || ""}`, data.text); },
  model_message(data) { addCard("model_message", `AGENT · TURN ${data.turn || "—"}`, data.content); },
  tool_call(data) { addCard("tool_call", `TOOL CALL · ${data.name}`, JSON.stringify(data.arguments || {}, null, 2), "code"); },
  tool_result(data) {
    const detail = data.ok ? data.summary : (data.error || data);
    addCard(data.ok ? "tool_result" : "error", `${data.ok ? "TOOL OK" : "TOOL FAILED"} · ${data.tool || "tool"}`, detail, data.ok ? "text" : "code");
  },
  diff(data) { addCard("diff", `DIFF · ${data.path || "file"}`, data.diff || "", "diff"); },
  command_output: commandOutput,
  verification(data) { $("#verification-status").textContent = data.status || "NOT_RUN"; addCard("verification", "VERIFICATION", data.status || "NOT_RUN"); },
  approval_required: showApproval,
  approval_resolved(data) { if (approvalDialog.open) approvalDialog.close(); pendingApprovalId = null; setStatus("RUNNING"); addCard("approval_resolved", "APPROVAL RESOLVED", data.reason || "resolved"); },
  turn_completed(data) { $("#verification-status").textContent = data.verification_status || "NOT_RUN"; addCard("turn_completed", `TURN COMPLETE · ${data.stop_reason}`, data.text || "任务结束"); },
  error(data) { addCard("error", `ERROR · ${data.code || "UNKNOWN"}`, data.message || data, "code"); },
};

async function loadSnapshot() {
  const response = await fetch(withToken("/api/session"));
  if (!response.ok) throw new Error(`session snapshot failed: ${response.status}`);
  const data = await response.json();
  $("#workspace-path").textContent = data.workspace;
  $("#model-name").textContent = data.model;
  $("#verification-status").textContent = data.verification_status;
  setStatus(data.status);
  if (data.pending_approval) showApproval(data.pending_approval, false);
}

function connectEvents() {
  const source = new EventSource(withToken("/api/events"));
  Object.entries(handlers).forEach(([name, handler]) => source.addEventListener(name, (event) => handler(JSON.parse(event.data))));
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

function reportError(error) { setStatus("ERROR"); addCard("error", "CONNECTION ERROR", error.message || String(error), "code"); }
$("#prompt-form").addEventListener("submit", (event) => { event.preventDefault(); submitPrompt().catch(reportError); });
promptInput.addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); submitPrompt().catch(reportError); } });
$("#allow-command").addEventListener("click", () => decideApproval(true).catch(reportError));
$("#reject-command").addEventListener("click", () => decideApproval(false).catch(reportError));
approvalDialog.addEventListener("cancel", (event) => { event.preventDefault(); decideApproval(false).catch(reportError); });
loadSnapshot().then(connectEvents).catch(reportError);
