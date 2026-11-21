"""Hermes Tingle profile-level commitment tracker plugin."""
from pathlib import Path

try:  # Native plugin import; fallback lets pytest collect plugin-local tests as a top-level package.
    from . import hermes_tingle
    from .schemas import HERMES_TINGLE
except ImportError:  # pragma: no cover - pytest collection only
    import hermes_tingle
    from schemas import HERMES_TINGLE


def register(ctx):
    """Register the Hermes Tingle tool, slash command, and usage guidance."""
    ctx.register_tool(
        name="hermes_tingle",
        toolset="hermes_tingle",
        schema=HERMES_TINGLE,
        handler=lambda args, **kwargs: hermes_tingle.handle_tool(ctx, args, **kwargs),
        description=HERMES_TINGLE["description"],
        emoji="📌",
    )
    ctx.register_command(
        "tingle",
        handler=lambda raw_args, **kwargs: hermes_tingle.handle_command(ctx, raw_args, **kwargs),
        description="Manage Hermes Tingle commitments",
        args_hint="[list|done <ID>|snooze <ID> <duration>|dismiss <ID>]",
    )
    skill_path = Path(__file__).parent / "skills" / "usage" / "SKILL.md"
    if skill_path.is_file():
        ctx.register_skill("usage", skill_path)
    # Runtime integration wires chat history and admitted-source discovery.
    hermes_tingle.integrate(ctx)
