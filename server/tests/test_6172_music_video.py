"""Music video (#6172): planner, analysis, worker and router, with no ComfyUI/ffmpeg/network."""

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import sessionmaker

from audiplex.auth import hash_password
from audiplex.config import get_settings
from audiplex.database import get_db
from audiplex.models import Album, Artist, MusicVideoJob, Track, User
from audiplex.music_video import analysis, planner, worker
from audiplex.routers import auth_router
from audiplex.routers import music_video as mv_router

# ---- planner ----------------------------------------------------------------


def test_images_needed():
    assert planner.images_needed(90, 5) == 18
    assert planner.images_needed(91, 5) == 19
    assert planner.images_needed(0.5, 5) == 1
    assert planner.images_needed(0, 5) == 1


def test_h3_length_grid_and_cap():
    for secs in [0.1, 3, 3.75, 5, 5.5, 8, 10, 14.9, 15.1, 100]:
        n = planner.h3_length(secs)
        assert (n - 5) % 17 == 0
        assert planner.H3_MIN_FRAMES <= n <= planner.H3_MAX_FRAMES
        if secs * planner.FPS <= planner.H3_MAX_FRAMES:
            assert n >= secs * planner.FPS
    assert planner.h3_length(100) == planner.H3_MAX_FRAMES == 362


def _beats(duration, step=0.5):
    return [round(i * step, 3) for i in range(1, int(duration / step))]


def _check_contiguous(segs, duration, n):
    assert len(segs) == n
    assert segs[0].start == 0.0 and segs[-1].end == duration
    for a, b in zip(segs, segs[1:]):
        assert a.end == b.start
    assert all(s.end > s.start for s in segs)


def test_plan_single_segment():
    segs = planner.plan_segments(40.0, _beats(40), [[5, 30]], 1)
    _check_contiguous(segs, 40.0, 1)
    assert not segs[0].forced


def test_plan_invalid_n():
    with pytest.raises(ValueError):
        planner.plan_segments(40.0, [], [], 0)


def test_plan_cuts_land_on_beats():
    segs = planner.plan_segments(60.0, _beats(60, 0.5), [], 6)
    _check_contiguous(segs, 60.0, 6)
    for s in segs[:-1]:
        assert (s.end / 0.5) == pytest.approx(round(s.end / 0.5))
        assert not s.forced


@pytest.mark.parametrize("n", [2, 3, 5, 8, 12, 15, 20])
def test_plan_no_cut_inside_vocals(n):
    duration = 120.0
    spans = [[10 * k + 1, 10 * k + 8] for k in range(12)]  # 3 s gaps every 10 s
    segs = planner.plan_segments(duration, _beats(duration), spans, n)
    _check_contiguous(segs, duration, n)
    for s in segs[:-1]:
        assert not planner.in_vocals(s.end, spans), f"cut {s.end} inside vocals (n={n})"
        assert not s.forced
    if n >= 12:  # 10 s/clip or shorter: every clip within bounds
        assert all(planner.MIN_CLIP - 1e-9 <= s.duration <= planner.MAX_CLIP + 1e-9 for s in segs)


def test_plan_uses_gap_midpoint_when_no_safe_beat():
    spans = [[0, 28], [32, 60]]  # the only silence is 28..32; beats all sit in vocals
    segs = planner.plan_segments(60.0, [10.0, 20.0, 50.0], spans, 2)
    assert segs[0].end == pytest.approx(30.0)
    assert not segs[0].forced


def test_plan_forced_only_when_unavoidable():
    segs = planner.plan_segments(60.0, _beats(60), [[0, 60]], 3)
    _check_contiguous(segs, 60.0, 3)
    assert [s.forced for s in segs] == [True, True, False]
    # an avoidable layout never sets the flag
    segs = planner.plan_segments(60.0, _beats(60), [[0, 25], [35, 60]], 2)
    assert not any(s.forced for s in segs)
    assert 25 + planner.VOCAL_MARGIN <= segs[0].end <= 35 - planner.VOCAL_MARGIN


