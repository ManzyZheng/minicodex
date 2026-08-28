"use strict";

const assert = require("node:assert/strict");

class FakeClassList {
  constructor(node) { this.node = node; }
  add(...names) {
    const values = new Set(this.node.className.split(/\s+/).filter(Boolean));
    names.forEach((name) => values.add(name));
    this.node.className = [...values].join(" ");
  }
  contains(name) { return this.node.className.split(/\s+/).includes(name); }
}

class FakeNode {
  constructor(tagName, id = "") {
    this.tagName = tagName.toUpperCase();
    this.id = id;
    this.children = [];
    this.parentNode = null;
    this.className = "";
    this.classList = new FakeClassList(this);
    this.dataset = {};
    this._text = "";
    this.open = false;
    this.disabled = false;
    this.value = "";
    this.listeners = {};
  }
  append(...children) {
    children.forEach((child) => { child.parentNode = this; this.children.push(child); });
  }
  remove() {
    if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((child) => child !== this);
  }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  querySelectorAll(selector) {
    const selectors = selector.split(",").map((part) => part.trim());
    const matches = [];
    const visit = (node) => {
      for (const child of node.children) {
        if (selectors.some((part) => part.startsWith(".") && child.classList.contains(part.slice(1)))) matches.push(child);
        visit(child);
      }
    };
    visit(this);
    return matches;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  scrollIntoView() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
  set textContent(value) { this.children = []; this._text = String(value); }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
}

const nodes = {};
for (const id of [
  "timeline", "prompt-input", "send-button", "session-status", "approval-dialog",
  "workspace-path", "model-name", "verification-status", "approval-purpose",
  "approval-command", "approval-timeout", "approval-title", "mode-select",
  "allow-command", "reject-command", "prompt-form",
]) nodes[id] = new FakeNode(id === "approval-dialog" ? "dialog" : "div", id);
const empty = new FakeNode("article", "empty-state");
nodes.timeline.append(empty);

global.window = {
  location: {search: "?token=test"},
  MiniCodexMarkdown: {renderMarkdown(node, text) { node.textContent = text; }},
};
global.document = {
  querySelector(selector) { return selector.startsWith("#") ? nodes[selector.slice(1)] || null : null; },
  createElement(tagName) { return new FakeNode(tagName); },
};
global.fetch = async () => ({
  ok: true,
  async json() {
    return {workspace: "C:/demo", model: "demo", verification_status: "NOT_RUN", status: "IDLE", mode: "act", event_id: 7};
  },
});

class FakeEventSource {
  constructor(url) { this.url = url; this.listeners = {}; FakeEventSource.instance = this; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  emit(name, data, id) {
    assert.ok(this.listeners[name], `missing frontend handler for ${name}`);
    this.listeners[name]({data: JSON.stringify(data), lastEventId: String(id)});
  }
}
global.EventSource = FakeEventSource;

require(process.argv[2]);

setImmediate(() => {
  const source = FakeEventSource.instance;
  assert.match(source.url, /after=0(?:&|$)/, "refresh should replay retained events to rebuild turn history");
  source.emit("user_prompt", {prompt_index: 1, text: "fix the bug"}, 1);
  source.emit("model_reasoning", {turn: 1, content: "Need inspect the failing tests."}, 2);
  source.emit("model_message", {turn: 1, content: "I will inspect the tests."}, 3);
  source.emit("tool_result", {ok: true, tool: "read_file", summary: "read 10 characters"}, 4);
  source.emit("turn_completed", {turns: 2, text: "First result", verification_status: "VERIFIED"}, 5);
  source.emit("mode_changed", {from: "act", to: "plan"}, 6);
  source.emit("user_prompt", {prompt_index: 2, text: "add export"}, 7);
  source.emit("model_reasoning", {turn: 1, content: "Need inspect the export flow."}, 8);
  source.emit("tool_result", {ok: true, tool: "read_file", summary: "read tracker.py"}, 9);
  source.emit("turn_completed", {turns: 3, text: "Second result", verification_status: "NOT_RUN"}, 10);

  const groups = nodes.timeline.querySelectorAll(".turn-group");
  assert.equal(groups.length, 2, "events should be grouped by user prompt");
  assert.equal(groups[0].open, false, "starting a new prompt should collapse the previous turn");
  assert.equal(groups[1].open, true, "the latest turn should remain expanded");

  const latestProcess = groups[1].querySelector(".turn-process");
  const latestFinal = groups[1].querySelector(".turn-final");
  assert.equal(latestProcess.open, false, "completed execution details should be collapsed");
  assert.match(latestProcess.textContent, /add export/, "the full prompt should collapse with execution details");
  assert.match(latestProcess.textContent, /Need inspect the export flow/);
  assert.match(latestFinal.textContent, /Second result/);
  assert.match(groups[1].querySelector(".turn-summary").textContent, /PROMPT 2/);
  assert.match(groups[1].querySelector(".turn-summary").textContent, /TURN 3/);
  assert.doesNotMatch(latestFinal.textContent, /read tracker\.py/, "tool trace must stay outside the final answer");
  assert.doesNotMatch(latestFinal.textContent, /Need inspect the export flow/, "reasoning must stay outside the final answer");
  assert.equal(nodes["mode-select"].value, "plan");
  assert.ok(latestFinal.querySelector(".plan-actions"), "a completed plan should expose implementation choices");

  source.emit("approval_required", {
    request_id: "file-1", kind: "file_change", summary: "edit app.py", reason: "ACT requires review",
    risk: "medium", details: {path: "app.py", diff: "-old\n+new\n"}, approval_timeout_sec: 300,
  }, 11);
  assert.equal(nodes["approval-dialog"].open, true);
  assert.match(nodes["approval-command"].textContent, /\+new/);
  console.log("turn timeline grouping: ok");
});
