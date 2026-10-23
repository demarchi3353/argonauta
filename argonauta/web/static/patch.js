// Splits one combined `git` patch into per-file chunks. Works as a classic
// browser script (window.ArgonautaPatch) and under Node's require.
(function (root) {
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
    const header = chunkLines[0].replace(/\r?\n$/, "");
    let path = null;
    let oldPath = null;
    let status = "M";
    let binary = false;
    let hasHunk = false;
    let modeChange = false;
    for (const line of chunkLines.slice(1)) {
      if (line.startsWith("@@")) hasHunk = true;
      else if (line.startsWith("+++ b/")) path = line.slice(6).replace(/\r?\n$/, "");
      else if (line.startsWith("new file mode")) status = "A";
      else if (line.startsWith("deleted file mode")) status = "D";
      else if (line.startsWith("rename from ")) {
        status = "R";
        oldPath = line.slice(12).replace(/\r?\n$/, "");
      } else if (line.startsWith("old mode") || line.startsWith("new mode")) modeChange = true;
      else if (line.startsWith("Binary files") || line.startsWith("GIT binary patch")) binary = true;
    }
    if (status === "M" && modeChange && !hasHunk) status = "T";
    if (path === null) {
      const m = /^diff --git a\/.* b\/(.*)$/.exec(header);
      path = m ? m[1] : header.slice("diff --git ".length);
    }
    return { path, oldPath, status, binary, text: chunkLines.join("") };
  }

  const api = { splitPatch };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.ArgonautaPatch = api;
})(typeof window !== "undefined" ? window : globalThis);
