"""Capability-scoped local Tool Gateway for agent workspace operations."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError


class ToolRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)


@dataclass(frozen=True)
class ToolResult:
    status: str
    output: str
    side_effect: bool
    approval_state: str
    rollback: dict[str, Any] | None = None

    @property
    def output_hash(self) -> str:
        return hashlib.sha256(self.output.encode()).hexdigest()


class ToolGateway:
    """Validate and execute a small, auditable local tool registry."""

    version = "local-v1"
    write_tools = {"workspace.write_file", "workspace.replace_text"}
    registry = {
        "workspace.list_files": {
            "description": "List workspace files using an optional glob.",
            "arguments": {"glob": "optional string, default '**/*'", "limit": "optional integer"},
        },
        "workspace.read_file": {
            "description": "Read a UTF-8 workspace file.",
            "arguments": {"path": "relative string"},
        },
        "workspace.write_file": {
            "description": "Create or replace a UTF-8 workspace file; requires write approval.",
            "arguments": {"path": "relative string", "content": "string"},
        },
        "workspace.replace_text": {
            "description": "Replace exact text in a workspace file; requires write approval.",
            "arguments": {
                "path": "relative string",
                "old": "string",
                "new": "string",
                "expected_replacements": "optional integer, default 1",
            },
        },
        "workspace.run_command": {
            "description": "Run an allowlisted test, lint, build, or read-only Git command.",
            "arguments": {
                "command": (
                    "array beginning with uv run pytest, uv run ruff, npm test, "
                    "npm run build, git status, git diff --check"
                ),
                "cwd": "optional relative directory",
            },
        },
        "git.status": {
            "description": "Return concise local Git status.",
            "arguments": {},
        },
    }

    def __init__(
        self,
        workspace_root: Path,
        timeout_seconds: int = 120,
        max_output_chars: int = 50_000,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars

    def prompt_catalog(self, permissions: list[str]) -> str:
        allowed = [
            (name, definition)
            for name, definition in self.registry.items()
            if f"tool:{name}" in permissions
        ]
        if not allowed:
            return "No tools are authorized for this task."
        rendered = "\n".join(
            f"- {name}: {definition['description']} Arguments: "
            f"{json.dumps(definition['arguments'], sort_keys=True)}"
            for name, definition in allowed
        )
        return (
            "Authorized tools:\n"
            f"{rendered}\n"
            'To call one tool, return only: TOOL_CALL: {"name":"...",'
            '"arguments":{...},"idempotency_key":"required-for-writes"}\n'
            "After a tool result, continue until the objective is complete, then return the "
            "requested final output without a TOOL_CALL prefix."
        )

    def execute(
        self,
        request: ToolRequest,
        permissions: list[str],
        write_approved: bool,
    ) -> ToolResult:
        if request.name not in self.registry:
            return ToolResult("denied", f"Unknown tool: {request.name}", False, "not_required")
        if f"tool:{request.name}" not in permissions:
            return ToolResult(
                "denied",
                f"Task capability does not allow {request.name}.",
                request.name in self.write_tools,
                "denied",
            )
        side_effect = request.name in self.write_tools
        if side_effect and not write_approved:
            return ToolResult(
                "denied",
                "Workspace write tools require explicit run approval.",
                True,
                "required",
            )
        if side_effect and not request.idempotency_key:
            return ToolResult(
                "denied",
                "Mutating tool calls require an idempotency_key.",
                True,
                "approved",
            )
        preimage = self._capture_preimage(request) if side_effect else None
        try:
            output = self._dispatch(request)
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            return ToolResult(
                "failed",
                str(error) or type(error).__name__,
                side_effect,
                "approved" if side_effect else "not_required",
            )
        status = (
            "failed"
            if request.name in {"workspace.run_command", "git.status"}
            and not output.startswith("exit_code=0")
            else "succeeded"
        )
        return ToolResult(
            status,
            output[: self.max_output_chars],
            side_effect,
            "approved" if side_effect else "not_required",
            self._complete_rollback(preimage) if preimage else None,
        )

    def rollback(self, payload: dict[str, Any]) -> ToolResult:
        try:
            path_value = payload.get("path")
            before = payload.get("before")
            existed = payload.get("existed")
            expected_hash = payload.get("after_hash")
            if (
                not isinstance(path_value, str)
                or not isinstance(before, str)
                or not isinstance(existed, bool)
                or not isinstance(expected_hash, str)
            ):
                raise ValueError("Rollback payload is invalid.")
            path = self._path(path_value, allow_missing=True)
            current = self._read_text(path) if path.exists() else ""
            current_hash = hashlib.sha256(current.encode()).hexdigest()
            if current_hash != expected_hash:
                return ToolResult(
                    "conflict",
                    f"Rollback skipped {path_value}; the file changed after the tool call.",
                    True,
                    "approved",
                )
            if existed:
                path.write_text(before, encoding="utf-8")
                message = f"Restored {path_value} to its pre-tool content."
            else:
                if path.exists():
                    path.unlink()
                message = f"Removed newly created file {path_value}."
            return ToolResult("succeeded", message, True, "approved")
        except (OSError, ValueError) as error:
            return ToolResult(
                "failed", str(error) or type(error).__name__, True, "approved"
            )

    def _dispatch(self, request: ToolRequest) -> str:
        arguments = request.arguments
        if request.name == "workspace.list_files":
            pattern = self._string(arguments, "glob", default="**/*")
            if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                raise ValueError("Glob pattern must remain inside the workspace.")
            limit = self._integer(arguments, "limit", default=200, minimum=1, maximum=1_000)
            paths = sorted(
                str(path.relative_to(self.workspace_root))
                for path in self.workspace_root.glob(pattern)
                if path.is_file() and ".git" not in path.parts
            )
            return "\n".join(paths[:limit])
        if request.name == "workspace.read_file":
            return self._read_text(self._path(self._string(arguments, "path")))
        if request.name == "workspace.write_file":
            path = self._path(self._string(arguments, "path"), allow_missing=True)
            content = self._string(arguments, "content")
            if len(content) > 500_000:
                raise ValueError("Workspace write content exceeds 500000 characters.")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return f"Wrote {len(content)} characters to {path.relative_to(self.workspace_root)}."
        if request.name == "workspace.replace_text":
            path = self._path(self._string(arguments, "path"))
            old = self._string(arguments, "old")
            new = self._string(arguments, "new")
            if len(new) > 500_000:
                raise ValueError("Replacement content exceeds 500000 characters.")
            expected = self._integer(
                arguments, "expected_replacements", default=1, minimum=1, maximum=100
            )
            content = self._read_text(path)
            actual = content.count(old)
            if actual != expected:
                raise ValueError(f"Expected {expected} replacement(s), found {actual}.")
            path.write_text(content.replace(old, new), encoding="utf-8")
            return f"Replaced {actual} occurrence(s) in {path.relative_to(self.workspace_root)}."
        if request.name == "workspace.run_command":
            command = arguments.get("command")
            if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
                raise ValueError("command must be an array of strings.")
            self._validate_command(command)
            cwd_value = self._string(arguments, "cwd", default=".")
            cwd = self._path(cwd_value)
            if not cwd.is_dir():
                raise ValueError("Command cwd must be a directory.")
            return self._run(command, cwd)
        if request.name == "git.status":
            return self._run(["git", "status", "--short", "--branch"], self.workspace_root)
        raise ValueError(f"Unsupported tool: {request.name}")

    def _path(self, relative: str, allow_missing: bool = False) -> Path:
        if Path(relative).is_absolute():
            raise ValueError("Tool paths must be relative to the workspace.")
        candidate = (self.workspace_root / relative).resolve()
        if not candidate.is_relative_to(self.workspace_root):
            raise ValueError("Tool path escapes the workspace.")
        if ".git" in candidate.relative_to(self.workspace_root).parts:
            raise ValueError("Direct access to .git is forbidden.")
        if not allow_missing and not candidate.exists():
            raise ValueError(f"Workspace path does not exist: {relative}")
        return candidate

    def _capture_preimage(self, request: ToolRequest) -> dict[str, Any]:
        path_value = self._string(request.arguments, "path")
        path = self._path(path_value, allow_missing=True)
        existed = path.exists()
        before = self._read_text(path) if existed else ""
        return {"path": path_value, "existed": existed, "before": before}

    def _complete_rollback(self, preimage: dict[str, Any]) -> dict[str, Any]:
        path = self._path(str(preimage["path"]), allow_missing=True)
        current = self._read_text(path) if path.exists() else ""
        return {
            **preimage,
            "after_hash": hashlib.sha256(current.encode()).hexdigest(),
        }

    @staticmethod
    def _validate_command(command: list[str]) -> None:
        allowed_prefixes = (
            ["uv", "run", "pytest"],
            ["uv", "run", "ruff", "check"],
            ["npm", "test"],
            ["npm", "run", "build"],
            ["git", "status"],
            ["git", "diff", "--check"],
        )
        if not any(command[: len(prefix)] == prefix for prefix in allowed_prefixes):
            raise ValueError("Command is not in the Tool Gateway allowlist.")
        if any(item in {";", "&&", "||", "|", ">", ">>", "<"} for item in command):
            raise ValueError("Shell control operators are forbidden.")
        path_candidates = [
            item.split("=", maxsplit=1)[-1] if "=" in item else item
            for item in command[1:]
        ]
        if any(
            Path(item).is_absolute() or ".." in Path(item).parts
            for item in path_candidates
        ):
            raise ValueError("Command arguments must remain inside the workspace.")

    @staticmethod
    def _read_text(path: Path) -> str:
        if path.stat().st_size > 2_000_000:
            raise ValueError("Workspace file exceeds the 2000000-byte tool limit.")
        return path.read_text(encoding="utf-8")

    def _run(self, command: list[str], cwd: Path) -> str:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
        return f"exit_code={completed.returncode}\n{output}"

    @staticmethod
    def _string(arguments: dict[str, Any], key: str, default: str | None = None) -> str:
        value = arguments.get(key, default)
        if not isinstance(value, str) or (default is None and not value):
            raise ValueError(f"{key} must be a non-empty string.")
        return value

    @staticmethod
    def _integer(
        arguments: dict[str, Any],
        key: str,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        value = arguments.get(key, default)
        if not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{key} must be an integer from {minimum} to {maximum}.")
        return value


def parse_tool_request(text: str) -> ToolRequest | None:
    stripped = text.strip()
    if not stripped.startswith("TOOL_CALL:"):
        return None
    try:
        return ToolRequest.model_validate_json(stripped.removeprefix("TOOL_CALL:").strip())
    except ValidationError as error:
        raise ValueError(f"Invalid tool call: {error}") from error
