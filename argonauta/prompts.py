"""Prompt construction and response parsing for the diff-proposing model call."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from argonauta.diffs import extract_diff

SYSTEM_PROMPT = """You are a careful software engineer working inside a single git repository.
You will receive a task and the contents of a few relevant files, each labelled with
its path relative to the repository root.

Respond with:
1. One short paragraph explaining the change you propose.
2. Exactly one unified diff inside a ```diff fenced block.

Diff rules:
- Use `diff --git a/<path> b/<path>` headers with `---`/`+++` lines and @@ hunks.
- Paths are relative to the repository root. Never touch .git, .env or .argonauta.
- Only change files you were shown, or create new files next to them.
- Keep the change minimal and complete; include tests when the task asks for them.
- If the task cannot be done safely from the files shown, say so instead of guessing,
  and return no diff.
"""


@dataclass(frozen=True)
class ContextFile:
    path: str
    content: str

    @property
    def size(self) -> int:
        return len(self.content.encode("utf-8", "replace"))


@dataclass(frozen=True)
class ProposalRequest:
    task: str
    repo_summary: str
    files: list[ContextFile]


@dataclass(frozen=True)
class ParsedReply:
    explanation: str
    diff: str


def build_user_message(req: ProposalRequest) -> str:
    parts = [f"<task>\n{req.task.strip()}\n</task>", f"<repository>\n{req.repo_summary.strip()}\n</repository>"]
    if req.files:
        parts.append("<files>")
        for f in req.files:
            body = f.content if f.content.endswith("\n") else f.content + "\n"
            parts.append(f'<file path="{f.path}">\n{body}</file>')
        parts.append("</files>")
    else:
        parts.append("<files>\n(no indexed files matched the task; the repository may be empty)\n</files>")
    parts.append("Reply with the explanation and the ```diff block now.")
    return "\n\n".join(parts)


def parse_reply(text: str) -> ParsedReply:
    diff = extract_diff(text)
    explanation = text
    if diff:
        fence = text.find("```")
        if fence >= 0:
            explanation = text[:fence]
        else:
            first = text.find(diff.splitlines()[0])
            if first > 0:
                explanation = text[:first]
    explanation = explanation.strip()
    if len(explanation) > 4000:
        explanation = explanation[:4000] + "…"
    return ParsedReply(explanation=explanation, diff=diff)


def prompt_bundle(req: ProposalRequest) -> str:
    """Markdown bundle for the manual (degraded) provider."""
    return (
        "# Argonauta prompt bundle\n\n"
        "Paste everything below into the model of your choice. Save its reply (or just the\n"
        "diff) to a file and run:\n\n"
        "    argonauta propose \"<same task>\" --from-diff <file>\n\n"
        "## System prompt\n\n" + SYSTEM_PROMPT + "\n\n## User message\n\n" + build_user_message(req) + "\n"
    )


# --------------------------------------------------------------------------
# Review: prompt, request, message builder and reply parser
# --------------------------------------------------------------------------

REVIEW_SYSTEM_PROMPT = """You are a rigorous senior code reviewer working inside a single git repository.
Your job is to find what is actually wrong or risky, and to say so plainly.
Disagree with the reviewer when the diff gives you grounds to.
Never praise for its own sake.

Each request starts with a <mode> section and works in one of three modes:

- code: review a change. You receive the diff under <diff>. Report real problems: bugs,
  regressions, missed edge cases, security or data-loss risks, broken tests. Skip style nitpicks
  unless they hide a defect. Every finding gets a severity: high, medium or low.
- received: judge the comments a reviewer left on a pull request. You receive the diff and a
  <comments> section with one <comment> per remark (author, path, line, and the hunk it points at).
  For each comment, check it against the diff and give a verdict: agree (the comment is right),
  partly (right in part, or right but overstated) or disagree (the diff shows it is wrong,
  already handled, or beside the point). Explain the verdict with evidence from the diff. You may
  add severity-tagged findings of your own for problems the reviewer missed.
- mine: critique the owner's own draft review, given under <draft>, against the diff. Judge each
  point of the draft with a verdict: sound (correct and worth raising), weak (vague, minor or
  not well supported) or wrong (contradicted by the diff). Also report significant problems the
  draft overlooks, as findings with a severity. Address the critique plainly to the owner: be
  direct about what is wrong or weak and give the reason, never vague and never padded. Also
  watch the tone of each point. When a point is phrased in a way the pull request's author would
  reasonably resent (sarcasm, blame, absolutes, commands without reasons), add a short "tone" note
  to that finding's body saying what to soften and why, without changing the technical verdict.

Other sections you may see: <source> (what is being reviewed), <omitted_files> and
<diff omitted="all"> (content that was left out; do not guess about it, and say when a verdict
cannot be reached for that reason), <pull_request> (title and description), <notes> (caveats about
what was or was not sent), and <repository> (a short summary of the repository).

Rules:
- Ground every claim in the diff or the comment text. Quote the relevant line when it helps.
- If the evidence is insufficient, say so rather than guessing.
- Do not invent files, lines or behaviour that you were not shown.
- Be concise: a title that states the point, a body that gives the reasoning.

Reply with a short prose remark if you wish, then exactly one JSON object inside a ```json
fenced block, shaped like this:

```json
{
  "summary": "one or two sentences with the overall assessment",
  "findings": [
    {
      "kind": "verdict | finding",
      "verdict": "agree | partly | disagree (mode received) or sound | weak | wrong (mode mine); omit otherwise",
      "severity": "high | medium | low (kind finding, and mode code); omit otherwise",
      "path": "file path, or null",
      "line": 123,
      "author": "comment author for received mode, or null",
      "quote": "the comment or draft point being judged, shortened, or null",
      "title": "short statement of the point (required)",
      "body": "the reasoning and evidence"
    }
  ]
}
```

Use null for path and line when they do not apply. Use an empty findings list when there is
nothing to report.
"""


