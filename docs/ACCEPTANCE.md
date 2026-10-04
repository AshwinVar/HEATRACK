# Acceptance report — 4 October 2026

Environment used for verification: Linux cloud sandbox with Python 3.11, PostgreSQL 16, Docker,
Flutter 3.47.6 / Dart 3.13.5, headless Chromium. **Not available:** Android SDK (dl.google.com
and Google Maven blocked by the sandbox egress policy), Android phone, Fitbit, macOS/Xcode,
Supabase project, Firebase project/APNs key, public server. Statuses below are strict: anything
not executed end‑to‑end in this environment is **UNVERIFIED**.

## Acceptance criteria

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | Two independently authenticated users can pair with wearer confirmation | **PASSED (backend, test‑signed JWTs)** · Supabase‑issued tokens **UNVERIFIED** | `test_pairing_requires_wearer_confirmation`, `test_invitation_single_use_and_expiry`, `test_owner_cannot_accept_own_invitation`, `test_category_scoped_grant` (separate ES256‑signed identities, full signature/iss/aud/exp/sub verification). Same flow run over HTTP by `scripts/seed_demo.py` against uvicorn and against the Docker stack. No Supabase project was available. |
| 2 | A real authorized Health Connect reading reaches the backend when hardware permits | **UNVERIFIED** | No phone/SDK. Kotlin collector implemented against the published `connect-client` API signatures (checked against androidx `api/current.txt`), passes ktlint parsing, but was **not compiled or run**. The ingest endpoint it targets is fully tested. |
| 3 | Latest measurements show correct source and age | **PASSED** | `test_latest_shows_source_and_times` (origin, device, measured vs received time, age); Flutter widget tests (`fresh value is shown as current with source`); browser E2E of the web build (caregiver dashboard screenshot: source, “Measured 3 min ago”, IST + BST labels). |
| 4 | Synthetic anomaly produces one persisted alert and notification when push is configured | **PASSED with FAKE transport** · real FCM/APNs **UNVERIFIED** | `test_persistent_episode_produces_one_alert_and_one_notification` (one alert, one outbox job, generic text, no vitals), `test_simulation_profile_synthetic_demo`; Docker stack: worker logged `alerts_created=2 dispatch={'accepted': 2}` then `alerts_created=0` on later cycles (no duplicates). No Firebase credentials. |
| 5 | Offline retry creates no duplicates | **PASSED (server)** · on‑device queue **UNVERIFIED** | `test_repeated_batch_is_idempotent`, `test_duplicate_source_records_under_new_batch_id`, `test_concurrent_retries_do_not_duplicate` (8 concurrent uploads incl. same/different batch ids → 4 rows), `test_updated_record_replaces_samples_atomically`. The Kotlin encrypted queue / checkpoint logic was not executed. |
| 6 | Stale data never looks current | **PASSED** | `test_heartbeat_and_empty_upload_never_refresh_metric_freshness`, `test_stale_sleep_alongside_fresh_hr`, `test_waiting_for_data_before_any_measurement`; widget test `stale value is labelled NOT CURRENT and struck through`; browser screenshot shows stale metrics struck through and labelled. |
| 7 | Revocation stops reads and future pushes | **PASSED** | `test_revocation_effective_on_next_request`, `test_revoked_recipient_suppressed_via_cancellation`, `test_revoked_recipient_suppressed_at_send_time` (grant re‑checked under `FOR SHARE` lock right before send). |
| 8 | Android collector works without an open Flutter screen | **UNVERIFIED** | Implemented as a Kotlin `CoroutineWorker` (`SyncWorker.kt`, 15‑min periodic, network constraint) that never touches Flutter; requires `READ_HEALTH_DATA_IN_BACKGROUND`, otherwise the app shows FOREGROUND ONLY. Not run on a device. |

## Checks run

