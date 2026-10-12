"""Backend-wide pytest cleanup hooks."""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(scope="session", autouse=True)
def dispose_database_engines_after_session():
    """Close lazy SQLAlchemy/aiosqlite pools so pytest can terminate cleanly."""

    yield

    from db import session as db_session

    asyncio.run(db_session.dispose_engines())
