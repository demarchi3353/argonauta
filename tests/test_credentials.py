from pathlib import Path

from argonauta.web import credentials as c

SSH_G = """user git
hostname github.com
port 22
identityfile ~/.ssh/personal
identityfile ~/.ssh/id_ed25519
identitiesonly yes
"""
SSH_ADD = "256 SHA256:s18ji8YyW/PaVYViBocxueZx1u0VVODqjlyHcwUc+nE demarchi@example (ED25519)\n"
KEYGEN = "256 SHA256:s18ji8YyW/PaVYViBocxueZx1u0VVODqjlyHcwUc+nE demarchi@example (ED25519)\n"
GPG = """tru::1:1718000000:0:3:1:5
pub:u:4096:1:D519886F454D7B57:1718000000:1800000000::u:::scESC::::::23::0:
fpr:::::::::DACD5A577C1C74DEFB025B7FD519886F454D7B57:
uid:u::::1718000000::ABCDEF::gpg-personal <me@example.invalid>::::::::::0:
sub:u:4096:1:AAAAAAAAAAAAAAAA:1718000000::::::e::::::23:
"""


def test_remote_kind():
    assert c.remote_kind("git@github-personal:me/repo.git") == ("ssh", "github-personal")
    assert c.remote_kind("ssh://git@example.com:2222/x.git") == ("ssh", "example.com")
    assert c.remote_kind("https://github.com/me/repo.git") == ("https", "github.com")
    assert c.remote_kind("/tmp/remote.git") == ("local", None)
    assert c.remote_kind("") == ("none", None)


def test_parsers():
    cfg = c.parse_ssh_config(SSH_G)
    assert cfg["hostname"] == "github.com" and cfg["user"] == "git"
    assert cfg["identity_files"] == ["~/.ssh/personal", "~/.ssh/id_ed25519"]
    assert c.parse_ssh_add(SSH_ADD) == ["SHA256:s18ji8YyW/PaVYViBocxueZx1u0VVODqjlyHcwUc+nE"]
    assert c.parse_ssh_add("The agent has no identities.\n") == []
    key = c.parse_keygen(KEYGEN)
    assert key == {"bits": 256, "fingerprint": "SHA256:s18ji8YyW/PaVYViBocxueZx1u0VVODqjlyHcwUc+nE", "comment": "demarchi@example", "type": "ED25519"}
    gpg = c.parse_gpg_colons(GPG)
    assert gpg["key_id"] == "D519886F454D7B57" and gpg["fingerprint"].endswith("454D7B57")
    assert gpg["uid"] == "gpg-personal <me@example.invalid>" and gpg["status"] == "ok"
    assert gpg["created"].startswith("2024") and gpg["expires"].startswith("2027")
    assert c.parse_gpg_colons("") is None
    home = str(Path.home())
    assert c.parse_config_origin(f"file:{home}/.gitconfig\tJane\n") == ("Jane", "~/.gitconfig")
    assert c.parse_config_origin("file:.git/config\tJane\n") == ("Jane", "repo .git/config")
    assert c.parse_config_origin("") == (None, None)


def test_inspect_composes_with_fake_runner(sample_repo, tmp_path, monkeypatch):
    from tests.conftest import git
    git(sample_repo, "remote", "add", "origin", "git@github-personal:me/repo.git")
    git(sample_repo, "config", "user.name", "test")  # the fixture commits via env vars, not config
    git(sample_repo, "config", "user.signingkey", "D519886F454D7B57")
    git(sample_repo, "config", "commit.gpgsign", "true")
    pub = tmp_path / "personal.pub"
    pub.write_text("ssh-ed25519 AAAA demarchi@example\n")
    monkeypatch.setattr(c, "_expand", lambda p: str(pub)[:-4] if p.endswith("personal") else p)

    def fake_run(argv, cwd=None):
        if argv[:2] == ["ssh", "-G"]:
            return 0, SSH_G
        if argv == ["ssh-add", "-l"]:
            return 0, SSH_ADD
        if argv[:2] == ["ssh-keygen", "-l"]:
            return 0, KEYGEN
        if argv[0] == "gpg":
            return 0, GPG
        return c.run_cmd(argv, cwd)

    info = c.inspect(sample_repo, run=fake_run)
    assert info["identity"]["name"] == "test" and info["identity"]["source"]
    assert info["ssh"]["kind"] == "ssh" and info["ssh"]["host_alias"] == "github-personal"
    assert info["ssh"]["resolved_hostname"] == "github.com"
    loaded = [f for f in info["ssh"]["identity_files"] if f["exists"]]
    assert loaded and loaded[0]["fingerprint"].startswith("SHA256:") and info["ssh"]["agent"]["loaded"] is True
    assert info["signing"]["enabled"] is True and info["signing"]["key_id"] == "D519886F454D7B57" and info["signing"]["status"] == "ok"
    assert info["head"]["signature"] in c.SIGNATURE_WORDS.values()


def test_inspect_without_tools_degrades(sample_repo):
    def nothing(argv, cwd=None):
        if argv[0] == "git":
            return c.run_cmd(argv, cwd)
        return 127, ""
    info = c.inspect(sample_repo, run=nothing)
    assert info["ssh"]["kind"] == "none" and info["ssh"]["agent"]["running"] is False
    assert info["signing"]["status"] == "disabled"
