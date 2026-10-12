"""SQLite URLs anchored to the deployment root, independent of launch cwd."""

from pathlib import Path

from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./knowledge-base/sciloop.sqlite3"


def database_url(value: str, *, asynchronous: bool = True) -> str:
    url = make_url(value.strip())
    if url.drivername not in {"sqlite", "sqlite+aiosqlite"}:
        raise ValueError("本项目仅支持 SQLite，请使用 sqlite+aiosqlite:/// 数据库地址")
    raw = url.database
    if raw and raw != ":memory:":
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        url = url.set(database=str(path.resolve()))
    return url.set(drivername="sqlite+aiosqlite" if asynchronous else "sqlite").render_as_string()
