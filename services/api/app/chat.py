import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from .attachments import render_attachments
from .config import get_settings
from .db import SessionLocal
from .models import ChatMessageRecord, ChatRecord, ProjectRecord
from .providers import ModelProvider, get_provider
from .routing import route_task
from .schemas import (
    ChatCreate,
    ChatMessageInput,
    ChatMessageView,
    ChatView,
    ProjectCreate,
    ProjectView,
)


@dataclass(frozen=True)
class ProjectContext:
    text: str
    content_hash: str


class ProjectContextBuilder:
    """Build a bounded, read-only project context from allowlisted directories."""

    ignored_directories = {".git", ".next", ".venv", "node_modules", "__pycache__"}
    preferred_files = (
        "README.md",
        "ARCHITECTURE.md",
        "AGENTS.md",
        "pyproject.toml",
        "package.json",
    )

    def __init__(self, allowed_roots: list[Path] | None = None) -> None:
        self.allowed_roots = allowed_roots or get_settings().project_roots()

    def resolve_directory(self, directory: str) -> Path:
        candidate = Path(directory).expanduser().resolve()
        if not candidate.is_dir():
            raise ValueError("Project directory does not exist or is not a directory.")
        if not any(candidate.is_relative_to(root) for root in self.allowed_roots):
            raise ValueError("Project directory is outside TEAMSWARM_PROJECT_CONTEXT_ROOTS.")
        return candidate

    def build(self, directory: str) -> ProjectContext:
        root = self.resolve_directory(directory)
        files = self._file_index(root)
        snippets = []
        for filename in self.preferred_files:
            path = root / filename
            if path.is_file():
                content = path.read_text(errors="replace")[:2_000]
                snippets.append(f"--- {filename} ---\n{content}")
        manifest = "\n".join(files)
        reference_files = "\n\n".join(snippets) or "(none found)"
        text = (
            f"Project directory: {root}\n\nProject file manifest:\n{manifest or '(empty)'}"
            f"\n\nProject reference files:\n{reference_files}"
        )
        return ProjectContext(
            text=text[:12_000],
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
        )

    def _file_index(self, root: Path) -> list[str]:
        paths: list[str] = []
        for current, directories, filenames in os.walk(root):
            directories[:] = [item for item in directories if item not in self.ignored_directories]
            for filename in filenames:
                relative = (Path(current) / filename).relative_to(root)
                paths.append(str(relative))
                if len(paths) >= 120:
                    return sorted(paths)
        return sorted(paths)


