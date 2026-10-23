// Rendering helpers for a stored review report: finding cards (all model text is escaped),
// the per-verdict / per-severity counts, and locating a new-side line in a file diff.
// Works as a classic browser script (window.ArgonautaReport) and under Node's require.
(function (root) {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const VERDICTS = ["agree", "partly", "disagree", "sound", "weak", "wrong"], SEVERITIES = ["high", "medium", "low"];
  const MODE_VERDICTS = { received: ["agree", "partly", "disagree"], mine: ["sound", "weak", "wrong"] };
  const NOT_SENT_TITLE = "Not everything was sent"; // the note workflow.review appends; not a model finding

  function fileOmitted(review, path) {
    return (review.omitted_files || []).includes(path) || (review.comments || []).some((c) => c && c.file_omitted && c.path === path);
  }

  function findingHtml(review, f) {
    const verdict = f.kind === "verdict" && VERDICTS.includes(f.verdict) ? f.verdict : null, sev = !verdict && SEVERITIES.includes(f.severity) ? f.severity : null;
    const badge = verdict ? '<span class="badge verdict ' + verdict + '">' + verdict.toUpperCase() + "</span>" : sev ? '<span class="badge sev ' + sev + '">' + sev.toUpperCase() + "</span>" : "";
    const line = Number.isInteger(f.line) && f.line > 0 ? f.line : null;
    const loc = f.path ? f.path + (line ? ":" + line : "") : "";
    const where = f.path ? '<button type="button" class="finding-path" data-rpath="' + esc(f.path) + '" title="Open the diff of ' + esc(f.path) + '">' + esc(loc) + "</button>" : "";
    const omitted = f.path && fileOmitted(review, f.path) ? '<span class="pill warn" title="This file was not in the diff sent to the model">File not sent</span>' : "";
    const quote = f.quote ? '<blockquote class="finding-quote">' + (f.author ? "<cite>" + esc(f.author) + "</cite>" : "") + esc(f.quote) + "</blockquote>" : "";
    return '<article class="finding' + (f.path ? " has-path" : "") + '"' + (f.path ? ' data-rpath="' + esc(f.path) + '"' : "") + (f.path && line ? ' data-rline="' + line + '"' : "") +
      '><div class="finding-head">' + badge + where + omitted + "</div>" + quote +
      '<div class="title">' + esc(f.title) + "</div>" + (f.body ? '<p class="finding-body">' + esc(f.body) + "</p>" : "") + "</article>";
  }

  // "1 agree · 1 partly · 2 disagree" (received/mine) or "1 high · 2 low" (code); "" when nothing to count
  function countsLine(review) {
    const keys = MODE_VERDICTS[review.mode] || SEVERITIES, field = MODE_VERDICTS[review.mode] ? "verdict" : "severity";
    const n = {};
    (review.findings || []).forEach((f) => { if (f && f.title !== NOT_SENT_TITLE && keys.includes(f[field])) n[f[field]] = (n[f[field]] || 0) + 1; });
    return keys.filter((k) => n[k]).map((k) => n[k] + " " + k).join(" · ");
  }

  function summaryLine(review) {
    const counts = countsLine(review), text = review.summary || "";
    return counts && text ? counts + " — " + text : counts || text;
  }

  // index (in diff.split("\n")) of the row showing new-side line `line`, or -1
  function newLineIndex(diff, line) {
    if (!diff || !Number.isInteger(line) || line < 1) return -1;
    const rows = diff.split("\n");
    let n = null;
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i];
      const h = /^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(r);
      if (h) { n = Number(h[1]); continue; }
      if (r.startsWith("diff --git ")) { n = null; continue; }
      if (n === null || r.startsWith("-") || r.startsWith("\\") || (r === "" && i === rows.length - 1)) continue;
      if (n === line) return i;
      n++;
    }
    return -1;
  }

  const api = { esc, findingHtml, fileOmitted, countsLine, summaryLine, newLineIndex, VERDICTS, SEVERITIES, NOT_SENT_TITLE };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.ArgonautaReport = api;
})(typeof window !== "undefined" ? window : globalThis);
