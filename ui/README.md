# Dashboard

The maintainer dashboard for auto-maintainer: React 18, TypeScript, Vite, and TanStack Query. It talks to the JSON API in `src/auto_maintainer/web/` and is served by `am serve` from `src/auto_maintainer/web/static/`, where `npm run build` writes it.

## Develop against the demo backend

The demo backend runs the whole service on fakes with sample data; nothing leaves the host.

```bash
uv run am serve --demo --port 7788      # in one terminal, from the repository root
cd ui && npm ci && npm run dev          # in another; Vite serves on :5173 and proxies /api, /auth, /healthz to :7788
```

Sign in by opening http://127.0.0.1:5173/auth/dev?login=maria (the dev login exists only in dev and demo mode). Set `AM_BACKEND` to proxy to another backend, for example `AM_BACKEND=http://127.0.0.1:7777 npm run dev`.

## Commands

```bash
npm run typecheck   # tsc --noEmit
npm run build       # typecheck, then build into ../src/auto_maintainer/web/static
```

## How it fits the API

- Every non-GET `/api` request sends `X-AM-CSRF` with the value of the `am_csrf` cookie (`src/api.ts`).
- A 401 from `/api/me` shows the sign-in page: the GitHub OAuth web flow (`/auth/login`) or the device flow (`/auth/device/start`, then `/auth/device/poll`).
- `/api/events` is a server-sent event stream; `src/events.ts` maps event kinds to the queries they invalidate, so pages refresh without polling.
- The language toggle (English or Chinese) covers navigation and main labels and is kept in `localStorage` when the browser allows it.

## Pages

| Path | Page |
|---|---|
| `/` | Review queue with internal tiers and scores, filtered by repository, tier, review state, and "mine" |
| `/pr/:owner/:repo/:number` | PR detail: review state, evaluation and override, claim, review request, merge arming and checklist, hygiene, timeline, reviews, CI |
| `/inbox` | Pending proposals grouped by repository, showing exactly what will be posted; edit before approving, reject with a reason, bulk-approve labels |
| `/memory` | Open memory changes with their files, approvals, approve and reject; a browser over the memory repo |
| `/repos`, `/repos/:owner/:repo` | Repositories, onboarding, the approved lock and layers, the pending candidate diff with approval, the memory-layer editor, pause and resume, agreement scores |
| `/ci` | Failed-job diagnoses and counts per job and category |
| `/agents` | Runtime health and recent agent runs |
| `/audit` | The audit log |
| `/chat` | Threads with the service; proposals it creates appear inline with approve and reject |
