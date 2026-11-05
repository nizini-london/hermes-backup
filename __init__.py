"""hermes-backup — encrypted restic backup + verified restore for the Hermes home.

A Hermes Agent plugin. ``register(ctx)`` wires the eight ``backup_*`` tools
(manifest ``provides_tools`` — keep the two in sync, catalog rule 6) and any
bundled recovery skills found under ``skills/`` via ``ctx.register_skill``.
The skills dir may be missing or empty (another worker populates it in
parallel) — a missing skill must never fail plugin load.
"""

from __future__ import annotations

import logging
from pathlib import Path

from schemas import TOOLSET, TOOL_NAMES
import tools

logger = logging.getLogger("hermes-backup")

__all__ = ["register", "TOOL_NAMES"]

TOOL_EMOJI = "💾"


def _register_skills(ctx) -> int:
    """Register skills/*/SKILL.md (restore + drill recipes). Tolerates a
    missing/empty dir; one bad skill must not block the rest or the plugin."""
    count = 0
    skills_dir = Path(__file__).resolve().parent / "skills"
    if not skills_dir.is_dir():
        return count
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        name = skill_md.parent.name
        try:
            description = _skill_description(skill_md)
            ctx.register_skill(name=name, path=skill_md, description=description)
            count += 1
        except Exception as exc:
            logger.warning("hermes-backup: skill %s not registered: %s", name, exc)
    return count


def _skill_description(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("description:"):
                return line.split(":", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin loader."""
    from schemas import TOOL_SCHEMAS

    registered = []
    for schema in TOOL_SCHEMAS:
        name = schema["name"]
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schema,
            handler=lambda args, _tool=name, **kwargs: tools.handle(_tool, args, **kwargs),
            emoji=TOOL_EMOJI,
        )
        registered.append(name)
    skills = _register_skills(ctx)
    logger.info("hermes-backup: registered %d tools (%s) and %d skills",
                len(registered), ", ".join(registered), skills)
