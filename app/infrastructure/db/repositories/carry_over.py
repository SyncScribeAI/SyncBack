"""Копирование строк результата прошлой задачи анализа в новую.

Строки копируются страницами через Core, без ORM-объектов: память ограничена страницей,
а число запросов растёт линейно с числом строк. Новый id выдаётся на стороне Python,
потому что генератор UUID в SQL у PostgreSQL и SQLite разный. created_at копируется,
чтобы перенесённые правки выдавались в том же порядке, что и в прошлой задаче.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Any

from sqlalchemy import Table, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

_PAGE = 500
_REPLACED = frozenset({"id", "analysis_job_id", "carried_from_id"})


async def copy_job_rows(
    session: AsyncSession,
    table: Table,
    from_job_id: uuid.UUID,
    to_job_id: uuid.UUID,
    only: Collection[uuid.UUID] | None = None,
) -> int:
    """Скопировать строки from_job_id в to_job_id; carried_from_id указывает на оригинал.

    only ограничивает копию выбранными строками: инкрементальная задача переносит
    результат только непроверенных блоков. Тогда страница задаётся частью списка id.
    """
    columns = [column for column in table.c if column.name not in _REPLACED]
    copied = 0
    wanted = None if only is None else sorted(only)
    after: uuid.UUID | None = None
    offset = 0
    while True:
        stmt = select(table.c.id, *columns).where(table.c.analysis_job_id == from_job_id)
        if wanted is not None:
            page_ids = wanted[offset : offset + _PAGE]
            if not page_ids:
                return copied
            offset += _PAGE
            stmt = stmt.where(table.c.id.in_(page_ids))
        else:
            stmt = stmt.order_by(table.c.id).limit(_PAGE)
            if after is not None:
                stmt = stmt.where(table.c.id > after)
        rows = (await session.execute(stmt)).mappings().all()
        if rows:
            values: list[dict[str, Any]] = []
            for row in rows:
                value = {column.name: row[column.name] for column in columns}
                value["id"] = uuid.uuid4()
                value["analysis_job_id"] = to_job_id
                value["carried_from_id"] = row["id"]
                values.append(value)
            await session.execute(insert(table), values)
            copied += len(values)
        if wanted is None:
            if len(rows) < _PAGE:
                return copied
            after = rows[-1]["id"]