| Check | Result |
|---|---|
| `pytest` (backend, real PostgreSQL 16, Alembic‑migrated schema) | **57 passed** |
| `ruff check` + `ruff format --check` | clean |
| `mypy app` | no issues (23 files) |
| `alembic upgrade head` + `alembic check` | migration applies; models and migration in sync |
| `flutter analyze` | no issues |
| `flutter test` (state/rendering tests) | **8 passed** |
| `flutter build web --release --no-web-resources-cdn` | built |
| Playwright (Chromium, en‑GB/Europe‑London) against web build + live API | sign‑in → profile list → dashboard rendered (SIMULATED banner, per‑metric states, dual time zones) |
| `docker build` + `docker compose up` (dev stack: db, migrate, api, worker) | built and ran; `/readyz` ready; worker created alerts and dispatched to fake transport* |
| ktlint 1.8.0 on Kotlin sources | parses; formatted; 0 findings |
| `flutter build apk` / Gradle | **not run** (no Android SDK) |
| `flutter build ios` | **not run** (no macOS) |

\*The sandbox's TLS‑intercepting proxy required adding its CA in a throwaway copy of the
Dockerfile for the verification build only; the committed Dockerfile is unchanged and standard.

Backend tests cover every scenario the brief lists: cross‑user access, expired JWT, caregiver
cannot ingest, consent/grant revocation, repeated batch and duplicate source records,
updated/deleted records, rejected units/NaN/Infinity/future times, measurement vs upload
freshness, stale sleep vs fresh HR, historical backfill → no live alert, insufficient baseline,
persistent episode dedup, ongoing condition after acknowledgement (with hysteresis), failed push
retry, invalid token, revoked recipient suppression, plus RLS enabled and audit log free of
health values.

## Bugs found and fixed during verification

* Push‑device registration autoflushed a row before its token was set (500) — fixed.
* A NaN/Infinity ingest value made the 422 error response itself non‑serialisable (500) and
  echoed submitted health values — replaced with a value‑free validation error handler.
* App theme used `textTheme.apply(fontSizeFactor:)`, which asserts at runtime — replaced by a
  clamped minimum text scale that still honours larger system font settings.
* Sleep stage metadata could exceed the server's metadata limit and reject whole batches —
  limits aligned (collector caps stages, server limit raised).

## Known limitations

* Kotlin collector uncompiled (see #2/#8). Library versions in `android/app/build.gradle.kts`
  (`connect-client 1.1.0`, `work-runtime-ktx 2.10.1`) should be confirmed in Android Studio.
* HRV disabled until mapping verified; SpO2/ECG/falls/BP/glucose/battery shown unavailable.
* Health thresholds ship disabled; no activity context, so wording is neutral ("outside
  configured range"), never "resting tachycardia".
* FCM acceptance ≠ phone receipt ≠ human acknowledgement; alerts remain in the app if push
  fails. No SMS/call escalation. No rate limiting on the API yet.
* Background sync on Android is inexact (Doze/OEM); no new samples ≠ watch not worn.
* Deleting app data does not delete the Supabase auth identity (needs the service‑role key,
  which the API deliberately does not hold).
* RLS + privilege revocation verified on plain PostgreSQL; the `anon`/`authenticated` revoke
  branch only runs on Supabase and is unverified there (runbook has the check query).

## Shortest next steps to live use

1. On a machine with Android Studio: `cd mobile && flutter build apk --debug` → fix any compile
   errors in the Kotlin collector (expected to be small) → install on the mother's phone.
2. Enable Fitbit → Health Connect sync; run **Source diagnostics**; confirm HR records from the
   Fitbit origin, note cadence/export lag (this is the first‑hour integration spike).
3. Create Supabase + Firebase projects (RUNBOOK §1); deploy with
   `docker compose -f infra/docker-compose.prod.yml` behind a domain (RUNBOOK §2).
4. Build the release APK with Supabase defines; pair caregiver; send a test notification.
5. Observe background sync for 24–72 h; tune freshness budgets; only then configure any
   health threshold, reviewed with her clinician.
