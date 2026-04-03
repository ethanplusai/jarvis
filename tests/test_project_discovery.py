from pathlib import Path

import server


def _init_repo(path: Path, branch: str = "main"):
    path.mkdir(parents=True, exist_ok=True)
    git_dir = path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text(f"ref: refs/heads/{branch}")


def test_scan_projects_sync_includes_repo_root_and_common_roots(tmp_path, monkeypatch):
    repo_root = tmp_path / "VS_Code" / "jarvis"
    _init_repo(repo_root, "main")

    projects_root = tmp_path / "Projects"
    alpha = projects_root / "alpha"
    _init_repo(alpha, "feature-x")

    monkeypatch.setattr(server, "REPO_ROOT", repo_root)
    monkeypatch.setattr(server, "PROJECT_SCAN_ROOTS", [projects_root])

    projects = server._scan_projects_sync()

    assert projects == [
        {"name": "jarvis", "path": str(repo_root.resolve()), "branch": "main"},
        {"name": "alpha", "path": str(alpha.resolve()), "branch": "feature-x"},
    ]


def test_find_project_dir_falls_back_to_fresh_scan_when_cache_is_empty(tmp_path, monkeypatch):
    repo_root = tmp_path / "VS_Code" / "jarvis"
    _init_repo(repo_root, "main")

    monkeypatch.setattr(server, "REPO_ROOT", repo_root)
    monkeypatch.setattr(server, "PROJECT_SCAN_ROOTS", [])
    monkeypatch.setattr(server, "cached_projects", [])

    class _FakeRegistry:
        def get_by_name(self, name: str):
            return None

    monkeypatch.setattr(server, "dispatch_registry", _FakeRegistry())

    assert server._find_project_dir("jarvis") == str(repo_root.resolve())


def test_find_project_dir_uses_dispatch_registry_for_non_git_projects(tmp_path, monkeypatch):
    project_dir = tmp_path / "prototype"
    project_dir.mkdir()

    monkeypatch.setattr(server, "cached_projects", [])
    monkeypatch.setattr(server, "PROJECT_SCAN_ROOTS", [])

    class _FakeRegistry:
        def get_by_name(self, name: str):
            return {"project_path": str(project_dir)}

    monkeypatch.setattr(server, "dispatch_registry", _FakeRegistry())

    assert server._find_project_dir("prototype") == str(project_dir)
