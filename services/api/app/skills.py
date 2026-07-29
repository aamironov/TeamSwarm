"""Portable Agent Skills discovery and safe instruction loading."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_SKILL_BYTES = 512_000


@dataclass(frozen=True)
class AgentSkill:
    name: str
    description: str
    instructions: str
    path: Path
    content_hash: str
    allowed_tools: tuple[str, ...] = ()

    def snapshot(self) -> dict[str, str | list[str]]:
        return {
            "name": self.name,
            "description": self.description,
            "instructions": self.instructions,
            "content_hash": self.content_hash,
            "allowed_tools": list(self.allowed_tools),
        }


class SkillCatalog:
    """Discover open-standard ``SKILL.md`` packages under configured roots."""

    def __init__(self, roots: list[Path]) -> None:
        self.roots = [root.expanduser().resolve() for root in roots]

    def discover(self) -> list[AgentSkill]:
        skills: dict[str, AgentSkill] = {}
        for root in self.roots:
            if not root.is_dir():
                continue
            for skill_file in sorted(root.glob("*/SKILL.md")):
                skill = self._read(skill_file)
                if skill.name in skills:
                    raise ValueError(f"Duplicate skill name '{skill.name}'.")
                skills[skill.name] = skill
        return sorted(skills.values(), key=lambda skill: skill.name)

    def select(self, names: list[str]) -> list[AgentSkill]:
        available = {skill.name: skill for skill in self.discover()}
        missing = sorted(set(names) - available.keys())
        if missing:
            raise ValueError(f"Unknown skills: {', '.join(missing)}.")
        return [available[name] for name in names]

    @staticmethod
    def _read(skill_file: Path) -> AgentSkill:
        if skill_file.stat().st_size > _MAX_SKILL_BYTES:
            raise ValueError(f"Skill file is too large: {skill_file}.")
        raw = skill_file.read_text(encoding="utf-8")
        if not raw.startswith("---\n"):
            raise ValueError(f"Skill is missing YAML frontmatter: {skill_file}.")
        try:
            frontmatter, instructions = raw[4:].split("\n---\n", maxsplit=1)
        except ValueError as error:
            raise ValueError(f"Skill has invalid YAML frontmatter: {skill_file}.") from error
        metadata = yaml.safe_load(frontmatter)
        if not isinstance(metadata, dict):
            raise ValueError(f"Skill metadata must be a mapping: {skill_file}.")
        name = metadata.get("name")
        description = metadata.get("description")
        if not isinstance(name, str) or not _NAME_PATTERN.fullmatch(name):
            raise ValueError(f"Skill has an invalid name: {skill_file}.")
        if name != skill_file.parent.name:
            raise ValueError(f"Skill name must match its directory: {skill_file}.")
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"Skill is missing a description: {skill_file}.")
        if len(name) > 64 or len(description) > 1024:
            raise ValueError(f"Skill metadata exceeds specification limits: {skill_file}.")
        allowed_tools = metadata.get("allowed-tools", "")
        if not isinstance(allowed_tools, str):
            raise ValueError(f"Skill allowed-tools must be a string: {skill_file}.")
        return AgentSkill(
            name=name,
            description=description.strip(),
            instructions=instructions.strip(),
            path=skill_file.parent.resolve(),
            content_hash=hashlib.sha256(raw.encode()).hexdigest(),
            allowed_tools=tuple(allowed_tools.split()),
        )
