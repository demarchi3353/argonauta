const test = require("node:test");
const assert = require("node:assert/strict");
const { splitPatch, mergeFileStats } = require("../../argonauta/web/static/patch.js");

const MODIFIED = `diff --git a/a.py b/a.py
index 1111111..2222222 100644
--- a/a.py
+++ b/a.py
@@ -1,2 +1,2 @@
-x = 1
+x = 2
 y = 3
`;
const ADDED = `diff --git a/new.txt b/new.txt
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/new.txt
@@ -0,0 +1 @@
+hello
`;
const DELETED = `diff --git a/old.txt b/old.txt
deleted file mode 100644
index 4444444..0000000
--- a/old.txt
+++ /dev/null
@@ -1 +0,0 @@
-bye
`;
const RENAMED = `diff --git a/src/one.py b/src/two.py
similarity index 100%
rename from src/one.py
rename to src/two.py
`;
const BINARY = `diff --git a/logo.png b/logo.png
new file mode 100644
index 0000000..5555555
Binary files /dev/null and b/logo.png differ
`;
const MODE_ONLY = `diff --git a/run.sh b/run.sh
old mode 100644
new mode 100755
`;

test("empty or whitespace patch yields no files", () => {
  assert.deepEqual(splitPatch(""), []);
  assert.deepEqual(splitPatch("\n  \n"), []);
});

test("one modified file", () => {
  const files = splitPatch(MODIFIED);
  assert.equal(files.length, 1);
  assert.equal(files[0].path, "a.py");
  assert.equal(files[0].status, "M");
  assert.equal(files[0].binary, false);
  assert.ok(files[0].text.startsWith("diff --git a/a.py b/a.py"));
  assert.ok(files[0].text.includes("+x = 2"));
});

test("added, deleted, renamed, binary and mode-only entries keep order and status", () => {
  const files = splitPatch(MODIFIED + ADDED + DELETED + RENAMED + BINARY + MODE_ONLY);
  assert.deepEqual(files.map((f) => [f.path, f.status, f.binary]), [
    ["a.py", "M", false],
    ["new.txt", "A", false],
    ["old.txt", "D", false],
    ["src/two.py", "R", false],
    ["logo.png", "A", true],
    ["run.sh", "T", false],
  ]);
  assert.equal(files[3].oldPath, "src/one.py");
  assert.equal(files[0].oldPath, null);
});

test("a line inside a hunk that starts with 'diff --git' does not split the file", () => {
  const tricky = MODIFIED.replace("+x = 2", "+x = 2\n+diff --git a/fake b/fake");
  assert.equal(splitPatch(tricky).length, 1);
});

test("each chunk text is reconstructable to the original patch", () => {
  const all = MODIFIED + ADDED + DELETED;
  assert.equal(splitPatch(all).map((f) => f.text).join(""), all);
});

test("a merge commit with no textual changes yields no files", () => {
  assert.deepEqual(splitPatch("commit message only, no diff --git lines\n"), []);
});

// captured from `git show --patch --numstat` on a commit with a binary, a quoted
// non-ASCII path and a rename with an edit
const REAL = 'diff --git a/bin.dat b/bin.dat\nnew file mode 100644\nindex 0000000..bdc955b\nBinary files /dev/null and b/bin.dat differ\n' +
  'diff --git "a/configura\\303\\247\\303\\243o.md" "b/configura\\303\\247\\303\\243o.md"\nindex c1b0730..5aad937 100644\n--- "a/configura\\303\\247\\303\\243o.md"\n+++ "b/configura\\303\\247\\303\\243o.md"\n@@ -1 +1 @@\n-x\n\\ No newline at end of file\n+xy\n' +
  "diff --git a/src/old.py b/src/new.py\nsimilarity index 97%\nrename from src/old.py\nrename to src/new.py\nindex 96cc558..1c5a36f 100644\n--- a/src/old.py\n+++ b/src/new.py\n@@ -48,3 +48,4 @@\n 48\n 49\n 50\n+51\n";
const REAL_STATS = [
  { status: "A", path: "bin.dat", insertions: 0, deletions: 0 },
  { status: "M", path: '"configura\\303\\247\\303\\243o.md"', insertions: 1, deletions: 1 },
  { status: "M", path: "src/{old.py => new.py}", insertions: 1, deletions: 0 },
];

test("a rename with edits takes its paths from rename from/to", () => {
  const [f] = splitPatch("diff --git a/src/a.py b/lib/b.py\nsimilarity index 90%\nrename from src/a.py\nrename to lib/b.py\nindex 1..2 100644\n--- a/src/a.py\n+++ b/lib/b.py\n@@ -1 +1 @@\n-a\n+b\n");
  assert.deepEqual([f.path, f.oldPath, f.status], ["lib/b.py", "src/a.py", "R"]);
});

