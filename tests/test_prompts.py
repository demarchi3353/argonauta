from argonauta.prompts import ContextFile, ProposalRequest, build_user_message, parse_reply, prompt_bundle
from tests.conftest import SAMPLE_REPLY


def test_parse_reply_separates_explanation_and_diff():
    parsed = parse_reply(SAMPLE_REPLY.read_text())
    assert parsed.explanation.startswith("The `power()` helper")
    assert parsed.diff.startswith("diff --git a/calc/core.py")
    assert "```" not in parsed.diff


def test_parse_reply_without_diff():
    parsed = parse_reply("I cannot safely do this.")
    assert parsed.diff == "" and parsed.explanation == "I cannot safely do this."


def test_user_message_labels_files():
    req = ProposalRequest(task="add a thing", repo_summary="root: x", files=[ContextFile("a.py", "print(1)")])
    msg = build_user_message(req)
    assert '<file path="a.py">' in msg and "<task>" in msg and "print(1)" in msg
    bundle = prompt_bundle(req)
    assert "--from-diff" in bundle and "System prompt" in bundle


from argonauta.prompts import ReviewComment, ReviewRequest, REVIEW_SYSTEM_PROMPT, build_review_message, parse_review_reply, review_bundle


def _review_req(mode="received", **kw):
    base = dict(mode=mode, source_kind="pr", source_label="PR #3 Rebuild the web UI", diff="diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n",
                omitted_files=["big.bin"], comments=[ReviewComment(author="alice", body="early return hides d.files", path="app.js", line=312, hunk="@@ -310,3 +310,3 @@"),
                ReviewComment(author="bob", body="rewrite this", path="big.bin", line=None, file_omitted=True)], draft="", pr_title="Rebuild the web UI", pr_body="Long description", repo_summary="root: demo")
    base.update(kw)
    return ReviewRequest(**base)


def test_review_system_prompt_demands_disagreement_and_json():
    assert "Disagree with the reviewer when the diff gives you grounds to." in REVIEW_SYSTEM_PROMPT
    assert "Never praise for its own sake." in REVIEW_SYSTEM_PROMPT
    assert '"findings"' in REVIEW_SYSTEM_PROMPT and "```json" in REVIEW_SYSTEM_PROMPT


def test_review_message_carries_every_section():
    msg = build_review_message(_review_req())
    assert "<mode>\nreceived\n</mode>" in msg
    assert '<source label="PR #3 Rebuild the web UI">' in msg
    assert "<omitted_files>\nbig.bin\n</omitted_files>" in msg
    assert '<comment author="alice" path="app.js" line="312" file_omitted="false">' in msg
    assert '<comment author="bob" path="big.bin" line="" file_omitted="true">' in msg
    assert "<pull_request>" in msg and "Rebuild the web UI" in msg
    assert "agree/partly/disagree" in msg and "<draft>" not in msg


def test_review_message_mode_mine_and_code():
    mine = build_review_message(_review_req(mode="mine", source_kind="branch", draft="app.js:312 early return is wrong", comments=[]))
    assert "<draft>\napp.js:312 early return is wrong\n</draft>" in mine and "sound/weak/wrong" in mine and "<comments>" not in mine
    code = build_review_message(_review_req(mode="code", source_kind="worktree", comments=[], diff="", omitted_files=["a.py"]))
    assert '<diff omitted="all">' in code and "high/medium/low" in code
    noted = build_review_message(_review_req(notes=["5 later comments were not sent"]))
    assert "<notes>\n5 later comments were not sent\n</notes>" in noted and "<notes>" not in code


def test_parse_review_reply_accepts_fenced_json_with_prose_around_it():
    text = 'Here is my assessment.\n```json\n{"summary": "1 agree, 1 disagree", "findings": [{"kind": "verdict", "verdict": "disagree", "path": "app.js", "line": "312", "author": "alice", "quote": "early return", "title": "Early return is fine", "body": "d.files is consulted later."}, {"kind": "finding", "severity": "HIGH", "title": "Missed null check", "body": "…"}, {"body": "no title, dropped"}]}\n```\nThanks.'
    parsed = parse_review_reply(text)
    assert parsed.ok and parsed.summary == "1 agree, 1 disagree"
    assert [f["title"] for f in parsed.findings] == ["Early return is fine", "Missed null check"]
    assert parsed.findings[0]["line"] == 312 and parsed.findings[0]["verdict"] == "disagree"
    assert parsed.findings[1]["severity"] == "high" and "verdict" not in parsed.findings[1]
    assert parsed.findings[0]["kind"] == "verdict" and parsed.findings[1]["kind"] == "finding"


def test_parse_review_reply_bare_object_and_garbage():
    assert parse_review_reply('{"summary": "clean", "findings": []} trailing').ok
    bad = parse_review_reply("I could not produce JSON, sorry.")
    assert bad.ok is False and bad.findings == [] and bad.summary == ""
    weird = parse_review_reply('```json\n{"summary": 3, "findings": [{"title": "t", "body": "b", "verdict": "maybe", "line": "x"}]}\n```')
    assert weird.ok and weird.summary == "" and "verdict" not in weird.findings[0] and weird.findings[0]["line"] is None


def test_review_bundle_has_both_prompts():
    bundle = review_bundle(_review_req())
    assert REVIEW_SYSTEM_PROMPT in bundle and "<mode>" in bundle and "argonauta" in bundle.lower()


def test_review_system_prompt_flags_tone_in_draft_reviews():
    assert "tone" in REVIEW_SYSTEM_PROMPT
    flat = " ".join(REVIEW_SYSTEM_PROMPT.split())
    assert "phrased in a way the pull request's author would reasonably resent" in flat


def test_parse_review_reply_ignores_objects_inside_failed_fences():
    text = '```json\n{"broken": {"title": "x"}, oops}\n```\nThen: {"summary": "s", "findings": []}'
    parsed = parse_review_reply(text)
    assert parsed.ok and parsed.summary == "s" and parsed.findings == []


def test_parse_review_reply_accepts_integral_float_line():
    parsed = parse_review_reply('{"summary": "s", "findings": [{"title": "t", "line": 312.0}, {"title": "u", "line": 3.5}]}')
    assert [f["line"] for f in parsed.findings] == [312, None]
