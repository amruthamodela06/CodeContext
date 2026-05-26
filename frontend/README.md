# CodeContext frontend

Next.js 16 (App Router) + TypeScript strict + Tailwind 4. Package manager: `pnpm` (per [ADR 0006](../docs/decisions/0006-frontend-pnpm.md)).

See the top-level [README](../README.md) for the full slice-1 dev workflow.

## Local dev

```bash
pnpm install     # first time
pnpm dev         # http://localhost:3000
```

The backend's CORS allows `http://localhost:3000` in dev (see [.env.example](../.env.example) → `FRONTEND_ORIGIN`).

## Layout

```
app/
  page.tsx              server shell
  layout.tsx            root layout + metadata + Geist fonts
  globals.css           Tailwind 4 import + a few base styles
  _components/
    IngestForm.tsx      'use client' — form + result table
lib/
  api.ts                types + fetch helper
```

## Env

`NEXT_PUBLIC_API_URL` overrides the backend URL. Defaults to `http://localhost:8000`. Create `frontend/.env.local` to change.
