import sys
import types

import pytest

from argonauta.web.terminal import TerminalError, TerminalManager


class FakePTY:
    instances = []

    def __init__(self, cols, rows):
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
