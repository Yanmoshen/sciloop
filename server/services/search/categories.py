"""Category definitions mirroring SearXNG's category tabs."""

from __future__ import annotations

CATEGORIES: dict[str, tuple[str, ...]] = {
    "general": ("bing", "duckduckgo", "brave", "wikipedia", "wikidata"),
    "images": ("wikimedia_images",),
    "science": ("arxiv", "crossref", "europepmc"),
    "it": ("github", "stackexchange", "mdn", "dockerhub"),
    "news": ("bing_news",),
}


def normalize_categories(value: str | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if isinstance(value, str):
        items = [item.strip().lower() for item in value.split(",")]
    else:
        items = [str(item).strip().lower() for item in (value or ())]
    return tuple(dict.fromkeys(item for item in items if item in CATEGORIES)) or ("general", "science", "it")


def engines_for_categories(categories: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(engine for category in categories for engine in CATEGORIES.get(category, ())))
