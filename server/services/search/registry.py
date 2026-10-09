"""Engine plugin registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .models import SearchRequest, SearchResult

EngineFetcher = Callable[[SearchRequest, Any], Any]


@dataclass(frozen=True, slots=True)
class EngineDefinition:
    name: str
    label: str
    categories: tuple[str, ...]
    shortcut: str
    fetcher: EngineFetcher
    enabled: bool = True
    timeout: float = 8.0
    supports_time_range: bool = False


class EngineRegistry:
    def __init__(self) -> None:
        self._engines: dict[str, EngineDefinition] = {}

    def register(self, definition: EngineDefinition) -> EngineDefinition:
        self._engines[definition.name] = definition
        return definition

    def get(self, name: str) -> EngineDefinition | None:
        return self._engines.get(name)

    def all(self) -> tuple[EngineDefinition, ...]:
        return tuple(self._engines.values())

    def config(self, enabled: set[str] | None = None, disabled: set[str] | None = None) -> list[dict[str, Any]]:
        enabled = enabled or set()
        disabled = disabled or set()
        rows = []
        for item in self.all():
            active = item.enabled and item.name not in disabled and (not enabled or item.name in enabled)
            rows.append(
                {
                    "name": item.name,
                    "label": item.label,
                    "categories": list(item.categories),
                    "shortcut": item.shortcut,
                    "enabled": active,
                    "paging": False,
                    "language_support": item.name not in {"arxiv", "crossref"},
                    "time_range_support": item.supports_time_range,
                    "timeout": item.timeout,
                }
            )
        return rows


engine_registry = EngineRegistry()


def register(
    name: str,
    *,
    label: str,
    categories: tuple[str, ...],
    shortcut: str,
    timeout: float = 8.0,
    supports_time_range: bool = False,
) -> Callable[[EngineFetcher], EngineFetcher]:
    def decorator(fetcher: EngineFetcher) -> EngineFetcher:
        engine_registry.register(
            EngineDefinition(
                name=name,
                label=label,
                categories=categories,
                shortcut=shortcut,
                fetcher=fetcher,
                timeout=timeout,
                supports_time_range=supports_time_range,
            )
        )
        return fetcher

    return decorator