def test_plan_clip_length_bounds():
    segs = planner.plan_segments(30.0, _beats(30, 0.25), [[3, 5], [9, 11]], 6)
    _check_contiguous(segs, 30.0, 6)
    assert all(planner.MIN_CLIP - 1e-9 <= s.duration <= planner.MAX_CLIP + 1e-9 for s in segs)


def test_plan_very_short_song_still_returns_n_segments():
    segs = planner.plan_segments(3.0, _beats(3), [], 4)
    _check_contiguous(segs, 3.0, 4)


# ---- analysis ---------------------------------------------------------------


def test_vocal_spans_empty():
    assert analysis.vocal_spans_from_rms([], 0.1) == []


def test_vocal_spans_merge_breath_and_drop_blip():
    # hop 0.1 s: loud 0.0-1.0, 0.2 s breath (< MERGE_GAP), loud 1.2-2.0,
    # silence, a 0.1 s blip (< MIN_SPAN), silence.
    loud, quiet = -10.0, -80.0
    rms = [loud] * 10 + [quiet] * 2 + [loud] * 8 + [quiet] * 10 + [loud] * 1 + [quiet] * 5
    assert analysis.vocal_spans_from_rms(rms, 0.1) == [[0.0, 2.0]]


def test_vocal_spans_long_gap_splits_and_threshold_is_relative_to_peak():
    rms = [-10.0] * 5 + [-80.0] * 10 + [-35.0] * 5  # floor is -40, so -35 counts
    spans = analysis.vocal_spans_from_rms(rms, 0.1)
    assert spans == [[0.0, 0.5], [1.5, 2.0]]
    assert analysis.vocal_spans_from_rms([x - 20 for x in rms], 0.1) == spans  # relative
    assert analysis.vocal_spans_from_rms([-10.0] * 5 + [-45.0] * 5, 0.1) == [[0.0, 0.5]]


# ---- worker: pure -----------------------------------------------------------


def test_build_prompt():
    assert worker.build_prompt("") == worker.DEFAULT_MOTION
    assert worker.build_prompt(None) == worker.DEFAULT_MOTION
    assert worker.build_prompt("<lora:x:0.8>  ") == worker.DEFAULT_MOTION
    p = worker.build_prompt("A fox  runs. <LoRA:nsfw:1> through snow.")
    assert p.startswith("A fox runs. through snow.")
    assert p.endswith(worker.DEFAULT_MOTION)
    assert "lora" not in p.lower()


# ---- shared DB fixtures -----------------------------------------------------


@pytest.fixture
def session(db_engine):
    s = sessionmaker(bind=db_engine)()
    yield s
    s.close()


def _make_track(session, tmp_path, duration=12.0):
    artist = Artist(name="A")
    session.add(artist)
    session.flush()
    album = Album(title="B", artist_id=artist.id, folder_path=str(tmp_path))
    session.add(album)
    session.flush()
    song = tmp_path / "song.mp3"
    song.write_bytes(b"x")
    t = Track(title="Song", album_id=album.id, artist_id=artist.id,
              duration_seconds=duration, file_path=str(song))
    session.add(t)
    session.commit()
    return t


def _png(path, size=(600, 400)):
    Image.new("RGB", size, (200, 30, 30)).save(path, "PNG")
    return path


# ---- worker: process_job ----------------------------------------------------


class Pipeline:
    pass


@pytest.fixture
def pipeline(session, tmp_path, monkeypatch):
    """A 12 s song (3 clips) with every external dependency faked."""
    track = _make_track(session, tmp_path, 12.0)
    imgs = [str(_png(tmp_path / f"i{k}.png", (8, 8))) for k in range(3)]
    job = MusicVideoJob(track_id=track.id, quality="draft", image_folder=str(tmp_path),
                        image_paths=json.dumps(imgs), direction="neon <lora:z:1> city",
                        status="queued", clips_total=3)
    session.add(job)
    session.commit()

    comfy_out = tmp_path / "comfy_out"
    (comfy_out / "audiplex_mv").mkdir(parents=True)
    monkeypatch.setattr(worker, "COMFY_OUTPUT", comfy_out)
    monkeypatch.setattr(analysis, "analyze", lambda p, c: {
        "duration": 12.0, "tempo": 120.0, "beats": _beats(12), "vocal_spans": []})
    monkeypatch.setattr(worker, "probe_comfy_port", lambda *a, **k: 8188)
    monkeypatch.setattr(worker, "gpu_wait_reason", lambda port: None)

    def fake_stitch(clips, durations, song, out, workdir):
        assert all(c.exists() for c in clips)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"video")
        return out

    monkeypatch.setattr(worker, "stitch", fake_stitch)

    calls = []

    def fake_run(name, params, **kw):
        calls.append((name, params, kw))
        fn = f"x{len(calls)}.mp4"
        (comfy_out / "audiplex_mv" / fn).write_bytes(b"clip")
        return {"outputs": [{"filename": fn, "subfolder": "audiplex_mv", "kind": "videos"}]}

    p = Pipeline()
    p.job, p.imgs, p.calls, p.run, p.base = job, imgs, calls, fake_run, tmp_path / "base"
    return p


