"""Shared test fixtures — in-memory DB, FastAPI TestClient, sample data."""

import os
import struct
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from audiplex.auth import get_current_user, hash_password
from audiplex.database import Base, get_db
from audiplex.models import (
    Album,
    Artist,
    Book,
    Chapter,
    PlaybackPosition,
    Playlist,
    PlaylistTrack,
    PlayStat,
    Track,
    User,
)
from audiplex.routers import (
    auth_router,
    dj_voice,
    library,
    music,
    playback,
    progress,
    streaming,
)


def _create_test_app():
    """Create a FastAPI app for testing (no lifespan, no startup scan)."""
    test_app = FastAPI()
    test_app.include_router(auth_router.router)
    test_app.include_router(library.router)
    test_app.include_router(streaming.router)
    test_app.include_router(progress.router)
    test_app.include_router(music.router)
    test_app.include_router(playback.router)
    test_app.include_router(dj_voice.router)
    return test_app


@pytest.fixture
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    session.add(User(
        username="testuser",
        password_hash=hash_password("testpass"),
        display_name="Test User",
        is_admin=True,
    ))
    session.commit()
    session.close()
    return engine


@pytest.fixture
def db_session(db_engine):
    Session = sessionmaker(bind=db_engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def client(db_engine):
    """FastAPI TestClient with in-memory DB and auth bypass."""
    test_app = _create_test_app()
    TestSession = sessionmaker(bind=db_engine)

    def override_get_db():
        session = TestSession()
        try:
            yield session
        finally:
            session.close()

    def override_get_current_user(db: Session = Depends(get_db)):
        return db.query(User).first()

    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[get_current_user] = override_get_current_user

    with TestClient(test_app) as c:
        yield c


@pytest.fixture
def sample_book(db_session):
    """Insert a sample book into the test DB."""
    book = Book(
        title="Test Book",
        author="Test Author",
        narrator="Test Narrator",
        series="Test Series",
        series_sequence="1",
        duration_seconds=3600.0,
        file_path="/fake/path/test.m4b",
        has_cover=False,
        file_size=1000000,
        file_hash="abc123",
    )
    db_session.add(book)
    db_session.flush()

    # Add chapters
    for i in range(5):
        db_session.add(Chapter(
            book_id=book.id,
            index=i,
            title=f"Chapter {i + 1}",
            start_seconds=i * 720.0,
            end_seconds=(i + 1) * 720.0,
        ))

    db_session.commit()
    db_session.refresh(book)
    return book


@pytest.fixture
def sample_cue_file(tmp_path):
    """Create a sample CUE file in Libation format."""
    cue_content = '''REM GENRE Audiobook
REM DATE 2023
PERFORMER "Test Author"
TITLE "Test Book"
FILE "Test Book.MP3" MP3
  TRACK 01 AUDIO
    TITLE "Opening Credits"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Chapter 1 - The Beginning"
    INDEX 01 02:30:50
  TRACK 03 AUDIO
    TITLE "Chapter 2 - The Middle"
    INDEX 01 15:45:00
  TRACK 04 AUDIO
    TITLE "Chapter 3 - The End"
    INDEX 01 32:10:25
'''
    cue_file = tmp_path / "test_book.cue"
    cue_file.write_text(cue_content, encoding="utf-8")
    return str(cue_file)


def _create_minimal_m4b(path: str, title: str = "Test Book", author: str = "Test Author"):
    """Create a minimal valid M4B file using mutagen.

    Creates a tiny AAC file that mutagen can read.
    """
    from mutagen.mp4 import MP4, MP4Cover

    # We need a valid MP4 container. Create a minimal one.
    # The simplest approach: create a tiny valid ftyp + moov structure
    # Actually, let's use mutagen to create it from scratch
    # mutagen can't create from scratch, so let's build a minimal MP4 manually

    # Minimal MP4 file structure:
    # ftyp box + moov box (with mvhd)
    data = bytearray()

    # ftyp box
    ftyp_data = b'M4B ' + b'\x00\x00\x00\x00' + b'M4B ' + b'mp42' + b'isom'
    ftyp_size = 8 + len(ftyp_data)
    data.extend(struct.pack('>I', ftyp_size))
    data.extend(b'ftyp')
    data.extend(ftyp_data)

    # moov box with mvhd
    mvhd_data = bytearray(108)  # version 0 mvhd is 108 bytes total (100 data + 8 header)
    mvhd_data[0:4] = b'\x00\x00\x00\x00'  # version + flags
    mvhd_data[12:16] = struct.pack('>I', 1000)  # timescale
    mvhd_data[16:20] = struct.pack('>I', 60000)  # duration (60 seconds)
    mvhd_data[20:24] = struct.pack('>I', 0x00010000)  # rate
    mvhd_data[24:26] = struct.pack('>H', 0x0100)  # volume

    mvhd_box = struct.pack('>I', 8 + len(mvhd_data)) + b'mvhd' + bytes(mvhd_data)

    moov_box = struct.pack('>I', 8 + len(mvhd_box)) + b'moov' + mvhd_box

    data.extend(moov_box)

    with open(path, 'wb') as f:
        f.write(bytes(data))

    # Now use mutagen to add tags
    try:
        mp4 = MP4(path)
    except Exception:
        # If mutagen can't parse our minimal file, that's ok —
        # tests that need real M4B files will mock mutagen instead
        return False

    mp4.tags["\xa9nam"] = [title]
    mp4.tags["\xa9ART"] = [author]
    mp4.save()
    return True


@pytest.fixture
def m4b_file(tmp_path):
    """Create a minimal M4B test file.

    Returns the path, or None if creation failed (tests should mock mutagen in that case).
    """
    path = str(tmp_path / "test_book.m4b")
    success = _create_minimal_m4b(path)
    if success:
        return path
    return None


@pytest.fixture
def audio_file_on_disk(tmp_path):
    """Create a fake audio file for streaming tests (just random bytes)."""
    content = b"FAKE_AUDIO_DATA_" * 1000  # 16KB of fake data
    audio_path = tmp_path / "stream_test.m4b"
    audio_path.write_bytes(content)
    return str(audio_path), len(content)


@pytest.fixture
def sample_artist(db_session):
    artist = Artist(name="Test Artist")
    db_session.add(artist)
    db_session.commit()
    db_session.refresh(artist)
    return artist


@pytest.fixture
def sample_album(db_session, sample_artist):
    album = Album(
        title="Test Album",
        artist_id=sample_artist.id,
        genre="Rock",
        year=2020,
        duration_seconds=2400.0,
        track_count=10,
        folder_path="/fake/music/Test Artist/Test Album",
    )
    db_session.add(album)
    db_session.commit()
    db_session.refresh(album)
    return album


@pytest.fixture
def sample_track(db_session, sample_album):
    track = Track(
        title="Test Track",
        album_id=sample_album.id,
        artist_id=sample_album.artist_id,
        disc_number=1,
        track_number=1,
        duration_seconds=240.0,
        file_path="/fake/music/Test Artist/Test Album/01 Test Track.mp3",
        file_size=5_000_000,
    )
    db_session.add(track)
    db_session.commit()
    db_session.refresh(track)
    return track


@pytest.fixture
def music_root_layout(tmp_path):
    """Build an on-disk music layout mirroring real Stacked Deck shapes.

    Returns the music root path. Files are empty (mutagen is mocked in
    tests). Layout::

        music/
          Artists & Albums/
            Jazz/
              Miles Davis/
                Kind of Blue/
                  01 So What.mp3
                  02 Freddie Freeloader.mp3
                  cover.jpg
            Classical/
              Beethoven/
                Symphonies/
                  CD1/01 - Symphony 1.mp3
                  CD2/01 - Symphony 2.mp3
              Mozart/
                Concertos/
                  -cd 1/01 - K1.mp3
                  -cd 2/01 - K2.mp3
          Bike Music/
            track1.mp3
            track2.mp3
            Folder.jpg
          Mixed/
            weird.wma
            real.mp3
          Embedded Only/
            song.mp3
    """
    music = tmp_path / "music"
    music.mkdir()

    # Jazz / Miles Davis / Kind of Blue
    kob = music / "Artists & Albums" / "Jazz" / "Miles Davis" / "Kind of Blue"
    kob.mkdir(parents=True)
    (kob / "01 So What.mp3").write_bytes(b"")
    (kob / "02 Freddie Freeloader.mp3").write_bytes(b"")
    (kob / "cover.jpg").write_bytes(b"\xff\xd8\xff" + b"FAKEJPG")

    # Classical / Beethoven / Symphonies (CD1/CD2)
    sym = music / "Artists & Albums" / "Classical" / "Beethoven" / "Symphonies"
    (sym / "CD1").mkdir(parents=True)
    (sym / "CD1" / "01 - Symphony 1.mp3").write_bytes(b"")
    (sym / "CD2").mkdir(parents=True)
    (sym / "CD2" / "01 - Symphony 2.mp3").write_bytes(b"")

    # Classical / Mozart / Concertos (-cd 1 / -cd 2)
    moz = music / "Artists & Albums" / "Classical" / "Mozart" / "Concertos"
    (moz / "-cd 1").mkdir(parents=True)
    (moz / "-cd 1" / "01 - K1.mp3").write_bytes(b"")
    (moz / "-cd 2").mkdir(parents=True)
    (moz / "-cd 2" / "01 - K2.mp3").write_bytes(b"")

    # Loose top-level
    bike = music / "Bike Music"
    bike.mkdir()
    (bike / "track1.mp3").write_bytes(b"")
    (bike / "track2.mp3").write_bytes(b"")
    (bike / "Folder.jpg").write_bytes(b"\xff\xd8\xff" + b"BIKEJPG")

    # WMA + mp3 mixed
    mixed = music / "Mixed"
    mixed.mkdir()
    (mixed / "weird.wma").write_bytes(b"")
    (mixed / "real.mp3").write_bytes(b"")

    # Embedded-cover-only
    emb = music / "Embedded Only"
    emb.mkdir()
    (emb / "song.mp3").write_bytes(b"")

    return music


@pytest.fixture
def sample_playlist(db_session, sample_track):
    user = db_session.query(User).first()
    playlist = Playlist(name="Test Playlist", user_id=user.id)
    db_session.add(playlist)
    db_session.flush()
    db_session.add(PlaylistTrack(
        playlist_id=playlist.id,
        track_id=sample_track.id,
        position=0,
    ))
    db_session.commit()
    db_session.refresh(playlist)
    return playlist


@pytest.fixture(autouse=True)
def isolated_client_logs(tmp_path, monkeypatch):
    """Keep the two durable on-disk logs out of the real files.

    add_client_log appends every process_exit entry to a file (#3021), and
    several tests post those — without this, running the suite would graft
    fake deaths onto the record we keep specifically so a real one can never
    be lost.

    The link history (#900 Phase 3a) has exactly the same exposure and was
    caught the hard way: every test that polls /command/next writes a link
    event, so one suite run left a dozen bogus "resumed" entries in the real
    file. A liveness record nobody can trust is worse than none, since the
    whole reason it exists is to answer "did the link actually hold".
    """
    from audiplex import playback_bus

    monkeypatch.setattr(
        playback_bus, "EXIT_LOG_PATH", tmp_path / "client-exits.jsonl"
    )
    monkeypatch.setattr(
        playback_bus, "LINK_LOG_PATH", tmp_path / "link-history.jsonl"
    )
    # #3249: the playback diag log gets every client-log entry and command
    # transition, so it needs the same isolation.
    monkeypatch.setattr(
        playback_bus, "DIAG_LOG_PATH", tmp_path / "playback-diag.jsonl"
    )
    # #3601: the resume snapshot too, or a test run overwrites Todd's queue.
    monkeypatch.setattr(
        playback_bus, "LAST_QUEUE_PATH", tmp_path / "last_queue.json"
    )


@pytest.fixture(autouse=True)
def isolated_dj_pool(tmp_path, monkeypatch):
    """Keep the DJ pool singleton off the real dj_pool.json (#5495), and keep
    the MCP server's DELETE/PATCH helpers from ever reaching a live server:
    dj_mix / dj_play_now stop the pool over HTTP, and a test that stubs only
    _get/_post would otherwise send a real DELETE. Tests that care stub these
    themselves (their monkeypatch runs after this one and wins)."""
    import sys

    from audiplex import dj_pool

    monkeypatch.setenv("AUDIPLEX_DJ_POOL_STATE", str(tmp_path / "dj_pool.json"))
    dj_pool.reset_pool_singleton()
    mcp = sys.modules.get("audiplex_mcp.server")
    if mcp is not None:
        async def no_delete(path):
            return {"stopped": False}

        async def no_patch(path, body):
            raise AssertionError(f"unstubbed PATCH {path} in a test")

        monkeypatch.setattr(mcp, "_delete", no_delete)
        if hasattr(mcp, "_patch"):
            monkeypatch.setattr(mcp, "_patch", no_patch)
        if hasattr(mcp, "ACK_WAIT_S"):  # #ride0928: stubs never ack; don't sit out the wait
            monkeypatch.setattr(mcp, "ACK_WAIT_S", 0.05)
        if hasattr(mcp, "START_VERIFY_S"):  # #2843: same for the playing= verdict
            monkeypatch.setattr(mcp, "START_VERIFY_S", 0.05)
            monkeypatch.setattr(mcp, "VERIFY_POLL_S", 0.01)
    # #ride0928: the talk guard defaults to Pantheon's REAL speech state; a test
    # run must not flake because Todd happened to be talking. Missing = unguarded.
    monkeypatch.setenv("DJ_SPEECH_STATE_FILE", str(tmp_path / "no-speech-state.json"))
    yield
    dj_pool.reset_pool_singleton()


@pytest.fixture(autouse=True)
def isolated_book_handoff(monkeypatch):
    """The phone-book handoff lookup (#2680) must never open the real DB.
    Tests that exercise it patch _book_session to a test-engine session."""
    from audiplex import playback_bus

    monkeypatch.setattr(playback_bus, "_book_session", lambda: None)


@pytest.fixture(autouse=True)
def _no_live_pantheon_mute(monkeypatch, tmp_path):
    """#3493: the DJ MCP's mute gate must never read the live quiet window, or a
    real quiet hour reddens every DJ tool test. Pointing it at a missing Pantheon
    runs the ungated (sound on) path; test_3493 overrides this per test."""
    monkeypatch.setenv("DJ_PANTHEON_SRC", str(tmp_path / "no-pantheon"))
