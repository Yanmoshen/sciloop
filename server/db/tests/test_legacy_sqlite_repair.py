"""Legacy database migration must preserve user data and relational constraints."""

import sqlite3

import pytest
from sqlalchemy import create_engine

from db.session import _repair_legacy_sqlite


def test_repair_preserves_extra_columns_relations_indexes_and_triggers(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(path) as c:
        c.executescript('''
          CREATE TABLE papers (id BIGINT NOT NULL PRIMARY KEY, title TEXT NOT NULL,
            is_parsed BOOLEAN DEFAULT 'false', extra TEXT CHECK(length(extra)>0));
          CREATE UNIQUE INDEX custom_title ON papers(title);
          CREATE TABLE refs (id INTEGER PRIMARY KEY, paper_id BIGINT REFERENCES papers(id));
          CREATE TABLE audit (title TEXT);
          CREATE VIEW paper_view AS SELECT title,extra FROM papers;
          CREATE TRIGGER paper_added AFTER INSERT ON papers
            BEGIN INSERT INTO audit VALUES(new.title); END;
          INSERT INTO papers VALUES(42,'已有论文','false','保留数据');
          INSERT INTO refs VALUES(1,42);
        ''')
    engine = create_engine(f'sqlite:///{path}')
    _repair_legacy_sqlite(engine)
    _repair_legacy_sqlite(engine)
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT * FROM papers').fetchone() == (42, '已有论文', 0, '保留数据')
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
        assert c.execute('SELECT * FROM paper_view').fetchall() == [('已有论文','保留数据')]
        assert c.execute('PRAGMA foreign_key_list(refs)').fetchone()[2] == 'papers'
        c.execute("INSERT INTO papers(title,extra) VALUES('新论文','保留')")
        assert c.execute("SELECT id,is_parsed,typeof(is_parsed) FROM papers WHERE title='新论文'").fetchone() == (43, 0, 'integer')
        assert c.execute("SELECT title FROM audit ORDER BY rowid").fetchall() == [('已有论文',), ('新论文',)]
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO papers(title,extra) VALUES('新论文','保留')")
        assert 'custom_title' in {row[1] for row in c.execute('PRAGMA index_list(papers)')}
    assert len(list((tmp_path.parent / '.data/database-backups').glob('legacy-before-*.sqlite3'))) == 1
    engine.dispose()


def test_preserves_autoincrement_sequence_after_high_id_deletion(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path / "sequence.sqlite3"}')
    with engine.begin() as c:
        c.exec_driver_sql("CREATE TABLE flags(id INTEGER PRIMARY KEY AUTOINCREMENT,flag BOOLEAN DEFAULT 'false')")
        c.exec_driver_sql('INSERT INTO flags(id) VALUES(100)')
        c.exec_driver_sql('DELETE FROM flags')
    _repair_legacy_sqlite(engine)
    with engine.begin() as c:
        c.exec_driver_sql('INSERT INTO flags DEFAULT VALUES')
        assert c.exec_driver_sql('SELECT id,flag FROM flags').fetchone() == (101,0)
    engine.dispose()


def test_normalizes_only_boolean_columns_and_retains_unknown_values(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path / "values.sqlite3"}')
    with engine.begin() as c:
        c.exec_driver_sql('CREATE TABLE flags(id INTEGER PRIMARY KEY, flag BOOLEAN, note TEXT)')
        c.exec_driver_sql("INSERT INTO flags VALUES(1,'false','false'),(2,'true','true'),(3,NULL,'untouched'),(4,'other','other')")
    _repair_legacy_sqlite(engine)
    with engine.connect() as c:
        assert c.exec_driver_sql('SELECT flag,note FROM flags ORDER BY id').fetchall() == [(0,'false'),(1,'true'),(None,'untouched'),('other','other')]
    engine.dispose()


def test_migration_failure_rolls_back_schema_and_all_rows(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path / "invalid.sqlite3"}')
    with engine.begin() as c:
        c.exec_driver_sql('CREATE TABLE papers(id BIGINT PRIMARY KEY, flag BOOLEAN DEFAULT \'false\')')
        c.exec_driver_sql("INSERT INTO papers VALUES('bad-id','false')")
    with pytest.raises(sqlite3.IntegrityError):
        _repair_legacy_sqlite(engine)
    with engine.connect() as c:
        assert c.exec_driver_sql('PRAGMA table_info(papers)').fetchone()[2] == 'BIGINT'
        assert c.exec_driver_sql('SELECT * FROM papers').fetchall() == [('bad-id','false')]
        assert c.exec_driver_sql("SELECT name FROM sqlite_master WHERE name LIKE '__sciloop_repair_%'").fetchall() == []
    engine.dispose()