def test_process_job_end_to_end(session, pipeline):
    p = pipeline
    worker.process_job(session, p.job, p.base, run=p.run, sleep=lambda s: None)
    session.refresh(p.job)
    assert p.job.status == "done"
    assert p.job.clips_done == 3
    assert p.job.output_path and p.job.output_path.endswith(".mp4")
    assert (p.base / "videos" / f"music_video_{p.job.id:05d}.mp4").is_file()
    assert [c[1]["first_frame"] for c in p.calls] == p.imgs
    assert all(c[0] == "h3" for c in p.calls)
    assert len({c[1]["seed"] for c in p.calls}) == 3
    plan = json.loads(p.job.plan_json)
    assert len(plan["segments"]) == 3
    for (_, params, _), seg in zip(p.calls, plan["segments"]):
        assert (params["length"] - 5) % 17 == 0
        assert params["length"] >= (seg["end"] - seg["start"]) * planner.FPS
        assert params["prompt"].startswith("neon city.")


def test_process_job_resumes_finished_clips(session, pipeline):
    p = pipeline
    done = worker.job_dir(p.base, p.job.id) / "clips" / "clip_000.mp4"
    done.parent.mkdir(parents=True)
    done.write_bytes(b"old")
    worker.process_job(session, p.job, p.base, run=p.run, sleep=lambda s: None)
    assert len(p.calls) == 2
    assert p.calls[0][1]["first_frame"] == p.imgs[1]
    assert done.read_bytes() == b"old"
    session.refresh(p.job)
    assert p.job.status == "done"


def test_process_job_waits_for_gpu(session, pipeline, monkeypatch):
    p = pipeline
    reasons = iter(["Waiting: busy", "Waiting: busy"])
    monkeypatch.setattr(worker, "gpu_wait_reason", lambda port: next(reasons, None))
    seen = []

    def sleep(s):
        session.refresh(p.job)
        seen.append((s, p.job.detail))

    worker.process_job(session, p.job, p.base, run=p.run, sleep=sleep)
    assert seen == [(worker.WAIT_POLL, "Waiting: busy")] * 2
    session.refresh(p.job)
    assert p.job.status == "done"


def test_process_job_cancel_midway(session, pipeline, db_engine):
    p = pipeline

    def run_then_cancel(name, params, **kw):
        out = p.run(name, params, **kw)
        other = sessionmaker(bind=db_engine)()
        other.get(MusicVideoJob, p.job.id).status = "cancelled"
        other.commit()
        other.close()
        return out

    with pytest.raises(worker.Cancelled):
        worker.process_job(session, p.job, p.base, run=run_then_cancel, sleep=lambda s: None)
    assert len(p.calls) == 1
    assert not (p.base / "videos").exists()


# ---- router -----------------------------------------------------------------


@pytest.fixture
def api(db_engine, tmp_path, monkeypatch):
    app = FastAPI()
    app.include_router(auth_router.router)
    app.include_router(mv_router.router)
    Sess = sessionmaker(bind=db_engine)

    def override():
        s = Sess()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override
    monkeypatch.setattr(get_settings(), "music_video_dir", str(tmp_path / "mvdir"))
    spawned = []
    monkeypatch.setattr(worker, "spawn_worker", lambda base: spawned.append(base) or True)

    s = Sess()
    s.add(User(username="plain", password_hash=hash_password("pw"), display_name="P", is_admin=False))
    s.commit()
    s.close()

    with TestClient(app) as c:
        def hdr(u, pw):
            tok = c.post("/api/auth/login", json={"username": u, "password": pw}).json()["token"]
            return {"Authorization": f"Bearer {tok}"}

        c.h = hdr("testuser", "testpass")
        c.plain_h = hdr("plain", "pw")
        c.spawned = spawned
        yield c