@dataclass(frozen=True)
class ReviewComment:
    author: str
    body: str
    path: str | None = None
    line: int | None = None
    hunk: str = ""
    file_omitted: bool = False


@dataclass(frozen=True)
class ReviewRequest:
    mode: str
    source_kind: str
    source_label: str
    diff: str
    omitted_files: list[str]
    comments: list[ReviewComment]
    draft: str
    pr_title: str
    pr_body: str
    repo_summary: str
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ParsedReview:
    ok: bool
    summary: str
    findings: list[dict]


_REVIEW_CLOSING = {
    "received": "agree/partly/disagree",
    "mine": "sound/weak/wrong",
}


def _attr(value: object) -> str:
    return str(value).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def build_review_message(req: ReviewRequest) -> str:
    parts = [f"<mode>\n{req.mode}\n</mode>", f'<source label="{_attr(req.source_label)}">\n{req.source_kind}\n</source>']
    if req.notes:
        parts.append("<notes>\n" + "\n".join(req.notes) + "\n</notes>")
    if req.source_kind == "pr":
        parts.append(f"<pull_request>\n<title>{req.pr_title.strip()}</title>\n<body>\n{req.pr_body.strip()}\n</body>\n</pull_request>")
    if req.diff.strip():
        diff = req.diff if req.diff.endswith("\n") else req.diff + "\n"
        parts.append(f"<diff>\n{diff}</diff>")
    else:
        parts.append('<diff omitted="all">\n(no diff content is available)\n</diff>')
    if req.omitted_files:
        parts.append("<omitted_files>\n" + "\n".join(req.omitted_files) + "\n</omitted_files>")
    if req.mode == "received":
        parts.append("<comments>")
        for c in req.comments:
            line = "" if c.line is None else str(c.line)
            head = (f'<comment author="{_attr(c.author)}" path="{_attr(c.path or "")}" '
                    f'line="{line}" file_omitted="{"true" if c.file_omitted else "false"}">')
            hunk = f"<hunk>\n{c.hunk}\n</hunk>\n" if c.hunk else ""
            parts.append(f"{head}\n{hunk}{c.body.strip()}\n</comment>")
        parts.append("</comments>")
    elif req.mode == "mine":
        parts.append(f"<draft>\n{req.draft.strip()}\n</draft>")
    parts.append(f"<repository>\n{req.repo_summary.strip()}\n</repository>")
    verdicts = _REVIEW_CLOSING.get(req.mode)
    if verdicts:
        parts.append(f"Reply with the summary and the ```json block now. Every verdict must be one of {verdicts}.")
    else:
        parts.append("Reply with the summary and the ```json block now. Every finding needs a severity of high/medium/low.")
    return "\n\n".join(parts)


_FENCE_RE = re.compile(r"```(?:json)?[ \t]*\r?\n(.*?)```", re.S | re.I)
_VERDICTS = {"agree", "partly", "disagree", "sound", "weak", "wrong"}
_SEVERITIES = {"high", "medium", "low"}


def _first_json_object(text: str) -> dict | None:
    candidates: list[dict] = []
    masked = text
    for m in _FENCE_RE.finditer(text):
        masked = masked[: m.start()] + " " * (m.end() - m.start()) + masked[m.end():]
        try:
            obj = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(obj, dict):
            candidates.append(obj)
    decoder = json.JSONDecoder()
    pos = masked.find("{")
    while pos >= 0:
        try:
            obj, end = decoder.raw_decode(masked, pos)
        except ValueError:
            obj, end = None, pos + 1
        if isinstance(obj, dict):
            candidates.append(obj)
        pos = masked.find("{", end)
    for obj in candidates:
        if "findings" in obj or "summary" in obj:
            return obj
    return candidates[0] if candidates else None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"\s*\d+\s*", value):
        return int(value)
    return None


def _clean_finding(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    body = raw.get("body")
    out: dict = {"title": title.strip(), "body": body if isinstance(body, str) else ""}
    for key in ("path", "author", "quote"):
        if isinstance(raw.get(key), str):
            out[key] = raw[key]
    for key, allowed in (("verdict", _VERDICTS), ("severity", _SEVERITIES)):
        value = raw.get(key)
        if isinstance(value, str) and value.strip().lower() in allowed:
            out[key] = value.strip().lower()
    out["kind"] = "verdict" if "verdict" in out else "finding"
    out["line"] = _as_int(raw.get("line"))
    return out


def parse_review_reply(text: str) -> ParsedReview:
    obj = _first_json_object(text)
    if obj is None:
        return ParsedReview(ok=False, summary="", findings=[])
    summary = obj.get("summary")
    raw_findings = obj.get("findings")
    findings = [f for f in map(_clean_finding, raw_findings if isinstance(raw_findings, list) else []) if f]
    return ParsedReview(ok=True, summary=summary.strip() if isinstance(summary, str) else "", findings=findings)


def review_bundle(req: ReviewRequest) -> str:
    """Markdown bundle for the manual (degraded) provider."""
    return (
        "# Argonauta review prompt bundle\n\n"
        "Paste everything below into the model of your choice and read its reply\n"
        "(a JSON object in a ```json block) yourself.\n\n"
        "## System prompt\n\n" + REVIEW_SYSTEM_PROMPT + "\n\n## User message\n\n" + build_review_message(req) + "\n"
    )
