from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from services.parsing.card_builder import SqlCardRepository


async def test_set_is_parsed_uses_sqlite_compatible_timestamp() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE papers ("
                    "id INTEGER PRIMARY KEY, is_parsed BOOLEAN, updated_at DATETIME)"
                )
            )
            await connection.execute(
                text("INSERT INTO papers (id, is_parsed) VALUES (1, 0)")
            )

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await SqlCardRepository(session).set_is_parsed(1, True)
            row = (
                await session.execute(
                    text("SELECT is_parsed, updated_at FROM papers WHERE id = :id"),
                    {"id": 1},
                )
            ).mappings().one()

        assert row["is_parsed"] == 1
        assert row["updated_at"]
    finally:
        await engine.dispose()
