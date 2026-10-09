"""SearXNG-inspired settings with no external service dependency."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DOTENV = dotenv_values(PROJECT_ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    value = (os.getenv(name) or _DOTENV.get(name) or default).strip()
    if "#" in value:
        value = value.split("#", 1)[0].strip()
    return value.strip('"').strip("'").strip()


def _csv(name: str, default: str) -> tuple[str, ...]:
    return tuple(item.strip().lower() for item in _env(name, default).split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class SearchSettings:
    enabled_engines: tuple[str, ...] = ()
    disabled_engines: tuple[str, ...] = ()
    default_categories: tuple[str, ...] = ("general", "science", "it")
    default_language: str = "auto"
    timeout_seconds: float = 8.0
    retries: int = 1
    concurrency: int = 8
    results_per_engine: int = 5
    proxy: str | None = None
    user_agent: str = "SciLoop/0.1 (SearXNG-compatible research search)"
    github_token: str = ""

    @classmethod
    def from_env(cls) -> "SearchSettings":
        proxy = _env("SCILOOP_SEARCH_PROXY") or os.getenv("HTTP_PROXY") or None
        return cls(
            enabled_engines=_csv("SCILOOP_SEARCH_ENGINES", ""),
            disabled_engines=_csv("SCILOOP_SEARCH_DISABLED_ENGINES", ""),
            default_categories=_csv("SCILOOP_SEARCH_CATEGORIES", "general,science,it"),
            default_language=_env("SCILOOP_SEARCH_LANGUAGE", "auto"),
            timeout_seconds=max(1.0, float(_env("SCILOOP_SEARCH_TIMEOUT", "8"))),
            retries=max(0, int(_env("SCILOOP_SEARCH_RETRIES", "1"))),
            concurrency=max(1, int(_env("SCILOOP_SEARCH_CONCURRENCY", "8"))),
            results_per_engine=max(1, int(_env("SCILOOP_SEARCH_RESULTS_PER_ENGINE", "5"))),
            proxy=proxy,
            user_agent=_env("SCILOOP_SEARCH_USER_AGENT", "SciLoop/0.1 (SearXNG-compatible research search)"),
            github_token=_env("GITHUB_TOKEN"),
        )


def get_search_settings() -> SearchSettings:
    return SearchSettings.from_env()
