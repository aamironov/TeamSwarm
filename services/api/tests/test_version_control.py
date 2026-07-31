import subprocess
from pathlib import Path

from services.api.app.version_control import LocalGitVersionControl


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def test_snapshot_targets_the_runs_selected_workspace(tmp_path: Path) -> None:
    service_root = tmp_path / "service"
    service_root.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    _git(target, "init", "-b", "main")
    _git(target, "config", "user.name", "TeamSwarm Test")
    _git(target, "config", "user.email", "teamswarm-test@local")
    (target / "feature.txt").write_text("before", encoding="utf-8")
    _git(target, "add", "--all")
    _git(target, "commit", "-m", "baseline")
    (target / "feature.txt").write_text("after", encoding="utf-8")

    snapshot = LocalGitVersionControl(service_root).snapshot(
        run_id="12345678-test",
        cycle=2,
        workspace_root=target,
    )

    assert snapshot.status == "created"
    assert snapshot.revision == _git(target, "rev-parse", "HEAD")
    assert _git(target, "status", "--porcelain") == ""
