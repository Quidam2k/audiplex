# Audiplex

A self-hosted audiobook and music server with a native Android client. Built for
people who rip their Audible library with [Libation](https://github.com/rmcrackan/Libation)
and want chapter-aware playback, position sync, and reliable background audio —
without running a full Plex stack.

## Why

Plex's Android client handles long audio files poorly. Audiplex is purpose-built
for audiobooks: cue/embedded chapter navigation, server-side position tracking,
30-second skips from the lock screen, and direct HTTP range streaming of M4B
files with no transcoding. It also serves a regular music library (albums,
artists, genres, playlists, folder browsing).

## Architecture

- **Server** (`server/`): Python 3.11+ / FastAPI / SQLAlchemy / SQLite. Scans
  library folders, extracts metadata with mutagen, parses Libation `.cue` files
  for chapters (embedded M4B chapters as fallback), streams audio via HTTP range
  requests. JWT bearer auth with multi-user support; the first registered user
  becomes admin.
- **Android client** (`android/`): Kotlin / Jetpack Compose / Media3 (ExoPlayer)
  / Retrofit / Room. Background playback via MediaSession foreground service,
  offline downloads, position sync, in-app update flow that pulls new APKs from
  the server.

## Server setup

```bash
cd server
pip install -r requirements.txt
# create config.yaml — see config.py for available keys:
#   library_roots:
#     - path: D:/Audiobooks
#       category: audiobook_clean   # Libation layout: one .m4b + .cue per folder
#     - path: D:/MoreBooks
#       category: audiobook_misc    # folders of loose mp3/m4a tracks
#     - path: D:/Music
#       category: music
uvicorn audiplex.main:app --host 0.0.0.0 --port 8000
```

Register the first user via the Android app (or `POST /api/auth/register`) —
that account is the admin. Trigger a library scan with
`POST /api/library/scan`.

Tests: `pytest` from `server/`.

## Android client

```bash
cd android
./gradlew assembleDebug
```

Install the APK, point Settings → Server URL at your server, and log in.
Subsequent updates can be installed from the app itself (Settings → Check for
Update), served by the server from the gradle output directory.

For off-LAN access, put the server and phone on a [Tailscale](https://tailscale.com)
tailnet and use the tailnet hostname as the server URL — no port forwarding
required.