@pytest.fixture
def folder(tmp_path):
    f = tmp_path / "pics"
    f.mkdir()
    (f / "sub").mkdir()
    _png(f / "a.png")
    _png(f / "b.PNG")
    (f / "notes.txt").write_text("hi")
    (f / "c.mp4").write_bytes(b"v")
    sib = tmp_path / "sibling"
    sib.mkdir()
    _png(sib / "x.png")
    return f


def test_estimate_has_no_default_folder(api, session, tmp_path):
    t = _make_track(session, tmp_path, 90.0)
    r = api.get(f"/api/music-video/estimate/{t.id}", headers=api.h)
    assert r.status_code == 200
    assert r.json()["n_images"] == 18
    assert r.json()["last_folder"] is None
    assert api.get("/api/music-video/estimate/9999", headers=api.h).status_code == 404
    assert api.get(f"/api/music-video/estimate/{t.id}?quality=bogus", headers=api.h).status_code == 400


def test_list_images(api, folder):
    r = api.get("/api/music-video/images", params={"folder": str(folder)}, headers=api.h)
    assert r.status_code == 200
    body = r.json()
    assert body["images"] == ["a.png", "b.PNG"]
    assert body["subfolders"] == ["sub"]
    assert api.get("/api/music-video/images", params={"folder": "pics"}, headers=api.h).status_code == 400
    assert api.get("/api/music-video/images", params={"folder": ""}, headers=api.h).status_code == 400
    missing = str(folder / "nope")
    assert api.get("/api/music-video/images", params={"folder": missing}, headers=api.h).status_code == 404


# "x.png" exists only in the sibling folder, never in `folder`
BAD_NAMES = ["../x.png", "..\\x.png", "sub/x.png", "sub\\x.png", "C:\\x.png", "C:/x.png",
             "/x.png", ".", "..", "", "notes.txt", "c.mp4", "missing.png", "a.png/..", "x.png"]


@pytest.mark.parametrize("name", BAD_NAMES)
def test_thumb_rejects_bad_names(api, folder, name):
    r = api.get("/api/music-video/thumb", params={"folder": str(folder), "name": name}, headers=api.h)
    assert r.status_code in (400, 404), (name, r.status_code)


def test_thumb_returns_jpeg(api, folder):
    r = api.get("/api/music-video/thumb", params={"folder": str(folder), "name": "a.png"}, headers=api.h)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content[:2] == b"\xff\xd8"


@pytest.mark.parametrize("name", BAD_NAMES)
def test_create_job_rejects_bad_names(api, session, tmp_path, folder, name):
    t = _make_track(session, tmp_path, 10.0)  # needs 2 images
    body = {"track_id": t.id, "folder": str(folder), "images": ["a.png", name]}
    r = api.post("/api/music-video/jobs", json=body, headers=api.h)
    assert r.status_code in (400, 404), (name, r.status_code)
    assert session.query(MusicVideoJob).count() == 0
    assert api.spawned == []


def test_create_job_wrong_count_and_long_direction(api, session, tmp_path, folder):
    t = _make_track(session, tmp_path, 10.0)
    for imgs in (["a.png"], ["a.png", "b.PNG", "a.png"]):
        r = api.post("/api/music-video/jobs", headers=api.h,
                     json={"track_id": t.id, "folder": str(folder), "images": imgs})
        assert r.status_code == 400
    r = api.post("/api/music-video/jobs", headers=api.h, json={
        "track_id": t.id, "folder": str(folder), "images": ["a.png", "b.PNG"], "direction": "x" * 1001})
    assert r.status_code == 400
    assert session.query(MusicVideoJob).count() == 0


