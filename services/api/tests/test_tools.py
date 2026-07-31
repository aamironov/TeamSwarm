from pathlib import Path

from services.api.app.tools import ToolGateway, ToolRequest


def test_gateway_denies_undeclared_and_unapproved_write_tools(tmp_path: Path) -> None:
    gateway = ToolGateway(tmp_path)
    request = ToolRequest(
        name="workspace.write_file",
        arguments={"path": "new.txt", "content": "hello"},
        idempotency_key="write-1",
    )

    undeclared = gateway.execute(request, [], write_approved=True)
    unapproved = gateway.execute(
        request, ["tool:workspace.write_file"], write_approved=False
    )

    assert undeclared.status == "denied"
    assert unapproved.status == "denied"
    assert not (tmp_path / "new.txt").exists()


def test_gateway_writes_inside_workspace_with_approval(tmp_path: Path) -> None:
    gateway = ToolGateway(tmp_path)

    result = gateway.execute(
        ToolRequest(
            name="workspace.write_file",
            arguments={"path": "src/new.txt", "content": "hello"},
            idempotency_key="write-1",
        ),
        ["tool:workspace.write_file"],
        write_approved=True,
    )

    assert result.status == "succeeded"
    assert result.side_effect is True
    assert result.approval_state == "approved"
    assert (tmp_path / "src/new.txt").read_text(encoding="utf-8") == "hello"


def test_gateway_rejects_workspace_escape_and_arbitrary_commands(tmp_path: Path) -> None:
    gateway = ToolGateway(tmp_path)

    escaped = gateway.execute(
        ToolRequest(name="workspace.read_file", arguments={"path": "../secret.txt"}),
        ["tool:workspace.read_file"],
        write_approved=False,
    )
    command = gateway.execute(
        ToolRequest(
            name="workspace.run_command",
            arguments={"command": ["sh", "-c", "echo unsafe"]},
        ),
        ["tool:workspace.run_command"],
        write_approved=False,
    )
    glob_escape = gateway.execute(
        ToolRequest(name="workspace.list_files", arguments={"glob": "../**/*"}),
        ["tool:workspace.list_files"],
        write_approved=False,
    )

    assert escaped.status == "failed"
    assert "escapes" in escaped.output
    assert command.status == "failed"
    assert "allowlist" in command.output
    assert glob_escape.status == "failed"
    assert "inside the workspace" in glob_escape.output
