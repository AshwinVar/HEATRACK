# FamilyPulse

A personal family wellness‑sharing prototype. A wearer (Android phone + Fitbit) shares selected
readings with family caregivers (iPhone/Android), who see per‑metric freshness and receive
explainable anomaly and stale‑data alerts.

> **Not an emergency service and not a medical device.** Alerts can be late or missed. Health
> thresholds are off until configured and reviewed for the wearer. Nothing here diagnoses
> anything (no AFib/heart‑attack inference from heart‑rate samples).

Status and evidence for every acceptance criterion: **[docs/ACCEPTANCE.md](docs/ACCEPTANCE.md)**.
Deployment and operations: **[docs/RUNBOOK.md](docs/RUNBOOK.md)**. Connecting the `heatrack` Supabase/Firebase projects: **[docs/SETUP_HEATRACK_PROJECTS.md](docs/SETUP_HEATRACK_PROJECTS.md)**.

## Integration decision

| Path | Status |
|---|---|
| Legacy Fitbit Web API | **Not used.** Support ended 30 Sep 2026; shutdown 30 Oct 2026 (confirmed by multiple secondary sources; developers.google.com was blocked from the build sandbox). |
| Google Health API | **Not used / not assumed.** The brief states it is not onboarding new projects; this could not be re‑verified from Google's own page in the sandbox. A future cloud adapter can be added behind the same ingest contract. |
| **Android Health Connect** | **Used.** Fitbit app → Health Connect → FamilyPulse Kotlin collector on the wearer's phone. |

Health Connect support for a record type does **not** prove the Fitbit app writes it on a given
phone/account. The app's **Source diagnostics** screen (the "integration spike") reads what is
actually present: record counts per app (data origin), heart‑rate sample cadence and export lag.
SpO2, ECG/AFib, falls, BP, glucose and watch battery are shown as unavailable. HRV is
implemented server‑side but **disabled in the collector** until its mapping is verified.

## Architecture

```
Fitbit watch ─BT→ Fitbit app ─→ Health Connect (wearer's Android phone)
                                   │  read-only, permission-gated
                                   ▼
            FamilyPulse Android app: Kotlin collector (WorkManager ≥15 min + "Sync now")
            encrypted on-device queue · checkpoint advanced only after server ack
                                   │ HTTPS, Supabase JWT + registered installation
                                   ▼
   FastAPI (ingest, grants, dashboard)  ── PostgreSQL (Supabase-managed; RLS deny-all)
   Python worker (rules every minute, freshness, outbox with row locks)
                                   │ Firebase Admin SDK
                                   ▼
                     FCM / APNs ─→ caregiver app (Flutter: iOS / Android; web fallback)
```

* One Flutter app, separate accounts; wearer and caregiver views.
* Supabase Auth issues JWTs; the API verifies signature, issuer, audience, expiry and subject
  (JWKS or legacy HS256). Every profile request needs ownership or an **active,
  category‑scoped grant** re‑read on each request. Client roles/profile IDs are untrusted.
* Pairing: wearer creates a short‑lived single‑use code → caregiver accepts → **wearer confirms
  the caregiver's identity** → grant active. Revocation is effective on the next request and
  cancels queued pushes; the worker re‑checks the grant (row lock) immediately before sending.
* No Redis/Celery/Kafka/Kubernetes/ML.

### Notable design choices

* **Single token refresher on Android.** Supabase rotates refresh tokens and revokes a session
  when a rotated token is reused. The Flutter UI and the background worker therefore share one
  Keystore‑encrypted session in Kotlin (`SessionStore.kt`) behind a process‑wide mutex; Flutter
  asks Kotlin for access tokens over the MethodChannel.
* **Two layers of ingest idempotency:** batch UUID (replay returns the stored result) and source
  record identity + version (resending the same Health Connect records under a new batch UUID
  writes nothing). Newer versions replace samples atomically; deletions remove them.
* **Freshness comes only from measurement timestamps.** Heartbeats and empty uploads never make
  a metric look current. States: `unsupported`, `permission_denied`, `waiting_for_data`,
  `fresh`, `stale`. Stale values are shown struck through and labelled NOT CURRENT.
* **Backfill can never alert.** Reconciliation reads older than 2 h are sent as `backfill`;
  threshold rules use only fresh, non‑backfill samples in a window relative to *now*.
* **Simulation is a separate profile type** (`data_mode=simulation`, origin
  `simulation:familypulse`). The server rejects simulated data in live profiles and vice versa;
  every simulated screen, alert and push is labelled.
* Day summaries are computed on read in the wearer's timezone (Asia/Kolkata), so corrections flow
  through automatically. Phone and Fitbit steps are reported per source and never summed.

## Repository

```
backend/            FastAPI app, worker, Alembic migrations, tests (pytest on real Postgres)
  app/routes/       accounts & sharing, collector & ingest, dashboard/alerts/rules, push, ops
  app/services/     ingest, freshness, rules (episodes), notify (outbox), push transports
  scripts/seed_demo.py   labelled SIMULATION fixtures via the HTTP API (dev only)
mobile/             Flutter app (Android, iOS, web fallback)
  lib/              screens, API client, auth, push, collector bridge, simulator
  android/app/src/main/kotlin/.../collector/   Health Connect collector (Kotlin)
  test/             widget/state tests
infra/              docker-compose (dev), docker-compose.prod (Caddy HTTPS), env templates
docs/               RUNBOOK.md, ACCEPTANCE.md
```

