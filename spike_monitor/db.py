from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


engine: Engine | None = None
SessionLocal: sessionmaker[Session] = sessionmaker(autoflush=False, expire_on_commit=False)


def init_engine(url: str) -> Engine:
    global engine
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

    SessionLocal.configure(bind=engine)
    from . import models  # noqa: F401  (register tables)

    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    return engine


def _add_missing_columns(engine: Engine) -> None:
    """Lightweight upgrade: add columns introduced by newer versions to existing tables.

    New columns always have a default, so existing rows stay valid.
    """
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl_type = col.type.compile(engine.dialect)
                default = ""
                if col.default is not None and col.default.is_scalar:
                    value = col.default.arg
                    if isinstance(value, bool):
                        value = int(value)
                    default = f" DEFAULT {value!r}" if isinstance(value, str) else f" DEFAULT {value}"
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl_type}{default}'))


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
