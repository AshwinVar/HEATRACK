# Deploying the backend on Railway (project `heatrack`)

Two Railway services run the **same image** from this repo's `backend/Dockerfile`:

| Service | `FP_ROLE` | What it does | Public domain |
|---|---|---|---|
| `heatrack-api` | `api` (default) | runs `alembic upgrade head` (advisory‑locked), then the API on `$PORT` | yes |
| `heatrack-worker` | `worker` | rules every minute, freshness, notification outbox | **no** |

Database and sign‑in come from the Supabase project `heatrack`; notifications from the Firebase
project `heatrack`. No Railway config file is needed (Railway's config‑as‑code is deprecated);
everything is in the image plus the settings below.

## 1. Create the services

Railway → project **heatrack** → **New → GitHub Repo → AshwinVar/HEATRACK**. Then for that service:

* **Settings → Source**: branch `claude/familypulse-mvp-implementation-om10fi` (or `main` after
  merging), **Root Directory** `backend`. Railway detects the Dockerfile.
* **Settings → Networking → Generate Domain** (API service only). Note the URL, e.g.
  `https://heatrack-api-production.up.railway.app`.
* **Settings → Deploy → Healthcheck Path**: `/readyz` (API service only).
* Rename it `heatrack-api`.

Add a second service from the same repo with the same branch and Root Directory, rename it
`heatrack-worker`, set `FP_ROLE=worker`, **no** domain, **no** healthcheck. Keep each service at
**1 replica** (the worker is safe with more, but nothing needs it).

## 2. Variables

Use a **Shared Variable** set (Project Settings → Shared Variables) and reference it from both
services, so both get identical values:

```
FP_APP_ENV=production
FP_DATABASE_URL=postgresql+psycopg://postgres.<project-ref>:<db-password>@<session-pooler-host>:5432/postgres?sslmode=require
FP_AUTH_JWKS_URL=https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json
FP_AUTH_ISSUER=https://<project-ref>.supabase.co/auth/v1
FP_AUTH_AUDIENCE=authenticated
FP_PUSH_TRANSPORT=fcm
FP_FIREBASE_CREDENTIALS_JSON=<paste the whole service-account JSON from Firebase>
FP_CORS_ORIGINS=["https://<web-fallback-domain-if-any>"]
```

Per service: `heatrack-worker` → `FP_ROLE=worker`. (`heatrack-api` needs no `FP_ROLE`.)

Notes:

* Use Supabase's **Session pooler** (port 5432) or direct connection string — not the
  Transaction pooler (6543): migrations take a session advisory lock.
* `FP_FIREBASE_CREDENTIALS_JSON` is a private key. Railway variables are encrypted; never
  commit it or paste it anywhere else. Firebase → Project settings → Service accounts →
  Generate new private key.
* If you don't use the web fallback, set `FP_CORS_ORIGINS=[]`.
* The API refuses to start in production if dev token minting or the fake push transport is
  on, if no JWT key is configured, or if CORS is a wildcard — check deploy logs if it crashes.

## 3. Verify

```bash
curl -fsS https://<api-domain>/healthz   # {"status":"ok"}
curl -fsS https://<api-domain>/readyz    # {"status":"ready"}  (database reachable)
curl -s -o /dev/null -w "%{http_code}\n" https://<api-domain>/v1/me   # 401 (auth enforced)
```

Worker logs should show `cycle alerts_created=… dispatch=…` once a minute.
Then run the RLS check from `docs/RUNBOOK.md` in the Supabase SQL editor.

## 4. Point the app at it

GitHub repo → Settings → Secrets and variables → Actions: `HEATRACK_API_BASE_URL` =
`https://<api-domain>` (plus the Supabase/Firebase secrets from
`docs/SETUP_HEATRACK_PROJECTS.md`), then Actions → **Release APK (heatrack)** → Run workflow.

## Verified locally (not on Railway itself)

The image was built and run in production mode the way Railway runs it: `PORT` honoured, two
API containers started simultaneously (advisory lock let one migrate), `/readyz` ready, docs
disabled, unauthenticated requests 401, and `FP_ROLE=worker` running the worker loop. Railway
itself, Supabase and Firebase were not reachable from the build sandbox.
