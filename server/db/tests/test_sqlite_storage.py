"""Exercise production storage operations against disposable SQLite files."""

import json
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from core.config import Settings
from core.database import PROJECT_ROOT, database_url
from db import session as db_session
from db.models.paper import Paper
from db.models.pipeline import PipelineRun, StageOutput
from db.models.project import Project
from db.models.research import ResearchNodeRun


@pytest.fixture
def sqlite_file(tmp_path, monkeypatch):
    path = tmp_path / "sciloop.sqlite3"
    engine = create_engine(database_url(f"sqlite:///{path}", asynchronous=False))
    monkeypatch.setattr(db_session, "sync_engine", engine)
    db_session.create_schema()
    yield path, engine
    engine.dispose()


@pytest_asyncio.fixture
async def async_db(sqlite_file):
    path, _ = sqlite_file
    engine = create_async_engine(database_url(f"sqlite:///{path}"))
    yield engine, async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def test_all_connections_use_same_file_from_other_cwd(tmp_path, monkeypatch):
    from llm.db import normalize_async_dsn

    monkeypatch.chdir(tmp_path)
    config = Settings(_env_file=None, database_url="sqlite:///./knowledge-base/sciloop.sqlite3")
    expected = PROJECT_ROOT / "knowledge-base" / "sciloop.sqlite3"
    for url in (
        config.database_url,
        config.sync_database_url,
        normalize_async_dsn(config.database_url),
    ):
        assert Path(make_url(url).database) == expected
    assert database_url("sqlite:///:memory:") == "sqlite+aiosqlite:///:memory:"


def test_old_database_config_is_rejected():
    with pytest.raises(ValidationError, match="仅支持 SQLite"):
        Settings(_env_file=None, database_url="postgresql://localhost/sciloop")


@pytest.mark.asyncio
async def test_research_upsert_and_restart_preserve_history(async_db):
    from services.research.store import reap_stale_running_runs, upsert_node_run

    _, factory = async_db
    async with factory() as session:
        a = await upsert_node_run(
            session,
            conversation_id="one",
            node="literature_review",
            entry_index=1,
            status="running",
            payload={"资料": ["a"]},
        )
        b = await upsert_node_run(
            session,
            conversation_id="one",
            node="literature_review",
            entry_index=1,
            status="running",
            payload={"资料": ["b"]},
        )
        assert a["id"] == b["id"]
        await upsert_node_run(
            session,
            conversation_id="two",
            node="literature_review",
            entry_index=1,
            status="done",
            payload={"资料": ["c"]},
        )
        session.add_all(
            [
                ResearchNodeRun(node="literature_review", entry_index=1),
                ResearchNodeRun(node="literature_review", entry_index=1),
            ]
        )
        await session.commit()
        assert await reap_stale_running_runs(session) == 1
        session.expire_all()
        rows = (await session.execute(select(ResearchNodeRun))).scalars().all()
        assert len(rows) == 4
        one = next(row for row in rows if row.conversation_id == "one")
        assert one.status == "pending" and one.payload["资料"] == ["b"]
        assert "reaped_reason" in one.payload
        assert next(row for row in rows if row.conversation_id == "two").status == "done"


@pytest.mark.asyncio
async def test_registry_miss_persists_cancel_for_running_worker(async_db):
    from services.research import orchestrator, store

    _, factory = async_db
    async with factory() as session:
        await store.upsert_node_run(
            session,
            conversation_id="cross-worker-stop",
            node="literature_review",
            entry_index=1,
            status="running",
            payload={"existing": "kept"},
        )

    assert await orchestrator.request_persisted_interrupt(factory, "cross-worker-stop")
    with pytest.raises(orchestrator.ResearchInterrupted):
        await orchestrator._raise_if_interrupted("cross-worker-stop", factory)
    await orchestrator._settle_interrupted(
        factory,
        conversation_id="cross-worker-stop",
        project_id=None,
        node="literature_review",
        entry_index=1,
    )

    async with factory() as session:
        row = await store.get_node_run(
            session,
            conversation_id="cross-worker-stop",
            node="literature_review",
            entry_index=1,
        )
    assert row["status"] == "pending"
    assert row["finished_at"] is not None
    assert row["payload"] == {"existing": "kept", "cancel_requested": True}


@pytest.mark.asyncio
async def test_pipeline_upsert_retains_unspecified_output(async_db):
    from services.pipeline.resume import upsert_stage_output

    _, factory = async_db
    async with factory() as session:
        project = Project(name="研究项目")
        session.add(project)
        await session.flush()
        run = PipelineRun(project_id=project.id, iteration=1, mode="manual", status="running")
        session.add(run)
        await session.flush()
        first = await upsert_stage_output(
            session,
            pipeline_run_id=run.id,
            stage="survey",
            attempt=1,
            status="running",
            output_json={"sources": ["a"]},
        )
        second = await upsert_stage_output(
            session, pipeline_run_id=run.id, stage="survey", attempt=1, status="done"
        )
        await session.commit()
        assert first.id == second.id
        session.expire_all()
        row = (await session.execute(select(StageOutput))).scalar_one()
        assert row.status == "done" and row.output_json == {"sources": ["a"]}


