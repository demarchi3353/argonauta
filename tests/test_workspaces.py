import json
from pathlib import Path

import pytest

from argonauta.workspaces import Registry, RegistryError, config_dir, open_registry


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGONAUTA_CONFIG_DIR", str(tmp_path / "cfg"))
    return open_registry()


def test_config_dir_honours_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGONAUTA_CONFIG_DIR", str(tmp_path / "cfg"))
    assert config_dir() == tmp_path / "cfg"


def test_registry_starts_empty_and_persists(registry, sample_repo):
    assert registry.workspaces == []
    ws = registry.add_workspace("Personal")
    repo = registry.add_repo(ws.id, sample_repo)
    assert repo.name == sample_repo.name and repo.id.startswith("r_") and ws.id.startswith("w_")
    reloaded = Registry(registry.path)
    reloaded.load()
    assert reloaded.get_repo(repo.id).path == sample_repo.resolve()
    data = json.loads(registry.path.read_text())
    assert data["version"] == 1 and data["workspaces"][0]["name"] == "Personal"


def test_add_repo_validates_git_toplevel(registry, tmp_path, sample_repo):
    ws = registry.add_workspace("W")
    with pytest.raises(RegistryError, match="git repository"):
        registry.add_repo(ws.id, tmp_path)
    sub = sample_repo / "calc"
    repo = registry.add_repo(ws.id, sub)
    assert repo.path == sample_repo.resolve()  # normalised to the toplevel
    with pytest.raises(RegistryError, match="already"):
        registry.add_repo(ws.id, sample_repo)
    with pytest.raises(RegistryError, match="workspace"):
        registry.add_repo("w_nope", sample_repo)


def test_remove_and_rename(registry, sample_repo):
    ws = registry.add_workspace("W")
    repo = registry.add_repo(ws.id, sample_repo)
    registry.rename_workspace(ws.id, "Renamed")
    assert registry.get_workspace(ws.id).name == "Renamed"
    registry.remove_repo(repo.id)
    assert registry.get_repo(repo.id) is None
    registry.remove_workspace(ws.id)
    assert registry.workspaces == []
    with pytest.raises(RegistryError):
        registry.remove_workspace(ws.id)


def test_workspace_name_validation(registry):
    with pytest.raises(RegistryError):
        registry.add_workspace("   ")
    with pytest.raises(RegistryError):
        registry.add_workspace("x" * 101)


def test_ensure_repo_adds_to_default(registry, sample_repo):
    repo = registry.ensure_repo(sample_repo)
    assert registry.workspaces[0].name == "Default"
    assert registry.ensure_repo(sample_repo).id == repo.id


def test_get_repo_path_missing(registry, sample_repo, tmp_path):
    import shutil
    ws = registry.add_workspace("W")
    repo = registry.add_repo(ws.id, sample_repo)
    shutil.rmtree(sample_repo)
    d = registry.to_dict()
    entry = d["workspaces"][0]["repos"][0]
    assert entry["id"] == repo.id and entry["exists"] is False and entry["initialized"] is False


def test_corrupt_file_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGONAUTA_CONFIG_DIR", str(tmp_path))
    (tmp_path / "workspaces.json").write_text("{not json")
    with pytest.raises(RegistryError, match="not valid"):
        open_registry()


def test_add_repo_with_a_file_path_is_a_registry_error(registry, sample_repo):
    ws = registry.add_workspace("W")
    with pytest.raises(RegistryError, match="git repository"):
        registry.add_repo(ws.id, sample_repo / "README.md")


def test_concurrent_mutations_are_serialised(registry):
    import threading

    def add(i):
        registry.add_workspace(f"ws-{i}")

    threads = [threading.Thread(target=add, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(registry.workspaces) == 20
    reloaded = Registry(registry.path)
    reloaded.load()
    assert len(reloaded.workspaces) == 20
