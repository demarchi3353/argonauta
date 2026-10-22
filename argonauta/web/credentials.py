"""Read-only view of the credentials git will use for this repository.

Reads: git config (with origins), `ssh -G` for the remote host, `.pub` files,
`ssh-add -l`, `gpg --list-keys --with-colons`, and git's own signature check.
Never opens a private key file or prints secret material.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from argonauta import gitops

Runner = Callable[[list[str], Path | None], tuple[int, str]]

SIGNATURE_WORDS = {
    "G": "good", "B": "bad", "U": "good_untrusted", "X": "expired_signature",
    "Y": "expired_key", "R": "revoked", "E": "unknown_key", "N": "unsigned",
}


def run_cmd(argv: list[str], cwd: Path | None = None, timeout: int = 10) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, errors="replace", timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 127, ""
    return proc.returncode, proc.stdout + proc.stderr


def _expand(path: str) -> str:
    return os.path.expanduser(path)


# ----- parsers ------------------------------------------------------------------
_SCP_RE = re.compile(r"^(?:[^@/]+@)?([^:/]+):(?!//).+")


def remote_kind(url: str) -> tuple[str, str | None]:
    url = (url or "").strip()
    if not url:
        return "none", None
    if url.startswith("ssh://"):
        host = url[6:].split("/", 1)[0]
        host = host.split("@")[-1].split(":")[0]
        return "ssh", host
    if url.startswith(("http://", "https://")):
        host = url.split("//", 1)[1].split("/", 1)[0].split("@")[-1]
        return "https", host
    m = _SCP_RE.match(url)
    if m and not url.startswith(("/", ".", "~")):
        return "ssh", m.group(1)
    return "local", None


def parse_ssh_config(text: str) -> dict:
    out: dict = {"hostname": None, "user": None, "identity_files": []}
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        key, value = parts[0].lower(), parts[1].strip()
        if key == "hostname":
            out["hostname"] = value
        elif key == "user":
            out["user"] = value
        elif key == "identityfile":
            out["identity_files"].append(value)
    return out


def parse_ssh_add(text: str) -> list[str]:
    fps = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].startswith(("SHA256:", "MD5:")):
            fps.append(parts[1])
    return fps


def parse_keygen(text: str) -> dict:
    parts = text.strip().split()
    if len(parts) < 3:
        return {}
    comment = " ".join(parts[2:-1]) if len(parts) > 3 else ""
    key_type = parts[-1].strip("()") if parts[-1].startswith("(") else ""
    return {"bits": int(parts[0]) if parts[0].isdigit() else 0, "fingerprint": parts[1], "comment": comment, "type": key_type}


def _ts(value: str) -> str | None:
    if not value or not value.isdigit():
        return None
    return datetime.fromtimestamp(int(value), UTC).date().isoformat()


def parse_gpg_colons(text: str) -> dict | None:
    key: dict | None = None
    for line in text.splitlines():
        f = line.split(":")
        if f[0] == "pub" and key is None:
            validity = f[1] if len(f) > 1 else ""
            status = {"e": "expired", "r": "revoked", "i": "invalid"}.get(validity, "ok")
            key = {"key_id": f[4], "fingerprint": None, "uid": None, "created": _ts(f[5]), "expires": _ts(f[6]) if len(f) > 6 else None, "status": status}
        elif f[0] == "fpr" and key and key["fingerprint"] is None:
            key["fingerprint"] = f[9] if len(f) > 9 else None
        elif f[0] == "uid" and key and key["uid"] is None:
            key["uid"] = f[9] if len(f) > 9 else None
    return key


def parse_config_origin(text: str) -> tuple[str | None, str | None]:
    line = text.strip().splitlines()[0] if text.strip() else ""
    if "\t" not in line:
        return None, None
    origin, value = line.split("\t", 1)
    source = origin.replace("file:", "", 1)
    home = os.path.expanduser("~")
    if source.startswith(home):
        source = "~" + source[len(home):]
    elif source.endswith(".git/config") or source == ".git/config":
        source = "repo .git/config"
    return value, source


# ----- inspection ------------------------------------------------------------------
def _git_config(run: Runner, repo_root: Path, key: str) -> tuple[str | None, str | None]:
    rc, out = run(["git", "config", "--show-origin", "--get", key], repo_root)
    return parse_config_origin(out) if rc == 0 else (None, None)


def _identity(run: Runner, repo_root: Path) -> dict:
    name, name_src = _git_config(run, repo_root, "user.name")
    email, email_src = _git_config(run, repo_root, "user.email")
    return {"name": name, "email": email, "source": name_src or email_src or "unset"}


def _ssh(run: Runner, repo_root: Path) -> dict:
    remotes = gitops.list_remotes(repo_root)
    url = remotes[0].url if remotes else ""
    kind, host = remote_kind(url)
    info: dict = {
        "remote_url": url or None, "kind": kind, "host_alias": host, "resolved_hostname": None, "user": None,
        "identity_files": [], "agent": {"running": False, "loaded": None}, "credential_helper": None,
    }
    if kind == "https":
        helper, _ = _git_config(run, repo_root, "credential.helper")
        info["credential_helper"] = helper or "none configured"
        return info
    if kind != "ssh":
        return info
    rc, out = run(["ssh", "-G", host], repo_root)
    cfg = parse_ssh_config(out) if rc == 0 else {"hostname": None, "user": None, "identity_files": []}
    info["resolved_hostname"] = cfg["hostname"]
    info["user"] = cfg["user"]
    rc, out = run(["ssh-add", "-l"], repo_root)
    agent_fps = parse_ssh_add(out) if rc in (0, 1) else []
    info["agent"]["running"] = rc in (0, 1)
    any_loaded = False
    for ident in cfg["identity_files"]:
        pub = _expand(ident) + ".pub"
        entry = {"path": ident, "exists": os.path.isfile(pub), "type": None, "bits": None, "fingerprint": None, "comment": None, "in_agent": None}
        if entry["exists"]:
            rc, out = run(["ssh-keygen", "-l", "-f", pub], repo_root)
            if rc == 0:
                entry.update(parse_keygen(out))
                entry["in_agent"] = entry["fingerprint"] in agent_fps
                any_loaded = any_loaded or bool(entry["in_agent"])
        info["identity_files"].append(entry)
    info["agent"]["loaded"] = any_loaded if info["agent"]["running"] else None
    return info


def _signing(run: Runner, repo_root: Path) -> dict:
    enabled_raw, _ = _git_config(run, repo_root, "commit.gpgsign")
    enabled = (enabled_raw or "").lower() in ("true", "1", "yes", "on")
    fmt, _ = _git_config(run, repo_root, "gpg.format")
    fmt = (fmt or "openpgp").lower()
    key, _ = _git_config(run, repo_root, "user.signingkey")
    info: dict = {"enabled": enabled, "format": fmt if (enabled or key) else None, "key_id": key, "fingerprint": None, "uid": None, "created": None, "expires": None, "status": "disabled"}
    if not enabled:
        return info
    if not key:
        info["status"] = "missing"
        return info
    if fmt == "ssh":
        pub = _expand(key)
        if os.path.isfile(pub):
            rc, out = run(["ssh-keygen", "-l", "-f", pub], repo_root)
            parsed = parse_keygen(out) if rc == 0 else {}
            info.update({"fingerprint": parsed.get("fingerprint"), "uid": parsed.get("comment"), "status": "ok" if parsed else "missing"})
        else:
            info["status"] = "missing"
        return info
    rc, out = run(["gpg", "--batch", "--list-keys", "--with-colons", key], repo_root)
    parsed = parse_gpg_colons(out) if rc == 0 else None
    if parsed is None:
        info["status"] = "missing"
        return info
    info.update({k: parsed[k] for k in ("key_id", "fingerprint", "uid", "created", "expires", "status")})
    return info


def _head(repo_root: Path) -> dict:
    sha = gitops.head_sha(repo_root)
    if not sha:
        return {"sha": None, "signature": "unsigned", "signer": None, "key_id": None}
    try:
        code, signer, key_id = gitops.signature_status(repo_root, "HEAD")
    except gitops.GitError:
        return {"sha": sha, "signature": "unsigned", "signer": None, "key_id": None}
    return {"sha": sha, "signature": SIGNATURE_WORDS.get(code, "unsigned"), "signer": signer or None, "key_id": key_id or None}


def inspect(repo_root: Path, run: Runner = run_cmd) -> dict:
    return {"identity": _identity(run, repo_root), "ssh": _ssh(run, repo_root), "signing": _signing(run, repo_root), "head": _head(repo_root)}
