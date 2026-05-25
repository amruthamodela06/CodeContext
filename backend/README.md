# CodeContext backend

Python 3.12 + FastAPI + SQLAlchemy 2.0 (async) + Alembic. Managed with `uv`.

See the top-level [README](../README.md) and [docs/PRD.md](../docs/PRD.md) for project context, and [docs/decisions/](../docs/decisions/) for ADRs.

## Local quick-start

```bash
# from repo root
make db-up       # start Postgres + pgvector
make db-migrate  # run Alembic to head
make backend-dev # uvicorn with --reload
```

## Layout

```
app/
  __init__.py
  main.py     FastAPI app + /healthz
  config.py   pydantic-settings (reads .env at repo root)
  db.py       async engine + session dependency
  models.py   SQLAlchemy declarative models (Repo, File)
migrations/   Alembic
tests/        pytest
```
