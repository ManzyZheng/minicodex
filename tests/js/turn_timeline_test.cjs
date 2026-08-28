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
  constructor(tagName, id = "") { this.tagName = tagName.toUpperCase(); this.id = id; this.children = []; this.parentNode = null; this.className = ""; this.classList = new FakeClassList(this); this.dataset = {}; this._text = ""; this.open = false; this.hidden = false; this.disabled = false; this.value = ""; this.listeners = {}; }
  append(...children) { children.forEach((child) => { child.parentNode = this; this.children.push(child); }); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((child) => child !== this); }
  replaceChildren(...children) { this.children = []; this._text = ""; this.append(...children); }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  click() { (this.listeners.click || []).forEach((callback) => callback({preventDefault() {}})); }
  querySelectorAll(selector) { const selectors = selector.split(",").map((part) => part.trim()); const matches = []; const visit = (node) => { for (const child of node.children) { if (selectors.some((part) => part.startsWith(".") && child.classList.contains(part.slice(1)))) matches.push(child); visit(child); } }; visit(this); return matches; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  scrollIntoView() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
  set textContent(value) { this.children = []; this._text = String(value); }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
}

const nodes = {};
for (const id of ["conversation", "prompt-input", "send-button", "session-status", "approval-dialog", "app-layout", "workspace-name", "workspace-path", "verification-status", "approval-purpose", "approval-command", "approval-timeout", "approval-title", "permission-select", "model-select", "allow-command", "reject-command", "prompt-form", "review-panel", "review-file-list", "review-diff", "review-title", "close-review", "empty-state"]) nodes[id] = new FakeNode(id === "approval-dialog" ? "dialog" : "div", id);
nodes.conversation.append(nodes["empty-state"]);

global.window = { location: {search: "?token=test"}, MiniCodexMarkdown: {renderMarkdown(node, text) { node.textContent = text; }} };
global.document = { querySelector(selector) { return selector.startsWith("#") ? nodes[selector.slice(1)] || null : null; }, createElement(tagName) { return new FakeNode(tagName); } };
global.fetch = async () => ({ ok: true, async json() { return {workspace: "C:/demo", model: "demo", allowed_models: ["demo"], verification_status: "NOT_RUN", status: "IDLE", execution_mode: "auto-act", plan_state: "inactive", pending_plan: null, file_changes: [], event_id: 0}; }, async text() { return ""; } });
class FakeEventSource { constructor(url) { this.url = url; this.listeners = {}; FakeEventSource.instance = this; } addEventListener(name, callback) { this.listeners[name] = callback; } }
global.EventSource = FakeEventSource;

require(process.argv[2]);

setImmediate(() => {
  const app = window.MiniCodexApp;
  assert.ok(app);
  app.handleEvent("user_prompt", {prompt_index: 1, text: "修复测试"});
  app.handleEvent("progress", {text: "正在定位失败原因", turn: 1});
  app.handleEvent("model_reasoning", {content: "raw private reasoning"});
  app.handleEvent("tool_summary", {text: "已读取 app.py", detail: {call_id: "read"}});
  app.handleEvent("file_changed", {prompt_index: 1, path: "app.py", diff: "--- a/app.py\n+++ b/app.py\n-old\n+new\n", additions: 1, deletions: 1});
  app.handleEvent("final_answer", {text: "已修复。", turns: 4, verification_status: "VERIFIED"});
  app.handleEvent("turn_completed", {text: "已修复。", turns: 4, verification_status: "VERIFIED"});

  const turns = nodes.conversation.querySelectorAll(".turn");
  assert.equal(turns.length, 1);
  assert.equal(turns[0].querySelector(".process").open, false);
  assert.match(turns[0].querySelector(".final-answer").textContent, /已修复/);
  assert.doesNotMatch(nodes.conversation.textContent, /raw private reasoning/);
  assert.match(turns[0].textContent, /最终结果 · Turn 4/);
  turns[0].querySelector(".change-file").click();
  assert.equal(nodes["review-panel"].hidden, false);
  assert.match(nodes["review-diff"].textContent, /\+new/);

  app.handleEvent("user_prompt", {prompt_index: 2, text: "继续添加功能"});
  assert.equal(turns[0].open, false);
  app.handleEvent("plan_ready", {id: "plan-1", text: "## 方案\n\n修改 API", execution_mode: "auto-act"});
  const latest = nodes.conversation.querySelectorAll(".turn")[1];
  assert.match(latest.querySelector(".plan-card").textContent, /使用 AUTO-ACT 执行/);
  assert.match(latest.querySelector(".plan-card").textContent, /执行方案/);
  app.setStatus("RUNNING");
  assert.equal(nodes["permission-select"].disabled, true);
  assert.equal(nodes["model-select"].disabled, true);
  console.log("codex-style conversation and review: ok");
});
