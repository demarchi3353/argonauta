"""Prompt construction and response parsing for the diff-proposing model call."""

from __future__ import annotations

from dataclasses import dataclass

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
