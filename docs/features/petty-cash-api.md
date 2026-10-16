# Petty Cash API - what minty-petty-cash-web calls

This service is the Django home of the bearer JSON API minty-petty-cash-web (port 3010) reads and writes a company's petty cash through. Until it took over, the API lived in Flask as the blueprint `blueprints/petty_cash_api/` (Minty branch `petty-cash-api`; its own `docs/features/petty-cash-api.md`). **The paths and the JSON are Flask's, byte for byte**: the web app moves by changing `PETTY_CASH_API_URL` alone (owner, 2026-10-14). Flask stays the only token minter (`/handoff/petty-cash-web`) and the only holder of Xero credentials.

Status (2026-10-15): the door and every step of the report wizard are ported - Opening to Submit and the outcome - each proved by tests and by `scripts/parity.py` against Flask (MATCH on three local companies). On 2026-10-15 minty-petty-cash-web's `development` was walked through a whole day against this service (E2E Petty Cash Shop, 2026-10-14: Opening, Sales, an expense with a receipt, Deposit, a short count with its reason, Ending, Finish) - every call answered here, every page checked at 360, 768 and 1440 px. Every route Flask's Petty Cash API serves is now served here.

## The door (`core/auth.py`) - fails closed

Every route is under `/api/petty-cash/v1/companies/{entity_id}`. The company comes from that address and nowhere else: not the body, not the token's `entity_id` claim, not a header. `NinjaAPI(auth=PettyCashBearerAuth())` gates every endpoint, so a new route is gated without being told. Checks, in Flask's `guard.py` order:

