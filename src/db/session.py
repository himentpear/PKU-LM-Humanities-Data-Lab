"""Database engine, session management, and SQLite FTS5 initialization."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, Index, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from src.db.models import Base

DEFAULT_DB_PATH = Path("data/processed/humanities_lab.db")
DEFAULT_DB_URL = f"sqlite:///{DEFAULT_DB_PATH.as_posix()}"


def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.close()


def create_db_engine(db_url: str = DEFAULT_DB_URL, echo: bool = False) -> Engine:
    """Create a SQLAlchemy Engine with SQLite PRAGMA optimizations when applicable."""
    if db_url.startswith("sqlite:///"):
        db_file = db_url.replace("sqlite:///", "", 1)
        if db_file and db_file != ":memory:":
            Path(db_file).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(db_url, echo=echo, future=True)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _configure_sqlite_connection)
    return engine


def _setup_sqlite_fts5(engine: Engine) -> None:
    """Create SQLite FTS5 virtual tables and sync triggers for OCR text & plate captions."""
    if engine.dialect.name != "sqlite":
        return

    fts_statements = [
        # FTS5 table for OCR text lines (supports IIIF Content Search API)
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_alto_lines USING fts5(
            line_id UNINDEXED,
            canvas_id UNINDEXED,
            content_text,
            tokenize='unicode61'
        );
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_alto_lines_ai AFTER INSERT ON alto_lines BEGIN
            INSERT INTO fts_alto_lines(rowid, line_id, canvas_id, content_text)
            VALUES (new.rowid, new.id, new.canvas_id, new.content_text);
        END;
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_alto_lines_ad AFTER DELETE ON alto_lines BEGIN
            DELETE FROM fts_alto_lines WHERE rowid = old.rowid;
        END;
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_alto_lines_au AFTER UPDATE ON alto_lines BEGIN
            DELETE FROM fts_alto_lines WHERE rowid = old.rowid;
            INSERT INTO fts_alto_lines(rowid, line_id, canvas_id, content_text)
            VALUES (new.rowid, new.id, new.canvas_id, new.content_text);
        END;
        """,
        # FTS5 table for archaeological plates & illustrations
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_plates_figures USING fts5(
            figure_id UNINDEXED,
            manifest_id UNINDEXED,
            figure_number,
            caption_text,
            excavation_unit,
            vessel_type,
            tokenize='unicode61'
        );
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_plates_ai AFTER INSERT ON plates_figures BEGIN
            INSERT INTO fts_plates_figures(
                rowid, figure_id, manifest_id, figure_number, caption_text, excavation_unit, vessel_type
            ) VALUES (
                new.rowid, new.id, new.manifest_id, COALESCE(new.figure_number, ''),
                COALESCE(new.caption_text, ''), COALESCE(new.excavation_unit, ''),
                COALESCE(new.vessel_type, '')
            );
        END;
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_plates_ad AFTER DELETE ON plates_figures BEGIN
            DELETE FROM fts_plates_figures WHERE rowid = old.rowid;
        END;
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_plates_au AFTER UPDATE ON plates_figures BEGIN
            DELETE FROM fts_plates_figures WHERE rowid = old.rowid;
            INSERT INTO fts_plates_figures(rowid,figure_id,manifest_id,figure_number,caption_text,excavation_unit,vessel_type)
            VALUES(new.rowid,new.id,new.manifest_id,COALESCE(new.figure_number,''),COALESCE(new.caption_text,''),
                   COALESCE(new.excavation_unit,''),COALESCE(new.vessel_type,''));
        END;
        """,
    ]

    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS lab_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"))
        migrate=not conn.execute(text("SELECT 1 FROM lab_migrations WHERE name='fts_rowid_v1'")).first()
        if migrate:
            for name in ["trg_alto_lines_ai","trg_alto_lines_ad","trg_alto_lines_au","trg_plates_ai","trg_plates_ad","trg_plates_au"]:
                conn.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
        for stmt in fts_statements:
            conn.execute(text(stmt))
        if migrate:
            conn.execute(text("DELETE FROM fts_alto_lines"))
            conn.execute(text("INSERT INTO fts_alto_lines(rowid,line_id,canvas_id,content_text) SELECT rowid,id,canvas_id,content_text FROM alto_lines"))
            conn.execute(text("DELETE FROM fts_plates_figures"))
            conn.execute(text("INSERT INTO fts_plates_figures(rowid,figure_id,manifest_id,figure_number,caption_text,excavation_unit,vessel_type) SELECT rowid,id,manifest_id,COALESCE(figure_number,''),COALESCE(caption_text,''),COALESCE(excavation_unit,''),COALESCE(vessel_type,'') FROM plates_figures"))
            conn.execute(text("INSERT INTO lab_migrations(name) VALUES ('fts_rowid_v1')"))


def init_db(db_url: str = DEFAULT_DB_URL, echo: bool = False) -> Engine:
    """Initialize all database tables, indexes, and SQLite FTS5 virtual tables."""
    engine = create_db_engine(db_url=db_url, echo=echo)
    Base.metadata.create_all(engine)
    # SQLite checks referencing rows on every cascading deletion. Without these
    # indexes, rebuilding a full report becomes repeated full-table scans.
    for table in Base.metadata.tables.values():
        for foreign_key in table.foreign_keys:
            column=foreign_key.parent
            name=f"ix_fk_{table.name}_{column.name}"
            index=next((i for i in table.indexes if i.name==name),None)
            if index is None:
                index=Index(name,column)
            index.create(bind=engine,checkfirst=True)
    _setup_sqlite_fts5(engine)
    return engine


@contextmanager
def get_session(engine: Engine) -> Iterator[Session]:
    """Transactional session context manager."""
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
