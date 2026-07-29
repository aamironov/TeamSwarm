import hashlib
from pathlib import PurePath

from .schemas import PromptAttachmentInput

_TEXT_EXTENSIONS = {
    ".css", ".csv", ".html", ".ini", ".js", ".json", ".jsx", ".log", ".md",
    ".py", ".sql", ".toml", ".ts", ".tsx", ".txt", ".xml", ".yaml", ".yml",
}


def render_attachments(
    attachments: list[PromptAttachmentInput],
) -> tuple[str, list[dict[str, str]]]:
    """Validate text attachments and return prompt text plus safe persisted metadata."""
    total_size = sum(len(item.content.encode()) for item in attachments)
    if total_size > 240_000:
        raise ValueError("Attached files exceed the 240 KB prompt limit.")
    rendered: list[str] = []
    metadata: list[dict[str, str]] = []
    for item in attachments:
        filename = PurePath(item.filename).name
        if filename != item.filename or not filename:
            raise ValueError("Attachment filenames must not contain path components.")
        if PurePath(filename).suffix.lower() not in _TEXT_EXTENSIONS:
            raise ValueError("Only text and source-code files can be attached to prompts.")
        content_hash = hashlib.sha256(item.content.encode()).hexdigest()
        rendered.append(f"--- Attached file: {filename} ---\n{item.content}")
        metadata.append({"filename": filename, "content_hash": content_hash})
    return "\n\n".join(rendered), metadata