| Check | Refusal |
|---|---|
| A preflight | answered by django-cors-headers (`200` - Flask answered `204`), no token needed |
| A valid HS256 token (the shared `SECRET_KEY`, 60 s clock leeway) carrying `module == "billing"` (Flask's `MODULE_TOKEN_CLAIM`) and a `user_id` | `401 {"error": "unauthorized"}` |
| The person exists and is `approved` | `401` |
| Membership (an approved `user_entity` row), or a system superuser. A stranger, an unknown id and a malformed id all get the same answer. | `403` "You don't have access to that company." |
| The company is past onboarding | `409` "This company is still setting up. Finish setting it up to use Petty Cash." |
| Petty Cash is on (`core/entitlements.py`, every unknown answers no) | `403` "Petty Cash isn't switched on for this company. An admin can turn it on in the company's modules." |

**Responses** (`core/exceptions.py`, `core/middleware.py`): always JSON. A refusal is `{"error": "<sentence>"}` - a sentence the app shows as it stands - sometimes with `to`, `field` or `report_id` the app routes on. `unauthorized` sends the app back through Flask's hand-off. A 500 says "Something went wrong on our side. Please try again." and the detail goes to the log only. An unknown path is a JSON `404`. Every response is `Cache-Control: no-store`.

**CORS**: `PETTY_CASH_WEB_URL` (default `http://localhost:3010`; `CORS_ALLOWED_ORIGINS` overrides) and a `petty-cash` preview set where `PREVIEW_ORIGIN_REGEX` allows it (`config/preview_origin.py`, refuses to boot on a pattern that could match other hosts); methods `GET, POST, PUT, PATCH, DELETE, OPTIONS`; headers `authorization, content-type` (no `x-entity-id`). Refusals carry CORS too, so the app can see its 401.

## Endpoints

| Endpoint | Status here |
|---|---|
| `GET ""` - the company (`id`, `name`, `role`, `role_label`, `currency {code, decimal_places}` or null) and the person (`id`, `first_name`, `last_name`, `name`, `initials`, `email`) | **ported** (`routes/company.py`) |
| `GET /home` | **ported** (`routes/home.py`) - "today" is Hong Kong's (Flask used the server's clock) |
| `POST /deposit-correction` | **forwarded to Flask** (`routes/home.py`, `core/accounts_api.py`; the owner's choice, 2026-10-15) - it can reverse and re-post the deposit's Xero BankTransfers, and this service holds no Xero code. `REPORT_EDIT_ENTITY` is checked here (a refusal never reaches Flask); the body goes to Flask's `POST /api/internal/xero/deposit-correction` under a 60 s assertion (scope `petty-cash-deposit-correction`, the company and the person in the claims), which re-checks both and answers as Flask's Petty Cash API does - that answer is handed back unchanged. No usable answer is 503, a timeout 504, both saying to check the day (the correction may have reached Xero); Flask refusing OUR assertion is a 500, never a 401 that would send the person through the hand-off. Waits up to 125 s (Flask allows 120; gunicorn here 130) |
| `GET`/`PUT /reports/opening` | **ported** (`routes/opening.py`) |
| `GET`/`PUT /reports/sales` | **ported** (`routes/sales.py`) |
| `GET`/`PUT /reports/expenses`, `POST /reports/expenses/lines`, `DELETE /expense-lines/{id}`, `GET /expense-lines/{id}/receipts/{attachment id}` | **ported** (`routes/expenses.py`) - a failed save deletes its upload; a line's receipts leave B2 only after the commit |
| `POST /suppliers` | **ported** (`routes/expenses.py`, `services/suppliers.py`) - the access token comes from Flask's `POST /api/internal/xero/token` (`core/xero_tokens.py`, a 60 s assertion naming the company); a reconnect-needed connection is 409, the token service down 503 |
| `GET`/`PUT /reports/deposit`, `GET`/`PUT /reports/cash-count` | **ported** (`routes/cash.py`) - the owner's three cash rules of 2026-10-15 |
| `GET /reports/close`, `POST /reports/submit`, `GET /reports/{id}/outcome` | **ported** (`routes/close.py`, `services/submit.py`) - from Flask's `5501587`, plus the working-tree change that a resubmitted report is no longer `xero_integrated` |
| `GET /reports?status=&start_date=&end_date=&page=&per_page=` | **ported** (`routes/history.py`, `services/history.py`; from Flask's `cbebb91`) - filtered (the Status pills as SQL), counted and paged in SQL. A failed report's publish reasons come from Flask's `POST /api/internal/xero/publish-reasons` (`core/accounts_api.py::publish_reasons`; the owner's choice, 2026-10-16 - working out "resolved" may ask Xero), asked only for the failed rows on the page; if Flask can't give them the page is a 503, never failed rows shown with no reasons |
| `GET /reports/{id}`, `POST /reports/{id}/reopen`, `DELETE /reports/{id}`, `POST /reports/{id}/share-link` | **ported** (`routes/report.py`, `services/report_actions.py`, `services/share.py`; from Flask's `e2eb42e`) - the router is mounted last so `/reports/{id}` never answers for a step. Delete names every table it empties and removes receipts after the commit; the share link is made exactly as Flask makes it and Flask serves it (`ACCOUNTS_API_URL/Minty_Report/...`), with an aware `expires_at` |

Each row's request, answer and refusals are Flask's, as its `docs/features/petty-cash-api.md` lists them; this table is updated as each router lands.

## What this service writes

The day's report and what hangs off it - `report`, `share_link`, and on delete `xero_report_sync`, `xero_bank_transfer`, `xero_bank_transaction`, `report_sale`, `report_expense`, `attachment`, `report_expense_attachment`, `report_cash_count`, `report_history` - plus `entity_cash_detail` on submit and a `xero_contact_sync` row for a new supplier. Everything else is read-only (`shared_models/models.py`). Deliberately not copied from Flask: the denomination resolver's write of a company's order onto the shared `cash_info` rows, and `log_history`'s separate commit that swallowed errors.

## Tests

| File | What |
|---|---|
| `petty_cash/tests/test_deposit_correction.py` | the forward: the assertion's claims, Flask's answers and refusals handed on, the permission checked here, every no-answer case |
| `petty_cash/tests/test_history.py` | the list, every status filter, paging, each refused filter, and the reasons hand-off (asked only for failed rows; a 503 when Flask can't answer) |
| `petty_cash/tests/test_report.py` | the Daily Report, reopen, delete (every table it empties, receipts after the commit) and the share link (its token checked the way Flask's public page checks it) |
| `petty_cash/tests/test_door.py` | every door check and its sentence, CORS and `no-store` on refusals, the preflight, a JSON 404, `/healthz` |
| `test_home.py`, `test_opening.py`, `test_sales.py`, `test_expenses.py`, `test_cash.py`, `test_close.py`, `test_suppliers.py` | each step's reads, saves and refusals - Flask's `tests/test_petty_cash_api_*.py` and `test_report_*_state.py` expectations restated; `RULE` / `FIXED` tests name each deliberate change from Flask |
| `petty_cash/tests/test_openapi_snapshot.py` | every path and method, against `snapshots/openapi.json` (`UPDATE_OPENAPI_SNAPSHOT=1` accepts a change - a decision, not a fix) |
| `petty_cash/tests/test_no_flask_imports.py` | nothing imports the Flask world |
| `petty_cash/tests/test_schema_name.py` | no code spells the schema name (CI also runs it with `?schema=pettycash_alt`) |

B2 is a fake client (`storage.client` patched); HTTP to Flask and Xero is a fake `requests.post`.

`pytest` runs on SQLite (tables from the models). With `MINTY_TEST_PG_URI` set it builds a database from Minty's `docs/schema/01_schema_rebased.sql` (`MINTY_REPO`, default `C:\Github\Minty`) and runs on Postgres - always with a unique `MINTY_TEST_PG_DBNAME`.

## Parity with Flask (`scripts/parity.py`)

`python scripts/parity.py --entity <id> --user <id> --day YYYY-MM-DD [--report <id> ...]` sends the same reads, with the same minted token, to Flask (`--flask`, default `http://127.0.0.1:5010`) and to this service (`--django`, default `http://127.0.0.1:8010`) on one database, and compares the JSON field by field: `MATCH`, `DIFF` (with the fields) or `NOT PORTED`. `EXPECTED_DIFFS` names the differences that are meant (today: `/home`'s `draft.last_edit_seconds`, timing). A route counts as ported only once parity says MATCH. Writes are compared by the tests, not by parity: both services share the database, so a write through one changes what the other reads.
