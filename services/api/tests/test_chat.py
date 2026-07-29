from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.api.app import chat as chat_module
from services.api.app.chat import ChatService, ProjectContextBuilder
from services.api.app.db import Base
from services.api.app.providers import ModelProvider, ProviderResult
from services.api.app.schemas import ChatCreate, ChatMessageInput, ProjectCreate


class ChatProvider(ModelProvider):
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def generate(self, prompt: str, model: str) -> ProviderResult:
        self.prompts.append(prompt)
        return ProviderResult("Persisted assistant reply", 10, 5, "internally_metered")


@pytest_asyncio.fixture
async def isolated_chat(monkeypatch: pytest.MonkeyPatch, tmp_path) -> AsyncIterator[None]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'chat-test.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(chat_module, "SessionLocal", session_factory)
    yield
    await engine.dispose()


@pytest.mark.asyncio
async def test_chat_persists_history_and_uses_project_context(isolated_chat, tmp_path) -> None:
    project_directory = tmp_path / "project"
    project_directory.mkdir()
    (project_directory / "README.md").write_text("# Example project\nThis is the project context.")
    (project_directory / "app.py").write_text("print('hello')")
    provider = ChatProvider()
    service = ChatService(provider=provider, context_builder=ProjectContextBuilder([tmp_path]))

    project = await service.create_project(ProjectCreate(directory=str(project_directory)))
    chat = await service.create_chat(ChatCreate(project_id=project.id, title="Project chat"))
    updated = await service.send_message(
        chat.id,
        ChatMessageInput(content="What does this project contain?"),
    )
    reloaded = await service.get_chat(chat.id)

    assert [message.role for message in updated.messages] == ["user", "assistant"]
    assert [message.content for message in reloaded.messages] == [
        "What does this project contain?",
        "Persisted assistant reply",
    ]
    assert updated.messages[1].context_hash
    assert "This is the project context." in provider.prompts[0]
    assert "app.py" in provider.prompts[0]


@pytest.mark.asyncio
async def test_project_context_rejects_directories_outside_allowlist(tmp_path) -> None:
    builder = ProjectContextBuilder([tmp_path / "allowed"])
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(ValueError, match="outside"):
        builder.resolve_directory(str(outside))


@pytest.mark.asyncio
async def test_chat_adds_text_file_attachments_to_the_prompt_with_provenance(
    isolated_chat, tmp_path
) -> None:
    project_directory = tmp_path / "project"
    project_directory.mkdir()
    provider = ChatProvider()
    service = ChatService(provider=provider, context_builder=ProjectContextBuilder([tmp_path]))
    project = await service.create_project(ProjectCreate(directory=str(project_directory)))
    chat = await service.create_chat(ChatCreate(project_id=project.id))

    updated = await service.send_message(
        chat.id,
        ChatMessageInput(
            content="Review this file.",
            attachments=[{"filename": "example.py", "content": "print('attached')"}],
        ),
    )

    assert "--- Attached file: example.py ---" in provider.prompts[0]
    assert "print('attached')" in provider.prompts[0]
    assert updated.messages[0].attachments[0].filename == "example.py"
    assert len(updated.messages[0].attachments[0].content_hash) == 64
