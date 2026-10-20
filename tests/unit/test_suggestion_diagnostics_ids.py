"""Driver UUID regression; no database, real LLM, or historical response required."""

import copy
import json
import logging
import uuid

import pytest
from asyncpg.pgproto.pgproto import UUID as AsyncpgUUID
from celery.app.log import TaskFormatter
from celery.utils.log import ColorFormatter

from app.core.logging_setup import CorrelationJsonFormatter, PlainCorrelationFormatter
from app.domain.interfaces.llm_client import LLMSuggestionBatch, LLMSuggestionItem
from app.workers.celery_app import celery_app
from app.workers.pipeline.suggestion_diagnostics import safe_id
from app.workers.pipeline.suggestion_mapper import map_to_suggestions

LOGGER = "syncscribe.workers.suggestion_mapper"
ID = "22222222-2222-2222-2222-222222222222"
PRIVATE = "PRIVATE_VALUE\nforged_log=true"


class UntrustedStringUUID(uuid.UUID):
    def __str__(self):
        return PRIVATE


class RaisingStringUUID(uuid.UUID):
    def __str__(self):
        raise RuntimeError(PRIVATE)


class UntrustedIntegerUUID(uuid.UUID):
    def __getattribute__(self, name):
        if name == "int":
            return PRIVATE
        return super().__getattribute__(name)


class RaisingIntegerUUID(uuid.UUID):
    def __getattribute__(self, name):
        if name == "int":
            raise RuntimeError(PRIVATE)
        return super().__getattribute__(name)


class OutOfRangeUUID(uuid.UUID):
    def __getattribute__(self, name):
        if name == "int":
            return 1 << 128
        return super().__getattribute__(name)


class NegativeIntegerUUID(uuid.UUID):
    def __getattribute__(self, name):
        if name == "int":
            return -1
        return super().__getattribute__(name)


class BooleanIntegerUUID(uuid.UUID):
    def __getattribute__(self, name):
        if name == "int":
            return True
        return super().__getattribute__(name)


class UntrustedObject:
    def __str__(self):
        raise RuntimeError(PRIVATE)


@pytest.mark.parametrize(
    "factory", [uuid.UUID, AsyncpgUUID, UntrustedStringUUID, RaisingStringUUID]
)
def test_safe_id_preserves_canonical_uuid_without_calling_custom_str(factory):
    assert safe_id(factory(ID)) == ID


@pytest.mark.parametrize(
    "value",
    [
        None,
        ID,
        PRIVATE,
        42,
        UntrustedObject(),
        UntrustedIntegerUUID(ID),
        RaisingIntegerUUID(ID),
        OutOfRangeUUID(ID),
        NegativeIntegerUUID(ID),
        BooleanIntegerUUID(ID),
    ],
    ids=[
        "none",
        "uuid-string",
        "private-string",
        "integer",
        "object",
        "private-int",
        "raising-int",
        "oversized-int",
        "negative-int",
        "boolean-int",
    ],
)
def test_safe_id_rejects_invalid_values_without_serializing_them(value):
    assert safe_id(value) == "unavailable"


@pytest.mark.parametrize(
    "text", ["00000000-0000-0000-0000-000000000000", "ffffffff-ffff-ffff-ffff-ffffffffffff"]
)
def test_safe_id_handles_uuid_integer_boundaries(text):
    assert safe_id(AsyncpgUUID(text)) == text


@pytest.mark.parametrize("factory", [AsyncpgUUID, UntrustedStringUUID, RaisingStringUUID])
def test_driver_ids_reach_item_aggregate_console_and_json_without_affecting_mapping(
    caplog, factory
):
    ids = [
        factory(text)
        for text in [
            ID,
            "33333333-3333-3333-3333-333333333333",
            "44444444-4444-4444-4444-444444444444",
        ]
    ]
    batch = LLMSuggestionBatch(
        items=[
            LLMSuggestionItem("section", "modify", "present", "replacement"),
            LLMSuggestionItem("section", "modify", "absent", "replacement"),
        ],
        raw_response=PRIVATE,
    )
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = map_to_suggestions(
            batch, ids[0], ids[1], source_id=ids[2], document_text="present"
        )
    disabled = map_to_suggestions(
        batch, ids[0], ids[1], source_id=ids[2], document_text="present", diagnostics_enabled=False
    )
    columns = lambda suggestions: [
        {
            column.key: getattr(suggestion, column.key)
            for column in suggestion.__mapper__.column_attrs
        }
        for suggestion in suggestions
    ]
    assert len(result) == 1
    assert columns(result) == columns(disabled)
    records = caplog.records
    assert len([r for r in records if hasattr(r, "item_index_0based")]) == 1
    assert any("received=2 kept=1 dropped=1" in r.getMessage() for r in records)
    for record in records:
        assert record.exc_info is None
        assert record.exc_text is None
        rendered = [
            record.getMessage(),
            repr(record.__dict__),
            CorrelationJsonFormatter().format(copy.copy(record)),
            PlainCorrelationFormatter().format(copy.copy(record)),
            TaskFormatter(celery_app.conf.worker_task_log_format).format(copy.copy(record)),
            ColorFormatter(celery_app.conf.worker_log_format).format(copy.copy(record)),
        ]
        assert all(
            "PRIVATE_VALUE" not in output and "forged_log" not in output for output in rendered
        )
        for field, value in zip(["analysis_job_id", "document_id", "source_id"], ids, strict=True):
            expected = str(uuid.UUID(int=value.int))
            assert getattr(record, field) == expected
            assert f"{field}={expected}" in record.getMessage()
            assert (
                json.loads(CorrelationJsonFormatter().format(copy.copy(record)))[field] == expected
            )
