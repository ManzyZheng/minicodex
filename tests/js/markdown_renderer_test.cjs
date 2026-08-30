"use strict";

const assert = require("node:assert/strict");

class FakeNode {
  constructor(tagName = null, text = "") {
    this.tagName = tagName ? tagName.toUpperCase() : null;
    this.children = [];
    this.dataset = {};
    this.className = "";
    this._text = String(text);
  }

  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = [...children]; this._text = ""; }
  set textContent(value) { this.children = []; this._text = String(value); }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
}

global.window = {};
global.document = {
  createElement: (tagName) => new FakeNode(tagName),
  createTextNode: (text) => new FakeNode(null, text),
};

require(process.argv[2]);
const markdown = window.MiniCodexMarkdown;
const mode = process.argv[3];
const tags = (node) => [node.tagName, ...node.children.flatMap(tags)].filter(Boolean);

if (mode === "normal") {
  const box = new FakeNode("div");
  markdown.renderMarkdown(box, "## Result\n\n**fixed** and `tested`\n\n| Step | Result |\n| --- | --- |\n| tests | 2 passed |\n\n```diff\n-old\n+new\n```\n\n&#60;script&#62;");
  const renderedTags = tags(box);
  for (const expected of ["H2", "STRONG", "TABLE", "PRE", "CODE"]) assert(renderedTags.includes(expected));
  assert(!renderedTags.includes("SCRIPT"));
  assert(box.textContent.includes("<script>"));
} else if (mode === "empty-markers") {
  const box = new FakeNode("div");
  markdown.renderMarkdown(box, "# \n\n- \n\n1. \n\n\t");
  assert(box.textContent.includes("#"));
  assert(box.textContent.includes("-"));
  assert(box.textContent.includes("1."));
} else if (mode === "invalid-entities") {
  assert.equal(markdown.decodeEntities("x&#x110000;y"), "x&#x110000;y");
  assert.equal(markdown.decodeEntities("x&#999999999999999999999;y"), "x&#999999999999999999999;y");
  assert.equal(markdown.decodeEntities("A&#x20;B"), "A B");
} else if (mode === "diff-lines") {
  const box = new FakeNode("div");
  markdown.renderMarkdown(box, "```diff\ndiff --git a/app.py b/app.py\n@@ -1 +1 @@\n-old <script>\n+new <script>\n context\n```");
  const code = box.children[0].children[0];
  assert.equal(code.dataset.language, "diff");
  assert.deepEqual(
    code.children.map((line) => line.className),
    ["md-diff-line header", "md-diff-line header", "md-diff-line remove", "md-diff-line add", "md-diff-line context"],
  );
  assert.equal(code.children[2].textContent, "-old <script>");
  assert(!tags(code).includes("SCRIPT"));
} else {
  throw new Error(`unknown mode: ${mode}`);
}

console.log(`markdown ${mode}: ok`);
