# FamilyPulse production runbook

These steps are written for a small always‑on Docker host (any Linux VM with a public IP).
They were **not executed** from the build sandbox (no credentials, domain or server); the
compose stack itself was built and run locally in development mode.

## 1. Accounts and secrets

| Item | Where | Goes to |
|---|---|---|
| Supabase project | supabase.com → New project | |
| Project URL + anon key (public) | Project Settings → API | app `--dart-define` |
| JWKS URL / issuer | `https://<ref>.supabase.co/auth/v1/.well-known/jwks.json`, issuer `https://<ref>.supabase.co/auth/v1` | `infra/.env.production` |
| DB connection string | Project Settings → Database → Connection string (session pooler, `sslmode=require`) | `infra/.env.production` |
| Email auth | Authentication → Providers → Email (keep "Confirm email" on) | |
| Firebase project | console.firebase.google.com | |
| Android app (`com.familypulse.familypulse`) | → `google-services.json` | `mobile/android/app/` (not in git) |
| iOS app (your bundle id) + APNs .p8 key | Project settings → Cloud Messaging | Xcode / Firebase |
| Service account key (Admin SDK) | Project settings → Service accounts → Generate key | `infra/secrets/firebase-service-account.json` (chmod 600, not in git) |

Never commit `.env.production`, service‑account JSON, `google-services.json`,
`GoogleService-Info.plist` or `.p8` keys (`.gitignore` covers them).

## 2. Server

```bash
# on the VM (Docker + compose plugin installed; DNS A records for both domains → this VM)
git clone <repo> familypulse && cd familypulse
cp infra/.env.production.example infra/.env.production && chmod 600 infra/.env.production
# edit: FP_DATABASE_URL, FP_AUTH_JWKS_URL, FP_AUTH_ISSUER, FP_CORS_ORIGINS, and add
#   API_DOMAIN=api.example.com
#   WEB_DOMAIN=app.example.com
mkdir -p infra/secrets && cp /path/to/firebase-service-account.json infra/secrets/
chmod 600 infra/secrets/firebase-service-account.json

# optional web fallback, built on any machine with Flutter, copied to mobile/build/web
flutter build web --release --no-web-resources-cdn \
  --dart-define=API_BASE_URL=https://api.example.com --dart-define=AUTH_MODE=supabase \
  --dart-define=SUPABASE_URL=https://<ref>.supabase.co --dart-define=SUPABASE_ANON_KEY=<anon>

docker compose --env-file infra/.env.production -f infra/docker-compose.prod.yml up -d --build
```

Verify:

```bash
curl -fsS https://api.example.com/healthz     # {"status":"ok"}
curl -fsS https://api.example.com/readyz      # {"status":"ready"} (DB reachable)
docker compose -f infra/docker-compose.prod.yml logs worker | tail   # "cycle alerts_created=..."
```

The API refuses to start in production if dev token minting or the fake push transport is
enabled, if no JWT verification key is configured, or if CORS is a wildcard. OpenAPI docs are
disabled in production. Only Caddy publishes ports (80/443, automatic HTTPS); API, worker and
database are not exposed.

### Database exposure

Migrations enable **row‑level security with no policies** on every table and revoke table
privileges from Supabase's `anon` and `authenticated` roles when those roles exist, so the
public Supabase Data API cannot read or write FamilyPulse tables; all access goes through the
API, which connects as the table owner. After the first migration, confirm in the Supabase SQL
editor:

```sql
select relname, relrowsecurity from pg_class
where relnamespace = 'public'::regnamespace and relkind = 'r';
select has_table_privilege('anon', 'public.measurements', 'select');   -- expect false
```

## 3. First live run (acceptance on hardware)

1. Install the release APK on the wearer's phone (README → Android). Sign up/in as the wearer.
2. Create the profile, connect watch data, open **Source diagnostics**: record what appears
   (origins, HR cadence, export lag). This is the integration spike evidence.
3. Caregiver installs the app (iOS needs Xcode build; Android APK works), signs in with a
   **different** account, enters the invitation code; wearer confirms; caregiver sees data.
4. Settings → *Register for notifications* → *Send test notification* on the caregiver phone.
   "accepted" means FCM accepted it; check the phone actually shows it.
5. Use the demo (SIMULATED) profile with the synthetic HR rule to see one alert + one push.
6. Leave the wearer phone untouched for 24–72 h and review *Recent sync runs* in Source
   diagnostics before trusting unattended operation; tune `FP_FRESHNESS_*` budgets and the
   connectivity rule to the observed cadence.

## 4. Operations

* **Logs** never contain request bodies, health values or tokens; the worker logs counts only.
* **Backups**: Supabase daily backups (or PITR on paid plans). Health data is personal: limit
  dashboard access to the owner.
* **Upgrades**: `git pull && docker compose ... up -d --build` (the `migrate` service runs
  `alembic upgrade head` before api/worker start).
* **Revoke a caregiver**: wearer app → Who can see my readings → Stop sharing (immediate).
* **Delete data**: wearer app → Delete all my shared data, or Settings → Delete my account data.
  The Supabase auth identity is then removed in Supabase → Authentication → Users.
* **Push failures**: alert detail shows queued / accepted‑by‑provider / discarded counts. Invalid
  tokens are disabled automatically; retries use exponential backoff with jitter (max 8).
* **Rotating secrets**: rotate the Supabase JWT signing key in Supabase (JWKS is re‑fetched
  hourly), the Firebase service account in Firebase (replace the file, restart api/worker).