## Local development

Prerequisites: Python 3.11 + [uv](https://docs.astral.sh/uv/), PostgreSQL 16 (or Docker),
Flutter 3.47+ (stable). For Android: Android Studio with SDK 36 + JDK 17.

### Backend with Docker Compose

```bash
cd infra
cp .env.example .env            # then set POSTGRES_PASSWORD and FP_DEV_JWT_SECRET
docker compose up -d --build    # db + migrate + api (127.0.0.1:8000) + worker
docker compose exec api python -m scripts.seed_demo   # optional SIMULATED demo data
docker compose logs -f worker
```

Development mode mints local tokens (`/v1/dev/token`, loopback/Docker bridge only) and uses a
**FAKE push transport** — both are labelled in the app's Settings → diagnostics. Neither is
available when `FP_APP_ENV=production` (the API refuses to start).

### Backend without Docker

```bash
cd backend
uv sync
createdb familypulse && createdb familypulse_test
export FP_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:5432/familypulse
uv run alembic upgrade head
FP_APP_ENV=development FP_DEV_TOKEN_MINT_ENABLED=true FP_DEV_JWT_SECRET=$(openssl rand -hex 24) \
  FP_PUSH_TRANSPORT=fake uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
# second shell (same env): uv run python -m app.worker
```

### Checks

```bash
cd backend
uv run ruff check app tests alembic && uv run ruff format --check app tests alembic
uv run mypy app
FP_TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:5432/familypulse_test uv run pytest

cd ../mobile
flutter analyze && flutter test
```

## Android: build and install (wearer phone)

1. Firebase (optional for the wearer; required for caregiver push): put `google-services.json`
   in `mobile/android/app/` (the Gradle plugin is applied only if the file exists).
2. Build:
   ```bash
   cd mobile
   # Local dev against an emulator (10.0.2.2 = host loopback; cleartext allowed in debug only):
   flutter build apk --debug --dart-define=API_BASE_URL=http://10.0.2.2:8000 --dart-define=AUTH_MODE=dev
   # Real phone against the deployed HTTPS API:
   flutter build apk --release \
     --dart-define=API_BASE_URL=https://api.example.com \
     --dart-define=AUTH_MODE=supabase \
     --dart-define=SUPABASE_URL=https://<project-ref>.supabase.co \
     --dart-define=SUPABASE_ANON_KEY=<anon-public-key>
   ```
3. Install: enable Developer options + USB debugging on the phone, then
   `adb install -r build/app/outputs/flutter-apk/app-release.apk`
   (or copy the APK and allow "Install unknown apps").
4. On the phone: make sure **Health Connect** is installed/updated (built in on Android 14+), and
   in the **Fitbit app** enable syncing to Health Connect. Open the Fitbit app so the watch syncs.
5. In FamilyPulse: sign in → *Set up sharing from this phone* → consent → *Connect watch data*
   (grants heart rate, resting HR, steps, sleep, and background read if offered).
6. Open **Source diagnostics** and confirm records from the Fitbit data origin
   (expected `com.fitbit.FitbitMobile`), sample cadence and export lag. If nothing appears, the
   integration is not working on this phone — use the demo (SIMULATED) profile instead.
7. If *background read* is denied/unavailable the app shows **FOREGROUND ONLY**: data uploads
   only when the wearer opens the app and taps *Sync now*. Even when granted, WorkManager runs
   are inexact (Doze, battery saver and OEM restrictions can delay them by hours). Exempting the
   app from battery optimisation on the wearer's phone helps.

## iOS (caregiver) prerequisites

Native iOS was **not built** here (no macOS). You need: a Mac with Xcode 16+ and CocoaPods; an
Apple Developer Program membership (push requires it); in Firebase, an iOS app with your bundle
ID and an **APNs authentication key (.p8)** uploaded; `GoogleService-Info.plist` added to
`mobile/ios/Runner` via Xcode; in Xcode *Signing & Capabilities*: your team, **Push
Notifications**, and **Background Modes → Remote notifications** (`Info.plist` already declares
`remote-notification`; see `Runner.entitlements.example`). Then
`flutter build ios --release --dart-define=...` (same defines as Android) and run on a real
iPhone. The **web fallback** (`flutter build web --release --no-web-resources-cdn ...`) lets a
caregiver view dashboards and alerts in a browser but has **no push** and does not validate
native iOS notifications.

## Remaining blockers (summary)

* The Kotlin collector compiles in GitHub Actions (debug APK artifact on every push) but has
  **not been run on a phone**; no real Health Connect reading has reached the backend yet.
* No Supabase project, Firebase project or APNs key were available → real sign‑in and real
  push delivery are unverified (fake transport + test‑signed JWTs were used).
* No public server/domain → nothing is deployed; `localhost` is not reachable from India/UK.

Shortest path to live use is in [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md#shortest-next-steps-to-live-use).
