"use strict";

const assert = require("node:assert/strict");

class FakeClassList {
  constructor(node) { this.node = node; }
  add(...names) { const values = new Set(this.node.className.split(/\s+/).filter(Boolean)); names.forEach((name) => values.add(name)); this.node.className = [...values].join(" "); }
  remove(...names) { const removed = new Set(names); this.node.className = this.node.className.split(/\s+/).filter((name) => name && !removed.has(name)).join(" "); }
  toggle(name, force) { const active = force === undefined ? !this.contains(name) : force; active ? this.add(name) : this.remove(name); return active; }
  contains(name) { return this.node.className.split(/\s+/).includes(name); }
}

class FakeNode {
  constructor(tagName, id = "") { this.tagName = tagName ? tagName.toUpperCase() : null; this.id = id; this.children = []; this.parentNode = null; this.className = ""; this.classList = new FakeClassList(this); this.dataset = {}; this._text = ""; this.open = false; this.hidden = false; this.disabled = false; this.value = ""; this.listeners = {}; this.scrollTop = 0; this.scrollHeight = 0; this.clientHeight = 0; this.scrollCalls = 0; this.scrollIntoViewCalls = 0; }
  append(...children) { children.forEach((child) => { child.parentNode = this; this.children.push(child); }); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((child) => child !== this); }
  replaceChildren(...children) { this.children = []; this._text = ""; this.append(...children); }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  dispatch(name) { (this.listeners[name] || []).forEach((callback) => callback({preventDefault() {}})); }
  click() { (this.listeners.click || []).forEach((callback) => callback({preventDefault() {}})); }
  querySelectorAll(selector) { const selectors = selector.split(",").map((part) => part.trim()); const matches = []; const visit = (node) => { for (const child of node.children) { if (selectors.some((part) => part.startsWith(".") && child.classList.contains(part.slice(1)))) matches.push(child); visit(child); } }; visit(this); return matches; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  scrollIntoView() { this.scrollIntoViewCalls += 1; }
  scrollTo(options) { this.scrollCalls += 1; this.scrollTop = options.top; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  set textContent(value) { this.children = []; this._text = String(value); }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
}

const nodes = {};
for (const id of ["conversation", "conversation-scroll", "conversation-nav", "scroll-to-bottom", "prompt-input", "send-button", "session-status", "approval-dialog", "app-layout", "workspace-name", "workspace-path", "verification-status", "approval-purpose", "approval-command", "approval-timeout", "approval-title", "permission-select", "model-select", "allow-command", "reject-command", "prompt-form", "review-panel", "review-file-list", "review-diff", "review-title", "close-review", "empty-state", "session-references", "reference-summary", "reference-list"]) nodes[id] = new FakeNode(id === "approval-dialog" ? "dialog" : "div", id);
nodes.conversation.append(nodes["empty-state"]);
nodes["conversation-scroll"].clientHeight = 400;
nodes["conversation-scroll"].scrollHeight = 1200;

global.window = { location: {search: "?token=test"} };
global.document = { querySelector(selector) { return selector.startsWith("#") ? nodes[selector.slice(1)] || null : null; }, createElement(tagName) { return new FakeNode(tagName); }, createTextNode(text) { const node = new FakeNode(null); node.textContent = text; return node; } };
const fetchCalls = [];
let interruptStatus = 200;
global.fetch = async (url, options = {}) => {
  fetchCalls.push({url, options});
  const status = url.includes("/api/interrupt") ? interruptStatus : 200;
  return {
    ok: status < 400,
    status,
    async json() { return {workspace: "C:/demo", model: "demo", allowed_models: ["demo"], verification_status: "NOT_RUN", status: "IDLE", execution_mode: "auto-act", plan_state: "inactive", pending_plan: null, file_changes: [], references: [], event_id: 0}; },
    async text() { return `status ${status}`; },
  };
};
class FakeEventSource { constructor(url) { this.url = url; this.listeners = {}; FakeEventSource.instance = this; } addEventListener(name, callback) { this.listeners[name] = callback; } }
global.EventSource = FakeEventSource;

require(process.argv[2]);
require(process.argv[3]);

setImmediate(async () => {
  const app = window.MiniCodexApp;
  assert.ok(app);
  app.handleEvent("context_loaded", {id: "ref-1", name: "api.md", path: "D:/docs/api.md", scope: "external", size: 2048, access: "read-only-session-snapshot", content: "MUST_NOT_RENDER"});
  app.handleEvent("user_prompt", {prompt_index: 1, text: "修复测试", event_timestamp: "2026-08-27T10:00:00Z"});
  app.handleEvent("progress", {text: "正在定位失败原因", turn: 1});
  app.handleEvent("model_reasoning", {content: "raw private reasoning"});
  for (let index = 1; index <= 12; index += 1) {
    app.handleEvent("tool_summary", {text: `已读取 file_${index}.py`, tool: "read_file", turn: 1, detail: {call_id: `read-${index}`}});
  }
  app.handleEvent("progress", {text: "正在实现 Streams 协议", turn: 2});
  app.handleEvent("tool_summary", {text: "已修改 streams.py", tool: "edit_file", turn: 2, detail: {call_id: "edit-streams"}});
  app.handleEvent("progress", {text: "正在收紧 stderr 断言", turn: 3});
  app.handleEvent("command_summary", {text: "验证通过", turn: 3, detail: {call_id: "pytest"}});
  for (let index = 1; index <= 4; index += 1) {
    app.handleEvent("file_changed", {prompt_index: 1, path: `app_${index}.py`, diff: `diff --git a/app_${index}.py b/app_${index}.py\nindex 1111111..2222222 100644\n--- a/app_${index}.py\n+++ b/app_${index}.py\n-old\n+new_${index}\n`, additions: index, deletions: 1});
  }
  app.handleEvent("final_answer", {text: "已修复。", turns: 4, verification_status: "VERIFIED"});
  app.handleEvent("turn_completed", {text: "已修复。", turns: 4, verification_status: "VERIFIED", event_timestamp: "2026-08-27T10:17:10Z"});

  const turns = nodes.conversation.querySelectorAll(".turn");
  assert.equal(turns.length, 1);
  assert.match(turns[0].querySelector(".reference-chip").textContent, /api.md/);
  assert.doesNotMatch(turns[0].textContent, /MUST_NOT_RENDER/);
  assert.equal(nodes["session-references"].hidden, false);
  assert.match(nodes["reference-summary"].textContent, /本会话参考 · 1/);
  assert.match(nodes["reference-list"].textContent, /外部只读/);
  assert.doesNotMatch(nodes["reference-list"].textContent, /MUST_NOT_RENDER/);
  assert.equal(turns[0].querySelector(".process").open, false);
  const modelSteps = turns[0].querySelectorAll(".model-step");
  assert.equal(modelSteps.length, 3);
  assert.equal(modelSteps[0].hidden, true);
  assert.equal(modelSteps[1].hidden, false);
  assert.equal(modelSteps[2].hidden, false);
  assert.match(modelSteps[0].textContent, /Turn 1/);
  assert.match(modelSteps[0].querySelector(".activity-item").textContent, /读取文件 · 12/);
  assert.match(modelSteps[1].textContent, /Turn 2.*Streams/);
  assert.match(modelSteps[1].querySelector(".activity-item").textContent, /修改文件/);
  assert.match(modelSteps[2].textContent, /Turn 3.*stderr/);
  assert.match(modelSteps[2].querySelector(".activity-item").textContent, /验证通过/);
  assert.match(turns[0].querySelector(".process").textContent, /用时 17分10秒 · 3 个执行阶段/);
  const earlierToggle = turns[0].querySelector(".model-turns-toggle");
  assert.match(earlierToggle.textContent, /显示更早 1 个执行阶段/);
  earlierToggle.click();
  assert.equal(modelSteps[0].hidden, false);
  assert.match(turns[0].querySelector(".final-answer").textContent, /已修复/);
  assert.doesNotMatch(nodes.conversation.textContent, /raw private reasoning/);
  assert.match(turns[0].textContent, /Model Turn 4 · VERIFIED/);
  assert.equal(turns[0].querySelectorAll(".change-file").length, 3);
  assert.match(turns[0].querySelector(".changes-toggle").textContent, /再显示 1 个文件/);
  turns[0].querySelector(".changes-toggle").click();
  assert.equal(turns[0].querySelectorAll(".change-file").length, 4);
  turns[0].querySelectorAll(".change-file")[3].click();
  assert.equal(nodes["review-panel"].hidden, false);
  assert.match(nodes["review-diff"].textContent, /\+new_4/);
  assert.equal(nodes["review-diff"].children[0].classList.contains("header"), true);
  assert.equal(nodes["review-diff"].children[1].classList.contains("header"), true);

  app.handleEvent("user_prompt", {prompt_index: 2, text: "继续添加功能"});
  assert.equal(turns[0].tagName, "ARTICLE");
  assert.equal(turns[0].querySelector(".turn-summary"), null);
  assert.match(turns[0].querySelector(".user-message").textContent, /修复测试/);
  assert.match(turns[0].querySelector(".final-answer").textContent, /已修复/);
  app.handleEvent("plan_ready", {id: "plan-1", text: "## 方案\n\n修改 API", execution_mode: "auto-act"});
  const latest = nodes.conversation.querySelectorAll(".turn")[1];
  assert.match(latest.querySelector(".process").textContent, /已用时 .* · 0 个执行阶段/);
  assert.match(latest.querySelector(".plan-card").textContent, /使用 AUTO-ACT 执行/);
  assert.match(latest.querySelector(".plan-card").textContent, /执行方案/);

  app.handleEvent("user_prompt", {prompt_index: 3, text: "只检查当前实现"});
  app.handleEvent("final_answer", {text: "检查完成。", turns: 2, verification_status: "VERIFIED"});
  app.handleEvent("turn_completed", {text: "检查完成。", turns: 2, verification_status: "VERIFIED"});
  const noChangeTurn = nodes.conversation.querySelectorAll(".turn")[2];
  assert.match(noChangeTurn.querySelector(".no-changes").textContent, /本轮未修改文件/);
  app.handleEvent("user_prompt", {prompt_index: 4, text: "继续长会话"});
  app.handleEvent("tool_summary", {text: "已读取 config.py", tool: "read_file", turn: 1, detail: {call_id: "read-config", data: {path: "config.py"}}});
  app.handleEvent("context_compacted", {before_messages: 28, after_messages: 15, before_chars: 111300, after_chars: 58000, stages: ["budget", "stale_snip", "auto_compact"]});
  const compactedTurn = nodes.conversation.querySelectorAll(".turn")[3];
  assert.equal(compactedTurn.querySelector(".model-step-progress").textContent, "");
  assert.match(compactedTurn.querySelector(".process").textContent, /上下文已压缩 · 111\.3K → 58\.0K 字符/);
  const promptMarkers = nodes["conversation-nav"].querySelectorAll(".conversation-nav-marker");
  assert.equal(promptMarkers.length, 4);
  assert.match(promptMarkers[0].textContent, /对话 1.*修复测试/);
  promptMarkers[0].click();
  assert.equal(turns[0].scrollIntoViewCalls, 1);
  assert.equal(promptMarkers[0].classList.contains("active"), true);
  app.setStatus("RUNNING");
  assert.equal(nodes["permission-select"].disabled, true);
  assert.equal(nodes["model-select"].disabled, true);
  assert.equal(nodes["send-button"].disabled, false);
  assert.equal(nodes["send-button"].textContent, "■");
  nodes["send-button"].click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(fetchCalls.some((call) => call.options.method === "POST" && call.url.includes("/api/interrupt")));
  assert.equal(nodes["send-button"].textContent, "■");

  nodes["conversation-scroll"].scrollTop = 100;
  nodes["conversation-scroll"].dispatch("scroll");
  assert.equal(nodes["scroll-to-bottom"].hidden, false);
  const callsBefore = nodes["conversation-scroll"].scrollCalls;
  app.handleEvent("progress", {text: "后台仍在运行", turn: 2});
  assert.equal(nodes["conversation-scroll"].scrollCalls, callsBefore);
  nodes["scroll-to-bottom"].click();
  assert.equal(nodes["conversation-scroll"].scrollTop, 1200);
  assert.equal(nodes["scroll-to-bottom"].hidden, true);
  nodes["reference-list"].querySelector(".reference-remove").click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(fetchCalls.some((call) => call.options.method === "DELETE" && call.url.includes("/api/references/ref-1")));

  interruptStatus = 409;
  app.setStatus("RUNNING");
  nodes["send-button"].click();
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(nodes["session-status"].textContent, "IDLE");
  assert.equal(nodes["send-button"].textContent, "↑");
  console.log("codex-style conversation and review: ok");
});
