"""Audit layer (spec section 5).

Every INSERT / UPDATE / DELETE on tracked tables is recorded into ``audit_log`` with
table, row id, field, old value, new value, source and timestamp - automatically, via
SQLAlchemy session listeners. Sources come from ``session.info['audit_source']``.

Usage:
    session.info['audit_source'] = 'import:anuga_2024'
    session.add(company)          # audited automatically
    company.tier = 1              # audited automatically

The audit_log table itself is never audited (no recursion).
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from buyerintel.db.models import AuditLog, Base

# Tables whose changes are audited (all model tables except the log itself).
TRACKED = {
    mapper.class_.__tablename__: mapper.class_
    for mapper in Base.registry.mappers
    if mapper.class_ is not AuditLog
}


def _serialize(value: Any) -> str | None:
    if value is None:
        return None
    try:
        json.dumps(value)
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _row_key(obj: Any) -> str | None:
    """Best-effort stable row key; primary key when assigned."""
    insp = obj.__mapper__
    pks = [getattr(obj, col.name, None) for col in insp.primary_key]
    if pks and all(p is not None for p in pks):
        return "/".join(str(p) for p in pks)
    # Fall back to a content key so pre-insert records are still traceable.
    return None


def capture_changes(session: Session, _flush_context, _instances) -> None:
    """Before flush: capture the change set for this flush."""
    pending: list[dict] = []

    for obj in session.deleted:
        table = type(obj).__tablename__
        if table not in TRACKED:
            continue
        for col in obj.__mapper__.column_attrs:
            old = _serialize(getattr(obj, col.key, None))
            pending.append(
                {
                    "table": table,
                    "row_id": _row_key(obj),
                    "field": col.key,
                    "old": old,
                    "new": None,
                    "obj": obj,
                }
            )

    for obj in session.dirty:
        table = type(obj).__tablename__
        if table not in TRACKED:
            continue
        insp = inspect(obj)
        for col in insp.mapper.column_attrs:
            history = insp.attrs[col.key].history
            if not history.has_changes():
                continue
            old = _serialize(history.deleted[0]) if history.deleted else None
            new = _serialize(history.added[0]) if history.added else None
            pending.append(
                {
                    "table": table,
                    "row_id": _row_key(obj),
                    "field": col.key,
                    "old": old,
                    "new": new,
                    "obj": obj,
                }
            )

    for obj in session.new:
        table = type(obj).__tablename__
        if table not in TRACKED:
            continue
        for col in obj.__mapper__.column_attrs:
            new = _serialize(getattr(obj, col.key, None))
            if new is None:
                continue
            pending.append(
                {
                    "table": table,
                    "row_id": None,  # PK not yet assigned
                    "field": col.key,
                    "old": None,
                    "new": new,
                    "obj": obj,
                }
            )

    session.info["audit_pending"] = pending


def write_audit_rows(session: Session, _flush_context) -> None:
    """After flush: primary keys are assigned; materialize audit_log rows."""
    pending = session.info.pop("audit_pending", [])
    if not pending:
        return
    source = session.info.get("audit_source", "unknown")
    rows = []
    for change in pending:
        row_id = change["row_id"] or _row_key(change["obj"])
        rows.append(
            AuditLog(
                table_name=change["table"],
                row_id=row_id,
                field=change["field"],
                old_value=change["old"],
                new_value=change["new"],
                source=source,
            )
        )
    session.add_all(rows)


def install_audit(session_class: type[Session]) -> None:
    """Install the audit listeners on a sessionmaker's session class."""
    event.listen(session_class, "before_flush", capture_changes)
    event.listen(session_class, "after_flush", write_audit_rows)