test("quoted non-ASCII paths are decoded in the header, ---/+++ and rename lines", () => {
  const files = splitPatch(REAL);
  assert.deepEqual(files.map((f) => [f.path, f.status, f.binary]), [
    ["bin.dat", "A", true],
    ["configuração.md", "M", false],
    ["src/new.py", "R", false],
  ]);
  assert.equal(files[2].oldPath, "src/old.py");
  const [r] = splitPatch('diff --git "a/caf\\303\\251 \\"x\\".txt" "b/d\\303\\251j\\303\\240\\tvu.txt"\nsimilarity index 100%\nrename from "caf\\303\\251 \\"x\\".txt"\nrename to "d\\303\\251j\\303\\240\\tvu.txt"\n');
  assert.deepEqual([r.path, r.oldPath], ["déjà\tvu.txt", 'café "x".txt']);
  const [d] = splitPatch('diff --git "a/\\303\\251\\\\n.txt" "b/\\303\\251\\\\n.txt"\ndeleted file mode 100644\nindex 1..0\n--- "a/\\303\\251\\\\n.txt"\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n');
  assert.deepEqual([d.path, d.status], ["é\\n.txt", "D"]);
});

test("mergeFileStats matches quoted and numstat-rename stat paths to their chunks", () => {
  const rows = mergeFileStats(splitPatch(REAL), REAL_STATS);
  assert.deepEqual(rows.map((r) => [r.path, r.status, r.chunkIndex, r.insertions, r.deletions]), [
    ["bin.dat", "A", 0, 0, 0],
    ["configuração.md", "M", 1, 1, 1],
    ["src/new.py", "R", 2, 1, 0],
  ]);
  assert.equal(rows[2].oldPath, "src/old.py");
  assert.equal(rows[0].binary, true);
});

test("numstat rename forms map to the new path", () => {
  const chunks = splitPatch("diff --git a/a.txt b/b.txt\nsimilarity index 100%\nrename from a.txt\nrename to b.txt\n" +
    "diff --git a/x.py b/pkg/x.py\nsimilarity index 100%\nrename from x.py\nrename to pkg/x.py\n");
  const rows = mergeFileStats(chunks, [
    { status: "M", path: "a.txt => b.txt", insertions: 0, deletions: 0 },
    { status: "M", path: "{ => pkg}/x.py", insertions: 0, deletions: 0 },
  ]);
  assert.deepEqual(rows.map((r) => [r.path, r.chunkIndex]), [["b.txt", 0], ["pkg/x.py", 1]]);
});

test("a clean merge lists its stats without chunks", () => {
  const stats = [
    { status: "M", path: "a.py", insertions: 2, deletions: 1 },
    { status: "A", path: "b.py", insertions: 5, deletions: 0 },
    { status: "D", path: "c.py", insertions: 0, deletions: 3 },
  ];
  const rows = mergeFileStats(splitPatch(""), stats);
  assert.deepEqual(rows.map((r) => [r.path, r.status, r.chunkIndex, r.insertions, r.deletions]), [
    ["a.py", "M", null, 2, 1], ["b.py", "A", null, 5, 0], ["c.py", "D", null, 0, 3],
  ]);
});

test("without stats the counts come from the hunks", () => {
  const rows = mergeFileStats(splitPatch(MODIFIED + ADDED), null);
  assert.deepEqual(rows.map((r) => [r.path, r.insertions, r.deletions, r.chunkIndex]), [["a.py", 1, 1, 0], ["new.txt", 1, 0, 1]]);
});

test("an added line that looks like a +++ header does not replace the path", () => {
  const [f] = splitPatch("diff --git a/notes.md b/notes.md\nindex 1..2 100644\n--- a/notes.md\n+++ b/notes.md\n@@ -1 +1,2 @@\n x\n+++ b/foo\n");
  assert.equal(f.path, "notes.md");
  const [g] = splitPatch("diff --git a/notes.md b/notes.md\nindex 1..2 100644\n--- a/notes.md\n+++ b/notes.md\n@@ -1 +1,3 @@\n x\n+rename to evil\n+new file mode 100644\n");
  assert.deepEqual([g.path, g.status], ["notes.md", "M"]);
});

test("CRLF line endings", () => {
  const crlf = MODIFIED.replace(/\n/g, "\r\n") + RENAMED.replace(/\n/g, "\r\n");
  const files = splitPatch(crlf);
  assert.deepEqual(files.map((f) => [f.path, f.oldPath, f.status]), [["a.py", null, "M"], ["src/two.py", "src/one.py", "R"]]);
  assert.equal(files.map((f) => f.text).join(""), crlf);
  const rows = mergeFileStats(files, null);
  assert.deepEqual([rows[0].insertions, rows[0].deletions], [1, 1]);
});

test("a patch without a trailing newline", () => {
  const files = splitPatch(ADDED + DELETED.replace(/\n$/, ""));
  assert.deepEqual(files.map((f) => [f.path, f.status]), [["new.txt", "A"], ["old.txt", "D"]]);
  const [r] = splitPatch(RENAMED.replace(/\n$/, ""));
  assert.deepEqual([r.path, r.oldPath], ["src/two.py", "src/one.py"]);
});
