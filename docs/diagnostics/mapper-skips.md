# Safe mapper skip diagnostics

This is diagnostic only. `_normalize`, `_resolve_anchor`, `_skip_reason` and
Suggestion construction retain their acceptance and persistence semantics. No
prompt, API, UI, job lifecycle or database schema changes. Existing results are
not reprocessed. Fixtures are synthetic and do not establish the cause of a
historical incident; no historical prompt/response/source was available.

## Output

At most 20 per-item warning records per mapper call, with the same metadata in
`message` (including the Celery console) and `extra` (JSON):

- `analysis_job_id`, `document_id`, `source_id` (canonical UUID or fixed
  `unavailable`). Driver UUID subclasses, including asyncpg's, are supported.
  IDs are reconstructed from a validated 128-bit integer, never by invoking a
  subclass's `__str__`; strings, invalid integers and failing accessors are
  rejected without serializing their values or exceptions.
- `item_index_0based`: index in the original `batch.items`, starting at **zero**.
- `original_change_type`, `final_change_type`: `add`, `modify`, `delete`, `unknown`.
- `reason`: the existing fixed skip reason.
- `raw_old_text_len`, `raw_insert_after_len`: lengths before normalization.
- `anchor_origin`: `insert_after`, `legacy_old_text`, `normalized_modify_tail`,
  `inferred_section`, `missing`, `not_applicable`.
- `anchor_len`: selected anchor length before the final strip when available;
  inferred anchors and modify-tail anchors have already been stripped by existing
  mapper code. Raw model fields retain their separate length counters.
- `checked_old_text_len`: length after the strip used by `_skip_reason`.
- `document_len` and fixed `mismatch_category`.

The existing aggregate warnings remain, including received, mapped/kept, dropped,
detail records emitted/omitted, and reason counts. `kept` means constructed by the
mapper, **not** proof of a subsequent database commit. An empty batch is not a
loss and produces no skip warning. The source UUID is passed by the worker; direct
mapper callers can omit it. No user/LLM-controlled string is a metadata value.

## Meaning and limits

For text-mismatch reasons only, independently transform both quote and document:
space variants, CRLF to LF, NFC (including decomposed NFD input), tabs to spaces,
removal of U+200B/U+FEFF/U+00AD/U+2060, casefold, and unescaping only the prompt's
`&lt;document` / `&lt;source` delimiter prefixes (including closing forms).

A category means **a substring matched after that transformation**, not that it
caused an incident. These checks never accept an edit or supply an export anchor.
First successful category wins; categories are not exhaustive and transformations
are not combined. `unknown` means no tested transformation matched.
`ambiguous` means the first successful transformation has multiple (including
overlapping) occurrences. No first occurrence is selected as a location. Exact
repeated anchors retain their existing acceptance semantics.

No source text is compared: no "copied from source" attribution is made. No fuzzy
search, closest-fragment search, divergence offset, character codes, content hash,
excerpt or full-text mode is implemented. Detailed character diagnostics are
**unavailable**, not enabled by an environment variable. Normal metadata is on by
default; `diagnostics_enabled=False` is an internal mapper/testing switch that
suppresses per-item diagnostics but retains aggregate warnings. No app setting
or worker/API flag was added.

Classification skips input above 200,000 document characters or 8,192 checked
quote input characters (`input_limit`). A per-batch budget allows at most
2,000,000 input characters across paired transformations (`budget_limit`);
transformed strings are discarded between checks, not cached for the batch.
At most seven fixed transformations are attempted per record, at most two
occurrences inspected per transformation, and only 20 dropped items are diagnosed.
Classification failures produce `diagnostic_error`, without exception details or
tracebacks, and do not change mapping. Validation/normalization themselves retain
their original cost and behavior; these limits bound additional classification.

## Existing behavior deliberately retained

For modify/delete, validation checks `old_text.strip()` while `original_text`
stores the unstripped `item.old_text`. This inconsistency is observable and left
for a separate behavior-changing PR. Adds store their resolved/stripped anchor.
DOC/DOCX reject multiline modify/delete but not multiline add; TXT/Markdown do
not apply that paragraph restriction. No validation was relaxed in this change.

The application plain/JSON formatters and Celery `TaskFormatter` are exercised
with the records themselves, including `args`, `extra` and exception fields.
No logger configuration is changed. These tests establish the mapper's output
contract, not the logging configuration of an uninspected running container.