@pytest.mark.asyncio
async def test_llm_fixtures_and_models_roundtrip(async_db):
    from llm.registry import SqlModelRegistry
    from llm.store import SqlLLMStore
    from llm.types import CallLogEntry

    engine, _ = async_db
    store = SqlLLMStore(engine=engine)
    log_id = await store.insert_call_log(
        CallLogEntry(provider="local", model="model-a", success=True)
    )
    assert log_id is not None
    await store.upsert_fixture("research", "a", {"资料": ["original"]})
    await store.upsert_fixture("research", "a", {"资料": ["updated"]})
    assert await store.find_fixture("research", "a") == {"资料": ["updated"]}
    registry = SqlModelRegistry(engine=engine)
    config_id = await registry.create_config(
        name="本地模型",
        base_url="https://example.com/v1",
        api_key_enc="",
        models=[{"model_id": "model-a"}],
        is_default=False,
    )
    await registry.update_config(config_id, {"models": [{"model_id": "model-b"}]})
    await registry.mark_tested(config_id, True)
    config = await registry.get_config(config_id)
    assert config.models == [{"model_id": "model-b"}]
    assert config.test_ok is True


def test_paper_category_search_handles_primary_array_and_missing(sqlite_file):
    from api.v1.papers import search_papers

    _, engine = sqlite_file
    with Session(engine) as session:
        session.add_all(
            [
                Paper(
                    source="arxiv",
                    external_id="1",
                    title="Primary",
                    raw={"arxiv": {"primary_category": "cs.CL"}},
                ),
                Paper(
                    source="arxiv",
                    external_id="2",
                    title="Secondary",
                    raw={"arxiv": {"categories": ["cs.AI", "cs.CL"]}},
                ),
                Paper(
                    source="arxiv",
                    external_id="3",
                    title="Other",
                    raw={"arxiv": {"categories": ["cs.AI"]}},
                ),
                Paper(source="arxiv", external_id="4", title="Missing", raw=None),
            ]
        )
        session.commit()
        result = search_papers(
            session=session,
            q=None,
            field="cs.CL",
            source=None,
            parse_status=None,
            sort="published",
            limit=20,
            page=1,
            page_size=20,
        )
        assert result["total"] == 2
        assert {item["title"] for item in result["items"]} == {"Primary", "Secondary"}


def test_provider_seed_sql_is_valid_and_idempotent(sqlite_file):
    import sqlite3

    path, _ = sqlite_file
    sql = (PROJECT_ROOT / "server/fixtures/seed_providers_7.sql").read_text(encoding="utf-8")
    with sqlite3.connect(path) as connection:
        connection.executescript(sql)
        connection.executescript(sql)
        rows = connection.execute("SELECT name, models FROM model_configs").fetchall()
        assert len(rows) == 7 and len({row[0] for row in rows}) == 7
        assert all(isinstance(json.loads(row[1]), list) for row in rows)


def test_schema_initialization_is_repeatable(sqlite_file):
    _, engine = sqlite_file
    with Session(engine) as session:
        session.add(Project(name="保留数据"))
        session.commit()
    db_session.create_schema()
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Project)) == 1


def test_boolean_default_and_paper_trends_day_week_history(sqlite_file):
    from datetime import UTC, datetime, timedelta

    from api.v1.papers import paper_trends

    path, engine = sqlite_file
    now = datetime.now(UTC)
    with Session(engine) as session:
        project = Project(name='中文项目')
        session.add(project)
        session.add_all([
            Paper(source='arxiv', external_id='old', title='旧论文', created_at=now-timedelta(days=9)),
            Paper(source='arxiv', external_id='new', title='新论文', created_at=now),
        ])
        session.commit()
        assert project.archived is False
        day = paper_trends(session=session, days=7, bucket='day')
        assert len(day['axis']) == 7
        assert day['series']['total'][-1] == 2
        assert sum(day['series']['new_papers']) == 1
        week = paper_trends(session=session, days=10, bucket='week')
        assert len(week['axis']) == 2
        assert sum(week['series']['new_papers']) == 2
        history = paper_trends(session=session, days=0, bucket='day')
        assert history['series']['total'][-1] == 2
    import sqlite3

    with sqlite3.connect(path) as c:
        assert c.execute('SELECT archived,typeof(archived) FROM projects').fetchone() == (0, 'integer')


@pytest.mark.asyncio
async def test_project_workspace_keeps_chinese_and_creates_chosen_path(tmp_path, monkeypatch):
    from api.v1.projects import _ensure_workspace
    from services import storage
    from services.agent import host_runner

    requests = []

    async def mkdir(**kwargs):
        requests.append(kwargs)
        return {'ok': True}

    monkeypatch.setattr(storage, 'knowledge_root', lambda: tmp_path)
    monkeypatch.setattr(host_runner, 'call_fs', mkdir)
    path, note = await _ensure_workspace(project_id=7, name='滑动窗口求和', chosen='')
    assert path.replace('\\','/').endswith('/projects/滑动窗口求和')
    assert note is None
    Path(path).mkdir(parents=True)
    duplicate_path, note = await _ensure_workspace(project_id=8, name='滑动窗口求和', chosen='')
    assert duplicate_path.replace('\\','/').endswith('/projects/滑动窗口求和-8')
    assert note is None
    chosen = str(tmp_path / '还没创建' / '我的论文')
    path, note = await _ensure_workspace(project_id=8, name='任选目录', chosen=chosen)
    assert path == chosen.replace('\\','/') and note is None
    assert requests[-1] == {'action':'mkdir','path':path,'workspace_root':path,'access_mode':'workspace'}
