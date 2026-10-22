import logging

from argonauta.logging_setup import RedactingFilter


def test_redacting_filter_scrubs_key_shapes_and_env_secrets(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "super-secret-value-123")
    f = RedactingFilter()
    record = logging.LogRecord("argonauta", logging.INFO, __file__, 1, "key=sk-ant-abcdefghijkl other=%s", ("super-secret-value-123",), None)
    assert f.filter(record)
    rendered = record.getMessage()
    assert "sk-ant-" not in rendered and "super-secret" not in rendered
    assert rendered.count("[redacted]") == 2


def test_short_secret_values_are_not_redacted(monkeypatch):
    monkeypatch.setenv("ARGONAUTA_LOCAL_API_KEY", "k")
    f = RedactingFilter()
    record = logging.LogRecord("argonauta", logging.INFO, __file__, 1, "key=value kept", None, None)
    f.filter(record)
    assert record.getMessage() == "key=value kept"
