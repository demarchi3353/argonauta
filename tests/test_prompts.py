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
