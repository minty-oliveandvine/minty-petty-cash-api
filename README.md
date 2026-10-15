# minty-petty-cash-api

The Django home of Minty's Petty Cash API - the day's petty cash report that
[minty-petty-cash-web](https://github.com/minty-oliveandvine/minty-petty-cash-web) reads and
writes. It serves **the same paths and JSON** as Flask's `blueprints/petty_cash_api`
(`/api/petty-cash/v1/companies/{entity_id}/...`), so the web app moves here by changing
`PETTY_CASH_API_URL`. Flask (Minty) stays the token minter and Xero's only credential holder.

- Django 5.2 + django-ninja, Python 3.13 - the toolchain of minty-subscription-api.
- Port **8010** locally.
- A tenant of the schema Minty owns: every model is `managed = False`, no migrations, the schema
  chosen by `?schema=` on `DATABASE_URL`.

## Run it

```
uv venv .venv --python 3.13
.venv\Scripts\python -m ensurepip --upgrade
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env          # then set SECRET_KEY and DATABASE_URL
.venv\Scripts\python manage.py runserver 8010
```

`GET /healthz` answers without a token or a database.

## Test it

```
.venv\Scripts\python -m pytest            # SQLite, tables built from the models
```

Postgres mode builds the real schema from Minty's `docs/schema/01_schema_rebased.sql`:
set `MINTY_TEST_PG_URI` (a server URL), `MINTY_TEST_PG_DBNAME` (always a unique name),
`MINTY_REPO` (default `C:\Github\Minty`) and, if psql is not on PATH, `PG_BIN`.

`ruff check .` lints; CI runs both modes through the shared `.github` workflow
(`python-api.yml@v1`).

## Docs

- [docs/features/petty-cash-api.md](docs/features/petty-cash-api.md) - the door, the endpoints, what is ported.