class ChatService:
    def __init__(
        self,
        provider: ModelProvider | None = None,
        context_builder: ProjectContextBuilder | None = None,
    ) -> None:
        self.provider = provider or get_provider()
        self.context_builder = context_builder or ProjectContextBuilder()

    async def create_project(self, request: ProjectCreate) -> ProjectView:
        directory = str(self.context_builder.resolve_directory(request.directory))
        async with SessionLocal() as session:
            existing = await session.scalar(
                select(ProjectRecord).where(ProjectRecord.directory == directory)
            )
            if existing:
                return self._project_view(existing)
            project = ProjectRecord(name=request.name or Path(directory).name, directory=directory)
            session.add(project)
            await session.commit()
            await session.refresh(project)
            return self._project_view(project)

    async def list_projects(self) -> list[ProjectView]:
        async with SessionLocal() as session:
            projects = list((await session.scalars(select(ProjectRecord))).all())
            return [self._project_view(project) for project in projects]

    async def create_chat(self, request: ChatCreate) -> ChatView:
        async with SessionLocal() as session:
            if await session.get(ProjectRecord, request.project_id) is None:
                raise KeyError(request.project_id)
            chat = ChatRecord(project_id=request.project_id, title=request.title)
            session.add(chat)
            await session.commit()
            await session.refresh(chat)
            return self._chat_view(chat, [])

    async def list_chats(self, project_id: str) -> list[ChatView]:
        async with SessionLocal() as session:
            if await session.get(ProjectRecord, project_id) is None:
                raise KeyError(project_id)
            chats = list(
                (
                    await session.scalars(
                        select(ChatRecord).where(ChatRecord.project_id == project_id)
                    )
                ).all()
            )
            return [self._chat_view(chat, []) for chat in chats]

    async def get_chat(self, chat_id: str) -> ChatView:
        async with SessionLocal() as session:
            chat = await session.get(ChatRecord, chat_id)
            if chat is None:
                raise KeyError(chat_id)
            messages = list(
                (
                    await session.scalars(
                        select(ChatMessageRecord)
                        .where(ChatMessageRecord.chat_id == chat_id)
                        .order_by(ChatMessageRecord.created_at, ChatMessageRecord.id)
                    )
                ).all()
            )
            return self._chat_view(chat, messages)

    async def send_message(self, chat_id: str, request: ChatMessageInput) -> ChatView:
        attachment_text, attachment_metadata = render_attachments(request.attachments)
        async with SessionLocal() as session:
            chat = await session.get(ChatRecord, chat_id)
            if chat is None:
                raise KeyError(chat_id)
            project = await session.get(ProjectRecord, chat.project_id)
            if project is None:
                raise KeyError(chat.project_id)
            session.add(
                ChatMessageRecord(
                    chat_id=chat_id,
                    role="user",
                    content=request.content,
                    attachments=attachment_metadata,
                )
            )
            await session.commit()

        context = self.context_builder.build(project.directory)
        current = await self.get_chat(chat_id)
        prompt_context = f"{context.text}\n\nATTACHED FILES:\n{attachment_text or '(none)'}"
        prompt_hash = hashlib.sha256(prompt_context.encode()).hexdigest()
        route = route_task(get_settings(), request.model_profile, len(prompt_context))
        prompt = self._render_prompt(
            context, current.messages[:-1], request.content, attachment_text
        )
        result = await self.provider.generate(prompt, route.model)

        async with SessionLocal() as session:
            chat = await session.get(ChatRecord, chat_id)
            if chat is None:
                raise KeyError(chat_id)
            session.add(
                ChatMessageRecord(
                    chat_id=chat_id,
                    role="assistant",
                    content=result.text,
                    model=route.model,
                    context_hash=prompt_hash,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                )
            )
            chat.updated_at = datetime.now(UTC)
            await session.commit()
        return await self.get_chat(chat_id)

    @staticmethod
    def _render_prompt(
        context: ProjectContext,
        history: list[ChatMessageView],
        message: str,
        attachment_text: str,
    ) -> str:
        transcript = "\n".join(
            f"{item.role.upper()}: {item.content}" for item in history[-16:]
        )
        return (
            "You are a TeamSwarm project assistant. Use the bounded project context and chat "
            "history. If the context is insufficient, say so.\n\n"
            f"PROJECT CONTEXT:\n{context.text}\n\nCHAT HISTORY:\n{transcript or '(none)'}"
            f"\n\nATTACHED FILES:\n{attachment_text or '(none)'}"
            f"\n\nUSER: {message}\nASSISTANT:"
        )

    @staticmethod
    def _project_view(project: ProjectRecord) -> ProjectView:
        return ProjectView(
            id=project.id,
            name=project.name,
            directory=project.directory,
            created_at=project.created_at,
        )

    @staticmethod
    def _chat_view(chat: ChatRecord, messages: list[ChatMessageRecord]) -> ChatView:
        return ChatView(
            id=chat.id,
            project_id=chat.project_id,
            title=chat.title,
            created_at=chat.created_at,
            updated_at=chat.updated_at,
            messages=[
                ChatMessageView(
                    id=message.id,
                    role=message.role,
                    content=message.content,
                    model=message.model,
                    context_hash=message.context_hash,
                    attachments=message.attachments or [],
                    created_at=message.created_at,
                )
                for message in messages
            ],
        )
