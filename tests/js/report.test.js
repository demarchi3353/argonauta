const test = require("node:test");
const assert = require("node:assert/strict");
const { findingHtml, countsLine, summaryLine, newLineIndex, esc } = require("../../argonauta/web/static/report.js");

const EVIL = `<script>alert("x")</script>'"&`;

test("findingHtml escapes every model-supplied field", () => {
  const review = { omitted_files: [], comments: [] };
  const html = findingHtml(review, { kind: "verdict", verdict: "agree", title: EVIL, body: EVIL, quote: EVIL, author: EVIL, path: `a"><script>x</script>.py`, line: 3 });
  assert.ok(!html.includes("<script>"), html);
  assert.ok(html.includes(esc(EVIL)));
  assert.ok(html.includes('data-rpath="a&quot;&gt;&lt;script&gt;x&lt;/script&gt;.py"'));
  assert.ok(html.includes('data-rline="3"'));
});

test("findingHtml only trusts known verdicts, severities and integer lines", () => {
  const html = findingHtml({}, { kind: "verdict", verdict: 'agree" onclick="x', severity: "<b>", title: "t", path: "p", line: '1" onclick="x' });
  assert.ok(!html.includes("onclick") && !html.includes("<b>") && !html.includes("data-rline"), html);
  const omitted = findingHtml({ omitted_files: ["p"] }, { title: "t", path: "p" });
  assert.ok(omitted.includes("File not sent"));
});

test("countsLine and summaryLine count verdicts or severities", () => {
  const received = { mode: "received", summary: "Mostly right.", findings: [{ verdict: "agree" }, { verdict: "partly" }, { verdict: "disagree" }, { verdict: "disagree" }, { severity: "high" }] };
  assert.equal(summaryLine(received), "1 agree · 1 partly · 2 disagree — Mostly right.");
  const code = { mode: "code", summary: "", findings: [{ severity: "low" }, { severity: "high" }, { severity: "low", title: "Not everything was sent" }] };
  assert.equal(countsLine(code), "1 high · 1 low");
  assert.equal(summaryLine({ mode: "mine", summary: "s", findings: [] }), "s");
});

test("newLineIndex walks hunk headers and skips removed lines", () => {
  const diff = "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three\n@@ -10,2 +10,3 @@\n ten\n+new\n eleven\n";
  const rows = diff.split("\n");
  assert.equal(rows[newLineIndex(diff, 2)], "+TWO");
  assert.equal(rows[newLineIndex(diff, 3)], " three");
  assert.equal(rows[newLineIndex(diff, 11)], "+new");
  assert.equal(newLineIndex(diff, 5), -1);
  assert.equal(newLineIndex(diff, null), -1);
});