def test_create_job_valid(api, session, tmp_path, folder):
    t = _make_track(session, tmp_path, 10.0)
    r = api.post("/api/music-video/jobs", headers=api.h, json={
        "track_id": t.id, "folder": str(folder), "images": ["a.png", "b.PNG"], "direction": "  moody  "})
    assert r.status_code == 201
    assert r.json()["status"] == "queued" and r.json()["clips_total"] == 2
    assert len(api.spawned) == 1
    job = session.query(MusicVideoJob).one()
    assert job.direction == "moody"
    paths = json.loads(job.image_paths)
    assert len(paths) == 2
    root = str(folder.resolve())
    assert job.image_folder == root
    assert all(p.startswith(root) and "sibling" not in p for p in paths)
    est = api.get(f"/api/music-video/estimate/{t.id}", headers=api.h).json()
    assert est["last_folder"] == root  # remembered from his last job, never a default


def test_non_admin_forbidden(api, session, tmp_path, folder):
    t = _make_track(session, tmp_path, 10.0)
    h = api.plain_h
    assert api.get(f"/api/music-video/estimate/{t.id}", headers=h).status_code == 403
    assert api.get("/api/music-video/images", params={"folder": str(folder)}, headers=h).status_code == 403
    assert api.get("/api/music-video/thumb", params={"folder": str(folder), "name": "a.png"},
                   headers=h).status_code == 403
    r = api.post("/api/music-video/jobs", headers=h, json={
        "track_id": t.id, "folder": str(folder), "images": ["a.png", "b.PNG"]})
    assert r.status_code == 403
    assert api.get("/api/music-video/jobs", headers=h).status_code == 403
    assert api.get("/api/music-video/images", params={"folder": str(folder)}).status_code in (401, 403)


def _job_row(session, track_id, status, **kw):
    j = MusicVideoJob(track_id=track_id, image_folder="/f", image_paths="[]", status=status, **kw)
    session.add(j)
    session.commit()
    return j.id


def test_cancel_and_retry(api, session, tmp_path):
    t = _make_track(session, tmp_path)
    jid = _job_row(session, t.id, "rendering")
    assert api.post(f"/api/music-video/jobs/{jid}/cancel", headers=api.h).json()["status"] == "cancelled"
    assert api.post(f"/api/music-video/jobs/{jid}/retry", headers=api.h).json()["status"] == "queued"
    assert len(api.spawned) == 1
    # cancel only touches active jobs, retry only failed/cancelled ones
    done = _job_row(session, t.id, "done")
    assert api.post(f"/api/music-video/jobs/{done}/cancel", headers=api.h).json()["status"] == "done"
    assert api.post(f"/api/music-video/jobs/{done}/retry", headers=api.h).json()["status"] == "done"
    assert api.post(f"/api/music-video/jobs/{jid}/retry", headers=api.h).json()["status"] == "queued"
    assert api.post("/api/music-video/jobs/9999/cancel", headers=api.h).status_code == 404


def test_video_url_and_signed_stream(api, session, tmp_path):
    t = _make_track(session, tmp_path)
    video = tmp_path / "out.mp4"
    video.write_bytes(b"VIDEOBYTES")
    pending = _job_row(session, t.id, "rendering")
    assert api.get(f"/api/music-video/jobs/{pending}/video-url", headers=api.h).status_code == 409
    jid = _job_row(session, t.id, "done", output_path=str(video))

    url = api.get(f"/api/music-video/jobs/{jid}/video-url", headers=api.h).json()["url"]
    r = api.get(url)  # no auth header: <video> can't send one
    assert r.status_code == 200 and r.content == b"VIDEOBYTES"
    assert r.headers["content-type"].startswith("video/mp4")

    base = f"/api/music-video/jobs/{jid}/video"
    exp = int(time.time()) + 600
    assert api.get(f"{base}?exp={exp}&sig={'a' * 64}").status_code == 403
    assert api.get(f"{base}?exp={exp}&sig={mv_router._sig(jid, exp + 1)}").status_code == 403
    old = int(time.time()) - 5
    assert api.get(f"{base}?exp={old}&sig={mv_router._sig(jid, old)}").status_code == 403
    assert api.get(f"{base}?exp={exp}&sig={mv_router._sig(jid + 1, exp)}").status_code == 403
