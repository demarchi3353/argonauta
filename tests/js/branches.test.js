const test = require("node:test");
const assert = require("node:assert/strict");
const { groupBranches } = require("../../argonauta/web/static/branches.js");

const b = (name, remote = null, extra = {}) => ({ name, remote, is_current: false, upstream: null, sha: "abc1234", subject: "", ahead: 0, behind: 0, gone: false, ...extra });
const BRANCHES = [
  b("main", null, { is_current: true }), b("feature/Login"), b("origin/main", "origin"), b("origin/feature/login", "origin"),
  b("upstream/main", "upstream"), b("old/x", "old"), b("origin/origin/nested", "origin"),
];
const REMOTES = [{ name: "upstream", url: "u" }, { name: "origin", url: "o" }];

test("empty filter keeps everything, remotes in st.remotes order, unknown remotes last", () => {
  const r = groupBranches(BRANCHES, REMOTES, "");
  assert.deepEqual(r.locals.map((x) => x.name), ["main", "feature/Login"]);
  assert.deepEqual(r.groups.map((g) => g.remote), ["upstream", "origin", "old"]);
  assert.deepEqual(r.groups[1].branches.map((x) => x.short), ["main", "feature/login", "origin/nested"]);
  assert.equal(r.localShown, 2); assert.equal(r.localTotal, 2); assert.equal(r.remoteShown, 5); assert.equal(r.remoteTotal, 5);
});

test("filter is case-insensitive, trimmed, and matches the full remote name", () => {
  const r = groupBranches(BRANCHES, REMOTES, "  LOGIN ");
  assert.deepEqual(r.locals.map((x) => x.name), ["feature/Login"]);
  assert.deepEqual(r.groups.map((g) => g.remote), ["origin"]);
  assert.deepEqual(r.groups[0].branches.map((x) => x.name), ["origin/feature/login"]);
  assert.equal(r.remoteShown, 1); assert.equal(r.remoteTotal, 5);
  assert.deepEqual(groupBranches(BRANCHES, REMOTES, "upstream").groups.map((g) => g.remote), ["upstream"]);
});

test("groups with no match are dropped and an undefined filter means no filter", () => {
  assert.deepEqual(groupBranches(BRANCHES, REMOTES, "zzz").groups, []);
  assert.equal(groupBranches(BRANCHES, REMOTES, undefined).remoteShown, 5);
  assert.deepEqual(groupBranches([], [{ name: "origin", url: "o" }], "").groups, []);
});
