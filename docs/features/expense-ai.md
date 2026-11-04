# Expense AI - reading a receipt into the expense form

When a receipt is attached on minty-petty-cash-web's Expenses stage, a vision model reads it and the
form's fields (amount, date, description, supplier, account) are **pre-filled as suggestions** the
person confirms. Nothing is saved without their **Add**, a supplier is never created automatically,
and the form stays editable while the read runs (or fails) in the background. The product and
technical plans are Minty's `docs/expense_ai/Stage 1 *.md` (history; their section numbers are the
ones the code cites).

**Moved here from Flask on 2026-11-04 (owner).** Until then this service forwarded the receipt to
Flask's `POST /api/internal/petty-cash/expense-extract`; now it reads it itself, on the engine it
shares with minty-payment-request-api's invoice reader. The answer to the web is unchanged.

## Where it lives

- `POST /reports/expenses/extract` (`petty_cash/routes/expenses.py::extract_expense`) - who may
  ask (`require_edit`, 403) and the file (one PDF / JPEG / PNG of at most 10 MB by its bytes,
  `services/expenses.py::checked_receipt`, 422) are refused with the Expenses step's sentences.
  Past those every outcome is 200 `{suggestions, reason}`; `suggestions` is null on any failure
  and the app shows nothing.
- `petty_cash/services/receipt_reader.py` - the receipt's half: the instruction, the reply's shape
  (`ExpenseSuggestion`), the company's lists sent with it (`build_context`: the same
  `expense_accounts` and `suppliers` the Expenses step offers, so nothing is suggested that cannot
  be chosen) and the check of the reply against those lists (`validate`: an id is accepted only if
  it was sent; a value that fails a check is dropped, never repaired).
- `minty_shared.ai` (minty-shared-py, extra `ai`) - the engine: the switches, the call and its one
  retry, the per-process rate limit, usage and cost, and the ONE log line (numbers only, never a
  value read off a receipt). `minty_shared.model_input` makes the small copy the model reads (an
  image capped at 2048 px and saved once as JPEG; a PDF's first page). The stored receipt is never
  changed: it reaches B2 exactly as uploaded.
- `python manage.py expense_ai_check` - one round-trip call with a generated image, from wherever
  the code runs, to prove the configuration.
- The screen is minty-petty-cash-web's: it marks a suggested value visibly, clears it with one tap
  and never overwrites what the person already typed.

## Provider

**Gemini through the direct API, paid tier** (a decision of 3 September 2026); the Vertex AI route
(`asia-southeast1`, IAM instead of a key, in-region processing) remains implemented and is selected
automatically when `GOOGLE_CLOUD_PROJECT` is set. The paid tier settles that content is not used to
train and no human reads it; it does **not** settle residency - receipts cross a border they
otherwise do not - which belongs in the customer disclosure. The free tier is unusable for customer
receipts, and because the SDK cannot tell the tiers apart the tier is stated in configuration and
the engine shouts when Google's quota errors contradict it.

## Configuration

| Variable | Meaning |
|---|---|
| `EXPENSE_AI_ENABLED` | off unless on - the route answers `disabled` and the app shows no suggestions |
| `EXPENSE_AI_MODEL` (`gemini-3.5-flash`), `EXPENSE_AI_THINKING_LEVEL` (`low`), `EXPENSE_AI_MAX_OUTPUT_TOKENS` (4096), `EXPENSE_AI_TIMEOUT_S` (25) | the call |
| `EXPENSE_AI_RATE_USER_PER_MIN` (10), `EXPENSE_AI_RATE_ENTITY_PER_HOUR` (200) | in-process rate limits per person and per company |
| `EXPENSE_AI_CONF_HIGH` (0.85), `EXPENSE_AI_CONF_MEDIUM` (0.60) | the confidence bands |
| `EXPENSE_AI_MAX_FILE_MB` (10) | the receipt size cap |
| `GEMINI_API_KEY`, `EXPENSE_AI_ALLOW_DIRECT_API`, `EXPENSE_AI_DIRECT_TIER`, `EXPENSE_AI_USE_VERTEX`, `EXPENSE_AI_LOCATION`, `GOOGLE_CLOUD_PROJECT` | the route and the tier assertion - shared by every reader |

## Tests

`petty_cash/tests/test_expense_extract.py` - the route (who may ask, the file refusals, the silent
reasons), the context sent (only switched-on accounts, a stable order), the reply checks (ids not
sent are dropped, bounds, bands, the detected supplier name), the one log line, and the check
command; the model is a stand-in. The engine's own cases (retries, timeouts, 429/5xx, cost, the
tier tripwire) are minty-shared-py's `tests/test_ai.py`.
