"""Local Git snapshots for accepted delivery cycles.

Snapshots deliberately use only the local repository.  They never push, create
branches, or rewrite history.  A failed Git operation is reported to the caller
so orchestration can record it without failing a validated delivery.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SnapshotStatus = Literal["created", "unchanged", "unavailable", "failed"]


@dataclass(frozen=True)
class VersionSnapshot:
    status: SnapshotStatus
    revision: str | None
    message: str


class LocalGitVersionControl:
    """Create one local commit for a stable, evaluator-approved cycle."""

    def __init__(self, workspace_root: Path | None = None) -> None:
        self.workspace_root = (workspace_root or Path.cwd()).resolve()

    def snapshot(
        self,
        *,
        run_id: str,
        cycle: int,
        workspace_root: str | Path | None = None,
    ) -> VersionSnapshot:
        start = Path(workspace_root).resolve() if workspace_root else self.workspace_root
        root = self._repository_root(start)
        if root is None:
            return VersionSnapshot("unavailable", None, "Local Git repository is not initialized.")
        if self._run(root, "status", "--porcelain").stdout.strip() == "":
            return VersionSnapshot(
                "unchanged", self._head(root), "Workspace is already represented by HEAD."
            )

        staged = self._run(root, "add", "--all")
        if staged.returncode != 0:
            return VersionSnapshot("failed", self._head(root), self._error(staged))
        if self._run(root, "diff", "--cached", "--quiet").returncode == 0:
            return VersionSnapshot(
                "unchanged", self._head(root), "No staged workspace changes to commit."
            )

        commit = self._run(
            root,
            "commit",
            "-m",
            f"teamswarm: stable run {run_id[:8]} cycle {cycle}",
        )
        if commit.returncode != 0:
            return VersionSnapshot("failed", self._head(root), self._error(commit))
        return VersionSnapshot(
            "created", self._head(root), f"Saved stable delivery cycle {cycle} to local Git."
        )

    def _repository_root(self, start: Path) -> Path | None:
        result = self._run(start, "rev-parse", "--show-toplevel")
        if result.returncode != 0:
            return None
        return Path(result.stdout.strip())

    def _head(self, root: Path) -> str | None:
        result = self._run(root, "rev-parse", "HEAD")
        return result.stdout.strip() if result.returncode == 0 else None

    @staticmethod
    def _error(result: subprocess.CompletedProcess[str]) -> str:
        return result.stderr.strip() or result.stdout.strip() or "Git command failed."

    @staticmethod
    def _run(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", *arguments],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return subprocess.CompletedProcess(
                ["git", *arguments], 1, "", f"Unable to run Git: {error}"
            )
