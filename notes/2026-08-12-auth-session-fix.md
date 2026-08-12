# 2026-08-12 — Auth / session persistence (#2906)

Commit `087e3fb`, pushed to origin/master.

## The reported symptom
Todd was logged out of the Android app roughly monthly, and was blocked at the
login screen with a forgotten admin password.

## Immediate unblock
Reset `admin` to `audiplex-fresh-start` through `audiplex.auth.hash_password`
(passlib bcrypt), verified end-to-end against the live server on port 8100.
Only the admin row was touched; `dj-agent` and `jimmy_bodolay` untouched.

## Root cause — the assigned hypothesis was wrong
The assignment suspected the JWT secret wasn't surviving restarts. It was, and
three hypotheses were disproved before finding the real cause:

1. **JWT secret rotation — NO.** `server/config.yaml` has held the same secret
   since Aug 8 14:57 (mtime unchanged). Only one config.yaml in the tree. Both
   `launch.bat` and `launch-hidden.vbs` `cd` to `server/` first, so cwd-relative
   config resolution is consistent. `_persist_jwt_secret()` works.
2. **Second/empty database — NO.** One live `audiplex.db` (33MB) plus three
   backups. The `Audiplex Server` boot Scheduled Task is *not* registered, so
   nothing launches uvicorn from a stray cwd.
3. **Multi-process DataStore corruption — NO.** No `android:process` in the
   manifest; single process, so DataStore's single-writer guarantee holds.

**Actual cause:** `token_expiry_hours = 720` (30 days) plus `ApiModule.kt`
deleting the stored token on *any* 401. The token expired, the server said
"Token expired", the client wiped the session silently, and the login screen
appeared for no visible reason. The monthly cadence is the 30-day expiry.

**Second bug found:** the 401-clear didn't exempt `/api/auth/login`, so one
mistyped password wiped an existing valid session.

## What shipped
Server (`auth.py`, `routers/auth_router.py`, new `manage.py`):
- Every 401 now logs its reason + path + client at WARNING. The silence is what
  let this hide for months.
- Sliding refresh: a token past halfway through its life returns renewed in an
  `X-Refresh-Token` header. Computed from remaining-vs-configured lifetime
  rather than an `iat` claim, so pre-existing tokens renew too instead of
  expiring out from under a client that never had a chance to refresh.
- `POST /api/auth/change-password` (self, requires current password);
  `POST /api/auth/users/{id}/reset-password` (admin-only via existing
  `get_admin_user`). 8-char minimum on both.
- `python -m audiplex.manage reset-password <user>` / `list-users`. Run from
  `server/` so the relative sqlite URL resolves to the same DB. Local shell
  access is the auth factor — there's no mail service for reset links.

Android (`ApiModule.kt`, `SettingsStore.kt`, new `AuthTokenStore.kt`,
`LoginViewModel.kt`, `LoginScreen.kt`):
- 401 from login/register no longer clears the token.
- `X-Refresh-Token` swapped in silently.
- A real 401 records *why*, so the login screen says "Your session expired"
  and prefills the last username.
- Token + host mirrored in `@Volatile` fields instead of two `runBlocking`
  DataStore reads per request.
- `AuthTokenStore` interface extracted purely so the interceptor is testable
  off-device.

## Verification
- Server: 260 passed (19 new in `tests/test_api_auth.py`, which deliberately
  does *not* override `get_current_user` — the shared `client` fixture bypasses
  auth, which would have made these tests vacuous).
- Android: 7 new JVM tests, `./gradlew test assembleDebug` clean (versionCode
  31 → 32). The JVM unit-test source set and its deps didn't exist before.
- End-to-end against a throwaway instance on port 8123: 18/18 checks including
  fresh-token-not-renewed, half-spent-token-renewed-and-renewal-works,
  expired-not-renewed, and all the new endpoint guards. Todd's server on 8100
  was left running throughout.

## Gotchas worth remembering
- `auth_router.py` binds `get_settings` at import time while `auth.py` imports
  it inside the function. Tests must patch **both** or the router signs tokens
  with the real secret while `auth.py` verifies with the fake one.
- `./gradlew` (the sh wrapper) fails under Git Bash with
  `Could not find or load main class "-Xmx64m"`. Use
  `cmd //c "Q:\Development\audiplex\android\gradlew.bat ..."`.
- `orch_safe_commit.py --allow-unmarked` takes **one path per flag**, repeated.
- Avoid em dashes in server log strings and CLI output — the Windows console is
  cp1252 and renders them as garbage.

## Not done / deferred
- **Deployment:** Todd's server on 8100 is still running the pre-fix code. It
  needs a restart (`launch.bat`) to activate. Left alone deliberately — it
  lives in a console window Todd opened.
- APK built but not installed to the phone.
- Port move off 8100 and the Solace→Athena host move remain separately deferred.
