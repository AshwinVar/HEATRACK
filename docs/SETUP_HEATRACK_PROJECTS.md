# Connecting the `heatrack` Supabase and Firebase projects

Nothing below needs to be pasted into a chat. Public values go into GitHub secrets / the
server env file; private keys go **only** onto the server (and GitHub secrets where noted).

## Supabase project `heatrack`

1. **Authentication → Sign In / Providers → Email**: enabled. Keep "Confirm email" on.
2. **Authentication → URL Configuration**: Site URL can stay default (the app uses email+password).
3. **Project Settings → API** (or "API Keys"), copy:
   - Project URL `https://<project-ref>.supabase.co` → GitHub secret `HEATRACK_SUPABASE_URL`
   - anon / publishable key (public) → GitHub secret `HEATRACK_SUPABASE_ANON_KEY`
4. **Project Settings → JWT / Signing keys**: make sure asymmetric signing keys (ES256/RS256) are
   in use. Server env:
   - `FP_AUTH_JWKS_URL=https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json`
   - `FP_AUTH_ISSUER=https://<project-ref>.supabase.co/auth/v1`
   (Legacy HS256-only project: set `FP_AUTH_JWT_SECRET` instead — server only, never in the app.)
5. **Connect → Session pooler** connection string → server env `FP_DATABASE_URL`
   (prefix `postgresql+psycopg://`, add `?sslmode=require`). Server only.
6. After the first deploy, run the RLS check query in `docs/RUNBOOK.md`.

## Firebase project `heatrack`

1. **Project settings → General → Add app → Android**, package name
   **`com.familypulse.familypulse`** (must match exactly; tell me if you want a different id and I
   will rename the Android package). Download `google-services.json`, then
   `base64 -w0 google-services.json` → GitHub secret `HEATRACK_GOOGLE_SERVICES_JSON_B64`.
2. **Add app → iOS** (for the caregiver iPhone) with your bundle id; download
   `GoogleService-Info.plist` (used on the Mac when building iOS).
3. **Project settings → Cloud Messaging → Apple app configuration**: upload an APNs
   authentication key (.p8) from your Apple Developer account.
4. **Project settings → Service accounts → Generate new private key** → paste its contents into
   the Railway variable `FP_FIREBASE_CREDENTIALS_JSON` (or, on a VM, save as
   `infra/secrets/firebase-service-account.json`). Private credential: do not commit it or send
   it in chat.

## Server (Railway)

Follow `docs/DEPLOY_RAILWAY.md` (two services from `backend/`: API + worker). The Firebase
service-account JSON goes into the Railway variable `FP_FIREBASE_CREDENTIALS_JSON`. Then set
GitHub secret `HEATRACK_API_BASE_URL` to the Railway API domain.

## Build the phone APK

GitHub → **Actions → "Release APK (heatrack)" → Run workflow**. It checks the secrets, verifies
`google-services.json` belongs to package `com.familypulse.familypulse`, builds a release APK
with Supabase sign-in, and attaches it as artifact `heatrack-release-apk`.
