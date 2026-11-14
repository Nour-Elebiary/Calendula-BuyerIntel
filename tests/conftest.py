from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from buyerintel.db.audit import install_audit
from buyerintel.db.engine import init_db, make_engine
from buyerintel.db.models import Base


@pytest.fixture()
def engine(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    init_db(engine)
    return engine


@pytest.fixture()
def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture()
def session(session_factory):
    s = session_factory()
    install_audit(type(s))
    s.info["audit_source"] = "test"
    yield s
    s.rollback()
    s.close()
