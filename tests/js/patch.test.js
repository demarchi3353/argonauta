const test = require("node:test");
const assert = require("node:assert/strict");
const { splitPatch } = require("../../argonauta/web/static/patch.js");

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
