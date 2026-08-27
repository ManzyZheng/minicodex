"use strict";

(function exposeMarkdown(root) {
  function decodeEntities(text) {
    const named = {amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " "};
    return String(text)
      .replace(/&#x([0-9a-f]+);/gi, (_match, value) => String.fromCodePoint(parseInt(value, 16)))
      .replace(/&#([0-9]+);/g, (_match, value) => String.fromCodePoint(parseInt(value, 10)))
      .replace(/&([a-z]+);/gi, (match, value) => named[value.toLowerCase()] ?? match);
  }

  function appendInline(parent, source) {
    const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*)/g;
    let cursor = 0;
    for (const match of String(source).matchAll(pattern)) {
      if (match.index > cursor) parent.append(document.createTextNode(decodeEntities(source.slice(cursor, match.index))));
      const token = match[0];
      const node = document.createElement(token.startsWith("`") ? "code" : "strong");
      node.textContent = decodeEntities(token.slice(token.startsWith("`") ? 1 : 2, token.startsWith("`") ? -1 : -2));
      parent.append(node);
      cursor = match.index + token.length;
    }
    if (cursor < source.length) parent.append(document.createTextNode(decodeEntities(source.slice(cursor))));
  }

  function tableCells(line) {
    return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
  }

  function isTableDivider(line) {
    const cells = tableCells(line);
    return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
  }

  function startsBlock(lines, index) {
    const line = lines[index] || "";
    return /^```/.test(line) || /^#{1,6}\s+/.test(line) || /^\s*([-*+] |\d+\. )/.test(line) ||
      (line.includes("|") && isTableDivider(lines[index + 1] || ""));
  }

  function renderMarkdown(container, source) {
    container.replaceChildren();
    const lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
    let index = 0;
    while (index < lines.length) {
      if (!lines[index].trim()) { index += 1; continue; }
      if (/^```/.test(lines[index])) {
        const language = lines[index].slice(3).trim();
        index += 1;
        const codeLines = [];
        while (index < lines.length && !/^```/.test(lines[index])) codeLines.push(lines[index++]);
        if (index < lines.length) index += 1;
        const pre = document.createElement("pre");
        const code = document.createElement("code");
        if (language) code.dataset.language = language;
        code.textContent = decodeEntities(codeLines.join("\n"));
        pre.append(code);
        container.append(pre);
        continue;
      }
      const heading = lines[index].match(/^(#{1,6})\s+(.+)$/);
      if (heading) {
        const node = document.createElement(`h${heading[1].length}`);
        appendInline(node, heading[2]);
        container.append(node);
        index += 1;
        continue;
      }
      if (lines[index].includes("|") && isTableDivider(lines[index + 1] || "")) {
        const table = document.createElement("table");
        const head = document.createElement("thead");
        const headRow = document.createElement("tr");
        tableCells(lines[index]).forEach((value) => { const cell = document.createElement("th"); appendInline(cell, value); headRow.append(cell); });
        head.append(headRow);
        table.append(head);
        index += 2;
        const body = document.createElement("tbody");
        while (index < lines.length && lines[index].trim() && lines[index].includes("|")) {
          const row = document.createElement("tr");
          tableCells(lines[index]).forEach((value) => { const cell = document.createElement("td"); appendInline(cell, value); row.append(cell); });
          body.append(row);
          index += 1;
        }
        table.append(body);
        container.append(table);
        continue;
      }
      const listMatch = lines[index].match(/^\s*([-*+] |\d+\. )(.+)$/);
      if (listMatch) {
        const ordered = /\d/.test(listMatch[1]);
        const list = document.createElement(ordered ? "ol" : "ul");
        while (index < lines.length) {
          const itemMatch = lines[index].match(/^\s*([-*+] |\d+\. )(.+)$/);
          if (!itemMatch || /\d/.test(itemMatch[1]) !== ordered) break;
          const item = document.createElement("li");
          appendInline(item, itemMatch[2]);
          list.append(item);
          index += 1;
        }
        container.append(list);
        continue;
      }
      const paragraph = [];
      while (index < lines.length && lines[index].trim() && !startsBlock(lines, index)) paragraph.push(lines[index++].trim());
      const node = document.createElement("p");
      appendInline(node, paragraph.join(" "));
      container.append(node);
    }
  }

  root.MiniCodexMarkdown = {decodeEntities, renderMarkdown};
})(window);