## Verification for this PR

Sandbox Python 3.12, isolated venv, dependencies installed with
`pip install -e '.[dev]'`, local ignored `.env` copied only from `.env.example`.
No production services, credentials or actual LLM calls were used. A new index
regression was run on main before implementation and failed as expected:
`1 failed`, missing `item_index_0based=1`. An intermediate Celery formatter import
failed collection and was corrected to `celery.app.log.TaskFormatter` before the
successful runs; configuration errors are not counted as passed tests.

Final commands and observed results are recorded in the PR description. Existing
mypy failures were reproduced in a separate detached main worktree:
`suggestion_service.py:88,128,258` (three ORM/protocol type errors). No changes to
that service are included. Migration integration, real infrastructure tests and
Docker image builds were not run locally (no Docker executable); GitHub CI must
run them. Running-container deployment and log configuration are not verified.

Existing `feat/mapper-skip-diagnostics` belonged to the authenticated user and was
one commit ahead of the freshly verified main `1bea69d4c7f28d2881444a234e0fc003df68aa28`.
It had no PR. This PR continues that branch without rewriting it and removes its
unconditional divergence/codepoint output and untrusted change-type logging.
`feat/quote-mismatch-diagnostics` was also inspected; its standalone helper was
not wired into the mapper and was not reused. No duplicate branch is created.

Observed local results on final code:

- Red regression on main: `1 failed` (missing per-item index).
- Targeted mapper/diagnostics/logging suite: `133 passed`.
- CI non-integration command with coverage: `542 passed, 3 skipped,
  32 deselected, 28 warnings`, total coverage **89.30%** (threshold 70%).
- `ruff check .`, `ruff format --check .` (278 files), both layer scripts,
  `python -c 'import app.main'`, `alembic heads` (single `0032`): passed.
- `mypy app/ --ignore-missing-imports`: failed with the same three baseline errors
  listed above; no diagnostic-module errors.
- A separate executed baseline comparison loaded mapper code from main and
  compared all returned ORM column values across **448 synthetic cases**:
  DOCX/DOC/TXT/Markdown, all edit types plus unknown, missing/whitespace/multiline/
  case-mismatch quotes and normalization variants. All matched.
- AST comparisons confirmed unchanged `_normalize`, `_resolve_anchor`,
  `_skip_reason`, `_with_inferred_note` and `infer_anchor`.

## Response retention is not a diagnostic feature

In this code path `OpenAICompatibleClient` returns the response content in the
in-memory `LLMSuggestionBatch.raw_response`. The worker maps `batch.items` and
persists only the retained suggestions; no application consumer persists
`raw_response`. The Celery task returns only source/status/count metadata on
success. Its Redis result backend therefore does not hold the LLM batch. The
`parsed_doc:<job_id>` Redis cache holds parsed document text, not the model
response. Neither store can recover discarded items from this implementation.

This is a source-code finding, not an inspection of a particular deployment,
provider's request history, proxy or external tracing system. No production
Redis/database/provider access was performed and no response from a past run was
recovered. Full-text retention/logging, new storage and automatic analysis reruns
are intentionally not introduced.

## UUID follow-up verification

Executed in an isolated Python 3.12.8 environment with project dev dependencies
(asyncpg 0.32.0), using `.env.example` test defaults and no external services:

- Before the fix, canonical UUID regression: **3 failed, 1 passed**, including
  the real asyncpg UUID class. An initial fixture collection error was corrected
  before that red run; it is not counted as a successful regression run.
- Targeted UUID + mapper + logging suites: **152 passed**. Driver IDs appear in
  per-item/aggregate messages, JSON extra and Celery formatting. Custom UUID
  string conversions are bypassed; invalid/failing integer accessors are safe.
  Returned Suggestion fields/statuses match with diagnostics enabled/disabled.
- CI unit command: **561 passed, 3 skipped, 32 deselected**, coverage **89.31%**.
- `ruff check .`, `ruff format --check .`, both layer checks, app import,
  `alembic heads` (single head 0032) and `git diff --check`: passed.
- `mypy app/ --ignore-missing-imports`: **failed**, the same three pre-existing
  errors in `suggestion_service.py:88,128,258`, reproduced on the unchanged PR
  head `2681c50c1158d0528a5af096a6565259623ef269` in a detached worktree.
- No Docker build, live database migration, infrastructure integration tests,
  paid LLM request or analysis rerun was performed.
