import sys
import types

import pytest

from argonauta.web.terminal import TerminalError, TerminalManager


class FakePTY:
    instances = []

    def __init__(self, cols, rows, backend=None):
        self.backend = backend
        self.cols, self.rows, self.out, self.spawned, self.alive_flag, self.pid = cols, rows, "", None, True, 4242
        FakePTY.instances.append(self)

    def spawn(self, appname, cmdline=None, cwd=None, env=None):
        self.spawned = (appname, cmdline, cwd, env)
        self.out += "PS> "
        return not appname.endswith("fail.exe")

    def read(self, blocking=False):
        data, self.out = self.out, ""
        return data

    def write(self, text):
        self.out += text.upper()  # the fake shell echoes upper-case
        return len(text)

    def set_size(self, cols, rows):
        self.cols, self.rows = cols, rows

    def isalive(self):
        return self.alive_flag

    def iseof(self):
        return not self.alive_flag


class WinptyError(Exception):
    pass


@pytest.fixture
def fake_winpty(monkeypatch, tmp_path):
    mod = types.ModuleType("winpty"); mod.PTY = FakePTY; mod.WinptyError = WinptyError
    mod.Backend = types.SimpleNamespace(ConPTY=0)
    monkeypatch.setitem(sys.modules, "winpty", mod)
    FakePTY.instances.clear()
    shell = tmp_path / "pwsh.exe"; shell.write_text(""); shell.chmod(0o755)
    monkeypatch.setattr("os.kill", lambda pid, sig: setattr(FakePTY.instances[-1], "alive_flag", False))
    from argonauta.web import terminal
    terminal.reset_support_cache()
    yield shell
    terminal.reset_support_cache()


def test_spawn_passes_cwd_env_and_cmdline(fake_winpty, tmp_path):
    from argonauta.web.terminal_windows import WindowsTerminalSession
    s = WindowsTerminalSession.spawn(tmp_path, cols=100, rows=30, shell=str(fake_winpty))
    appname, cmdline, cwd, env = FakePTY.instances[-1].spawned
    assert appname == str(fake_winpty) and cmdline == "-NoLogo" and cwd == str(tmp_path)
    assert "TERM=xterm-256color\0" in env and "COLORTERM=truecolor\0" in env and env.endswith("\0")
    assert s.cols == 100 and s.pid == 4242 and s.alive() and s.id.startswith("t_")
    s.kill()


def test_spawn_quotes_a_shell_path_with_spaces_and_pins_conpty(fake_winpty, tmp_path):
    from argonauta.web.terminal_windows import WindowsTerminalSession
    spaced = tmp_path / "Program Files" / "pwsh.exe"
    spaced.parent.mkdir(); spaced.write_text(""); spaced.chmod(0o755)
    s = WindowsTerminalSession.spawn(tmp_path, shell=str(spaced))
    assert FakePTY.instances[-1].spawned[0] == '"' + str(spaced) + '"' and FakePTY.instances[-1].backend == 0
    s.kill()
    s = WindowsTerminalSession.spawn(tmp_path, shell=str(fake_winpty))
    assert FakePTY.instances[-1].spawned[0] == str(fake_winpty)
    s.kill()


def test_kill_with_no_pid_and_shutdown_survives_a_failing_session(fake_winpty, tmp_path):
    from argonauta.web.terminal_windows import WindowsTerminalSession
    s = WindowsTerminalSession.spawn(tmp_path, shell=str(fake_winpty))
    s.pid = None
    s.kill()
    m = TerminalManager(backend="conpty")
    killed = []

    class Bad:
        id = "bad"
        def kill(self): raise RuntimeError("boom")

    class Good:
        id = "good"
        def kill(self): killed.append(1)

    m._sessions = {"a": Bad(), "b": Good()}
    m.shutdown()
    assert killed == [1]


def test_read_write_resize_and_exit(fake_winpty, tmp_path):
    from argonauta.web.terminal_windows import WindowsTerminalSession
    s = WindowsTerminalSession.spawn(tmp_path, shell=str(fake_winpty))
    assert s.read(0.01) == b"PS> "
    assert s.read(0.01) is None
    s.write(b"dir\r\n")
    assert s.read(0.01) == b"DIR\r\n"
    s.resize(9999, 1)
    assert (s.cols, s.rows) == (500, 3) and (FakePTY.instances[-1].cols, FakePTY.instances[-1].rows) == (500, 3)
    FakePTY.instances[-1].alive_flag = False
    assert s.read(0.01) == b"" and not s.alive() and s.read(0.01) == b""
    s.kill(); s.kill()  # idempotent


def test_spawn_failure_and_missing_shell(fake_winpty, tmp_path):
    from argonauta.web.terminal_windows import WindowsTerminalSession
    bad = tmp_path / "fail.exe"; bad.write_text(""); bad.chmod(0o755)
    with pytest.raises(TerminalError, match="could not start"):
        WindowsTerminalSession.spawn(tmp_path, shell=str(bad))
    with pytest.raises(TerminalError, match="shell not found"):
        WindowsTerminalSession.spawn(tmp_path, shell=str(tmp_path / "nope.exe"))


def test_manager_uses_the_conpty_backend(fake_winpty, tmp_path, monkeypatch):
    monkeypatch.setenv("ARGONAUTA_SHELL", str(fake_winpty))
    m = TerminalManager(backend="conpty")
    assert m.support.available and m.support.backend == "conpty"
    s = m.create(tmp_path, 80, 24)
    assert s.to_dict()["alive"] is True and m.list_for(tmp_path) == [s]
    m.shutdown()
    assert not s.alive()


def test_winpty_errors_end_the_session_but_kill_still_signals(fake_winpty, tmp_path, monkeypatch, caplog):
    from argonauta.web.terminal_windows import WindowsTerminalSession
    kills = []
    monkeypatch.setattr("os.kill", lambda pid, sig: (kills.append(pid), setattr(FakePTY.instances[-1], "alive_flag", False)))

    def boom(*a, **k):
        raise WinptyError("pipe broken")

    s = WindowsTerminalSession.spawn(tmp_path, shell=str(fake_winpty))
    monkeypatch.setattr(FakePTY.instances[-1], "read", boom)
    assert s.read(0.01) == b"" and not s.alive() and s.read(0.01) == b""
    assert "read failed" in caplog.text  # shell was alive: logged, not swallowed
    s.kill()
    assert kills == [4242]  # a live shell is signalled even after the session was marked exited

    s2 = WindowsTerminalSession.spawn(tmp_path, shell=str(fake_winpty))
    monkeypatch.setattr(FakePTY.instances[-1], "write", boom)
    s2.write(b"x")
    assert s2.read(0.01) == b"" and "write failed" in caplog.text
    s2.kill()
    assert kills == [4242, 4242]


def test_lone_surrogate_does_not_end_the_session(fake_winpty, tmp_path):
    from argonauta.web.terminal_windows import WindowsTerminalSession
    s = WindowsTerminalSession.spawn(tmp_path, shell=str(fake_winpty))
    FakePTY.instances[-1].out = "a\ud83d"
    data = s.read(0.01)
    assert data.startswith(b"a") and s.alive()
    s.kill()
