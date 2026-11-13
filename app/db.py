"""SQLite 数据层（SQLAlchemy Core/ORM），字段类型兼容 PostgreSQL 迁移。

不引入 Alembic：骨架阶段用 ``create_all`` 建表，表结构变更通过 ``init-db --force`` 重建。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base

_engine = None
_SessionFactory: sessionmaker | None = None


def init_engine(url: str | None = None):
    """初始化全局 engine。url 形如 ``sqlite:///data/qoder.db``。

    并发要点：
    - ``check_same_thread=False``：允许跨线程使用连接（并发注册需要）
    - ``timeout=30``：SQLite 写锁等待 30 秒，避免 "database is locked"
    - WAL 模式：读写不互斥，并发注册时读不被写阻塞
    - ``busy_timeout``：与 timeout 配合，进一步降冲突
    """
    global _engine, _SessionFactory
    url = url or os.environ.get("QODER_DB_URL", "sqlite:///data/qoder.db")

    is_sqlite = url.startswith("sqlite:///")
    if is_sqlite:
        db_path = Path(url.replace("sqlite:///", "", 1))
        if not db_path.is_absolute():
            db_path = Path.cwd() / db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{db_path.as_posix()}"

    connect_args = {"check_same_thread": False, "timeout": 30} if is_sqlite else {}
    _engine = create_engine(url, future=True, echo=False, connect_args=connect_args)

    if is_sqlite:
        from sqlalchemy import event

        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.execute("PRAGMA foreign_keys=ON")
            finally:
                cur.close()

    _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_engine():
    if _engine is None:
        init_engine()
    return _engine


def create_all(drop: bool = False) -> None:
    engine = get_engine()
    if drop:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务上下文：正常提交，异常回滚。"""
    if _SessionFactory is None:
        init_engine()
    assert _SessionFactory is not None
    s = _SessionFactory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_session() -> Session:
    if _SessionFactory is None:
        init_engine()
    assert _SessionFactory is not None
    return _SessionFactory()
