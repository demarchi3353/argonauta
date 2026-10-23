// Splits one combined `git` patch into per-file chunks and matches them with
// `git show --numstat` stats. Works as a classic browser script
// (window.ArgonautaPatch) and under Node's require.
(function (root) {
  const C_ESCAPES = { a: 7, b: 8, t: 9, n: 10, v: 11, f: 12, r: 13, '"': 34, "\\": 92 };

  // git C-quotes a path with special or non-ASCII bytes: "configura\303\247\303\243o.md".
  // Returns the decoded path, or the input unchanged when it is not exactly one quoted token.
  function unquote(s) {
    s = String(s);
    if (s.length < 2 || s[0] !== '"' || s[s.length - 1] !== '"') return s;
    const bytes = [];
    const enc = new TextEncoder();
    for (let i = 1; i < s.length - 1; i++) {
      const c = s[i];
      if (c === '"') return s; // an unescaped quote inside: not a single token
      if (c !== "\\") { for (const b of enc.encode(c)) bytes.push(b); continue; }
      const n = s[++i];
      if (n >= "0" && n <= "7" && /^[0-7]{3}$/.test(s.substr(i, 3))) { bytes.push(parseInt(s.substr(i, 3), 8)); i += 2; }
      else if (n in C_ESCAPES) bytes.push(C_ESCAPES[n]);
      else return s;
    }
    return new TextDecoder("utf-8").decode(new Uint8Array(bytes));
  }

  const stripEol = (line) => line.replace(/\r?\n$/, "").replace(/\r$/, "");
  const dropPrefix = (p, prefix) => (p.startsWith(prefix) ? p.slice(prefix.length) : p);
  // a path value on a header line: quoted or plain, without the a/ or b/ prefix
  function headerPath(rest, prefix) {
    const p = unquote(rest);
    return p === "/dev/null" ? null : dropPrefix(p, prefix);
  }
  // `diff --git a/x b/y`: quoted tokens when either side needs it, otherwise split at the last " b/"
  function gitHeaderPaths(header) {
    const rest = header.slice("diff --git ".length);
    const tokens = rest.match(/^("(?:[^"\\]|\\.)*"|\S+) ("(?:[^"\\]|\\.)*"|.+)$/);
    if (tokens && (tokens[1][0] === '"' || tokens[2][0] === '"')) return [dropPrefix(unquote(tokens[1]), "a/"), dropPrefix(unquote(tokens[2]), "b/")];
    const m = /^a\/(.*) b\/(.*)$/.exec(rest);
    return m ? [m[1], m[2]] : [rest, rest];
  }

  function splitPatch(text) {
    const lines = String(text || "").split(/(?<=\n)/);
    const chunks = [];
    for (const line of lines) {
      // Inside a hunk every line starts with "+", "-", " " or "\", so a
      // "diff --git " at column 0 is always a file boundary.
      if (line.startsWith("diff --git ")) chunks.push([line]);
      else if (chunks.length) chunks[chunks.length - 1].push(line);
    }
    return chunks.map(parseChunk);
  }

  function parseChunk(chunkLines) {
    const [headerOld, headerNew] = gitHeaderPaths(stripEol(chunkLines[0]));
    let plusPath = null, minusPath = null, renameTo = null, renameFrom = null;
    let status = "M", binary = false, hasHunk = false, modeChange = false;
    for (const raw of chunkLines.slice(1)) {
      const line = stripEol(raw);
      // the extended header ends at the first hunk: below it "++ b/foo" is an added line
      if (line.startsWith("@@")) { hasHunk = true; break; }
      if (line.startsWith("+++ ")) plusPath = headerPath(line.slice(4), "b/");
      else if (line.startsWith("--- ")) minusPath = headerPath(line.slice(4), "a/");
      else if (line.startsWith("new file mode")) status = "A";
      else if (line.startsWith("deleted file mode")) status = "D";
      else if (line.startsWith("rename from ")) renameFrom = unquote(line.slice(12));
      else if (line.startsWith("rename to ")) renameTo = unquote(line.slice(10));
      else if (line.startsWith("old mode") || line.startsWith("new mode")) modeChange = true;
      else if (line.startsWith("Binary files") || line.startsWith("GIT binary patch")) binary = true;
    }
    if (renameFrom !== null || renameTo !== null) status = "R";
    if (status === "M" && modeChange && !hasHunk) status = "T";
    const path = renameTo || plusPath || (status === "D" ? minusPath : null) || headerNew || headerOld;
    const oldPath = status === "R" ? (renameFrom || minusPath || headerOld) : null;
    return { path, oldPath, status, binary, text: chunkLines.join("") };
  }

  // numstat names a rename "src/{old.py => new.py}" or "old.py => new.py"; quoted names are C-quoted
  function statPath(raw) {
    const s = unquote(String(raw || ""));
    const brace = /^(.*)\{(.*) => (.*)\}(.*)$/.exec(s);
    if (brace) return (brace[1] + unquote(brace[3]) + brace[4]).replace(/\/\/+/g, "/");
    const arrow = s.indexOf(" => ");
    if (arrow >= 0) return unquote(s.slice(arrow + 4));
    return s;
  }

  function countLines(text, sign) {
    let n = 0, inHunk = false;
    for (const l of text.split("\n")) { if (l.startsWith("@@")) inHunk = true; else if (inHunk && l.startsWith(sign)) n++; }
    return n;
  }

  // One row per file: every chunk, with counts from its stat when one matches, then the
  // stats no chunk matched (a clean merge lists files but has no patch text).
  function mergeFileStats(chunks, stats) {
    const pending = new Map();
    for (const f of stats || []) { const p = statPath(f.path); if (!pending.has(p)) pending.set(p, f); }
    const rows = (chunks || []).map((ch, i) => {
      const st = pending.get(ch.path);
      if (st) pending.delete(ch.path);
      return { path: ch.path, oldPath: ch.oldPath, status: ch.status, binary: ch.binary, insertions: st ? st.insertions : countLines(ch.text, "+"), deletions: st ? st.deletions : countLines(ch.text, "-"), chunkIndex: i };
    });
    pending.forEach((f, p) => rows.push({ path: p, oldPath: null, status: f.status, binary: false, insertions: f.insertions, deletions: f.deletions, chunkIndex: null }));
    return rows;
  }

  const api = { splitPatch, mergeFileStats };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.ArgonautaPatch = api;
})(typeof window !== "undefined" ? window : globalThis);
