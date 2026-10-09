"""内置元搜索内核。

The package follows the SearXNG shape without requiring a second service:
configuration -> engine registry -> concurrent dispatcher -> ranking.
Engine adapters are intentionally small, isolated modules so a source can be
disabled or replaced without changing Agent or API contracts.
"""

from .config import SearchSettings, get_search_settings
from .dispatcher import search, status
from .models import SearchRequest, SearchResponse, SearchResult
from .registry import engine_registry

__all__ = [
    "SearchRequest",
    "SearchResponse",
    "SearchResult",
    "SearchSettings",
    "engine_registry",
    "get_search_settings",
    "search",
    "status",
]
