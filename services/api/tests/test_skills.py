from pathlib import Path

import pytest

from services.api.app.skills import SkillCatalog


def _write_skill(root: Path, name: str, description: str = "Useful skill.") -> None:
    directory = root / name
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nFollow these instructions.",
        encoding="utf-8",
    )


def test_catalog_discovers_and_selects_open_standard_skills(tmp_path: Path) -> None:
    _write_skill(tmp_path, "task-planning")

    catalog = SkillCatalog([tmp_path])
    discovered = catalog.discover()

    assert [skill.name for skill in discovered] == ["task-planning"]
    assert catalog.select(["task-planning"])[0].instructions == "Follow these instructions."


def test_catalog_rejects_unknown_skills(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown skills"):
        SkillCatalog([tmp_path]).select(["missing"])


def test_catalog_requires_directory_and_skill_names_to_match(tmp_path: Path) -> None:
    _write_skill(tmp_path, "directory-name")
    skill_file = tmp_path / "directory-name" / "SKILL.md"
    skill_file.write_text(
        "---\nname: another-name\ndescription: Useful skill.\n---\nInstructions.",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must match"):
        SkillCatalog([tmp_path]).discover()
