# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Audiplex** is a self-hosted audiobook server with an Android client. It replaces Plex for audio file playback — specifically Audible audiobook rips (M4B/M4A) managed by Libation. The server runs on Windows and serves content to a native Android app.

### Why This Exists

Plex's Android client handles audio files poorly. Audiplex is purpose-built for audiobooks: chapter navigation, position tracking, and reliable background playback.

## Architecture

### Server (`server/`)
- **Python 3.12+ with FastAPI** — REST API serving audiobook metadata and streaming audio
- **SQLite** via SQLAlchemy — library database, user progress, bookmarks
- **mutagen** — M4B/M4A metadata extraction (title, author, narrator, cover art, duration)
- Cue file parsing for chapter information (Libation exports `.cue` + `.m4b` per book)
- Falls back to embedded M4B chapter markers if no cue file present
- Static file serving for cover art, direct audio streaming with range request support

### Android Client (`android/`)
- **Kotlin + Jetpack Compose** — modern Android UI
- **Media3 (ExoPlayer)** — audio playback engine, handles M4B natively with chapter support
- **MediaSession** — lock screen controls, notification player, Android Auto
- **Retrofit** — API client to the server
- **Room** — local database for offline caching of metadata and playback position
- Background playback via foreground service

### Data Flow
1. Server scans configured library directories on startup and on-demand
2. Extracts metadata from M4B files + cue files, stores in SQLite
3. Android client fetches library catalog via REST API
4. Audio streams directly from server via HTTP range requests
5. Client reports playback position back to server for sync

## Audio File Expectations

- Primary format: `.m4b` (AAC audiobooks with chapter markers)
- Secondary: `.m4a` (AAC without chapter info)
- Each book lives in its own folder (Libation default structure)
- Folder contains: `{title}.m4b` + `{title}.cue` (chapter data)
- Metadata (author, title, narrator, series, cover art) embedded in M4B tags

## Common Commands

### Server
```bash
# Setup
cd server
pip install -r requirements.txt

# Run development server
uvicorn audiplex.main:app --reload --host 0.0.0.0 --port 8000

# Run tests
pytest
pytest tests/test_scanner.py -k "test_name"  # single test

# Rescan library (API call)
curl -X POST http://localhost:8000/api/library/scan
```

### Android
```bash
# Build debug APK
cd android
./gradlew assembleDebug

# Run tests
./gradlew test

# Install on connected device
./gradlew installDebug
```

## Key Design Decisions

- **No transcoding.** M4B/M4A files stream as-is. ExoPlayer handles them natively. This keeps the server dead simple.
- **Chapter info from cue files first**, embedded M4B chapters as fallback. Libation's cue files are more reliable.
- **SQLite, not Postgres/Mongo.** Single-user or small household use. No reason for a separate database service.
- **Range requests for streaming.** Allows seeking without downloading the whole file. Critical for 20+ hour audiobooks.
- **Position sync is server-authoritative.** Client pushes position updates; server stores them. Simple conflict resolution: latest timestamp wins.

## API Convention

- REST API under `/api/`
- JSON responses
- Audio streaming under `/api/stream/{book_id}`
- Cover art under `/api/covers/{book_id}`
- Progress tracking: `GET/PUT /api/progress/{book_id}`

## Migration Note
This project was moved from H: to Q: on 2026-03-25 as part of a filesystem reorganization.
Previous location: H:\Development\audiplex
If you encounter hardcoded paths referencing the old location, update them to the current path.
