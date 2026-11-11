# A company keeping its books in Minty (Xero optional)

Since 2026-11-10 a company can use Minty with no Xero at all: it chooses _Use Minty independently_ in
onboarding's Accounting Setup (minty-onboarding-api `docs/features/wizard-api.md`), which stores
`entities.setup_mode = 'minty'` (minty-db v0.5's EXPAND; the design is Minty's
`docs/modernisation/xero_optional_database.md`). It made its own accounts there, written for this
API's readers too: `account_info` with its old `xero_code`/`type`/`status`, an expense account ticked
in `entity_account_xero`. The rule here is `xero_settings.minty_on_its_own(entity_id)`: `setup_mode`
is `minty` and there is no Xero organisation. A company that connects Xero later follows Xero's rules
again.

| Where | What changes for such a company |
|---|---|
| Setup Required (`GET /home`, `xero_settings.missing_slots`) | Only the five accounts daily closing uses, named as onboarding's step 4.5 names them: _Petty cash_, _Bank you deposit into_, _Cash sales_, _Paid personally_, _Cash difference_. The three Xero contacts are not asked for, and the discrepancy bank follows the petty cash account. Onboarding lets a company go live with one missing; this is where it is asked again, before the first closing |
| Accounts and suppliers on the expense screens (`GET /reports/expenses`, the report page's options, the receipt reader, the line saves and edits) | Each is named by `expenses.public_id`: its Xero id, or its own id when it is Minty's alone. `find_account` / `find_supplier` take either |
| `POST /suppliers` | Kept in Minty (`xero_contact_sync` with no Xero id, `source = 'user'`), with no Xero call. A name the company already has, ignoring case and spaces, is that supplier, not a second one |
| `POST /reports/{id}/publish` | 409 _This company keeps its books in Minty and isn't connected to Xero, so there's nowhere to publish to._ - before any Xero call |

**The publish writes what it sent** (`publish._snapshot`, on every completed publish, for every
company): each expense line's `published_account_code` / `published_account_name` and
`published_contact_name`. A published report shows those; no sync or later code change rewrites them -
only the next publish.

Tests: `petty_cash/tests/test_minty_on_its_own.py` (both passes).

**Petty Cash Settings** (`GET/PUT /settings/xero*`, `xero_cards`): for such a company the read says
`minty: true` (and `live: false`), the mapping is its five accounts from its own active ones
(`minty_read`, named by `public_id`, each list filtered by Xero's type rules as for a Xero
company), and Xero is never asked. Saving (`minty_save`) writes the five and the discrepancy bank
(it follows the petty cash account); the account codes save as they do for a Xero company.
minty-petty-cash-web draws the same cards for it, titled _Account mapping_, with no contacts and no
"from Xero". So Setup Required's link now leads somewhere that can fix it.

**A disconnected company keeps its organisation** (2026-11-10, minty-accounts-api's disconnect). The
Xero actions here already go by `status` (publish, the Xero cards' `is_live`, a supplier at Xero
answers "reconnect"); Settings' `xero_connected` now does too (`bool(xero_org_id) and status !=
'disconnected'`). Its kept accounts and mapping keep daily closing working.

**A company that banks all its sales** (onboarding 4.2's "We bank all sales",
`entities.uses_sales_cash = false`; owner's brief, 2026-11-10): its Cash method is off, so it never
records cash sales and the publish's cash-sales step never runs. `xero_settings.missing_slots`
leaves out the cash-sale account and contact (Setup Required, the dashboard and the publish gate),
and Petty Cash Settings marks both `optional` in the mapping (`xero_cards._optional`) and saves
without them, clearing them when empty. Adding Cash Sales back later is a Settings change still to
come.

## Connecting Xero later (Settings Phase 2, 2026-11-11)

A Minty company that signs in to Xero is matched first (minty-accounts-api `xero/services/matching.py`):
until Finish it keeps `setup_mode` `minty` with an organisation recorded, and publishing is refused
before any Xero call ("Finish matching your accounts to Xero first (Settings, under Integration), then
publish."). Finish sets `entities.xero_publish_from` to that day (owner: only records from then on go
to Xero), so a report dated before it is refused ("This report is from before Xero was connected, so
it stays in Minty."). NULL - every company connected from the start - is no cut-off.

**Minty's books until Finish (2026-11-11).** `minty_on_its_own` is `setup_mode = 'minty'` alone: an
organisation recorded mid-matching no longer makes the company a Xero company here (it did, and
Petty Cash Settings drew empty Xero pickers while the company was wrongly marked connected).
Matching's Finish sets `setup_mode = 'xero'`.
An archived account is no account code to offer or tick (`xero_cards.account_codes` / its save):
one onboarding archived and re-added under the same code was listed twice.
