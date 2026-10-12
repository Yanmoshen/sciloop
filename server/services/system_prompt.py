"""Shared SciLoop instructions, bundled with the backend for all deployments."""

from importlib.resources import files


def load_system_prompt() -> str:
    text = (
        files("services")
        .joinpath("prompts/sciloop-system-prompt.zh.md")
        .read_text(encoding="utf-8")
        .strip()
    )
    if not text:
        raise ValueError("SciLoop system prompt must not be empty")
    return text


SCILOOP_SYSTEM_PROMPT = load_system_prompt()
