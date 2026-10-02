"""Music video (#6172): planner, analysis, worker and router, with no ComfyUI/ffmpeg/network."""

import io
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


def test_frame_size_every_aspect():
    for q in planner.QUALITY:
        w16, h16 = planner.frame_size(q, "16:9")
        for a, (aw, ah) in planner.ASPECTS.items():
            w, h = planner.frame_size(q, a)
            assert w % 32 == 0 and h % 32 == 0, (q, a)
            assert abs(w * h / (w16 * h16) - 1) < 0.15, (q, a)  # the render estimate still holds
            assert abs(w / h - aw / ah) < 0.1, (q, a)
    assert planner.frame_size("final") == (864, 480)


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
    # LoRA tags (standard syntax) ride along at the end, out of the sentence
    assert worker.build_prompt("<lora:x:0.8>  ") == worker.DEFAULT_MOTION + " <lora:x:0.8>"
    p = worker.build_prompt("A fox  runs. <LoRA:fur:1> through snow.")
    assert p.startswith("A fox runs. through snow.")
    assert p.endswith(worker.DEFAULT_MOTION + " <LoRA:fur:1>")


def test_build_prompt_loras_job_wide_plus_per_clip():
    job = worker.lora_tags("neon <lora:city:0.7> night")
    assert job == ["<lora:city:0.7>"]
    # a clip with its own direction keeps the job-wide LoRA and adds its own, no duplicates
    p = worker.build_prompt("drummer <lora:drums:1> <lora:city:0.7>", "{direction}", job)
    assert p == "drummer <lora:city:0.7> <lora:drums:1>"
    assert worker.build_prompt("", None, []) == worker.DEFAULT_MOTION


def test_build_prompt_template():  # #6867
    assert worker.build_prompt("spin", "Slow {direction}, neon.") == "Slow spin, neon."
    assert worker.build_prompt("", "Slow {direction}, neon.") == "Slow neon."
    assert worker.build_prompt("spin.", "") == "spin. " + worker.DEFAULT_MOTION
    assert worker.build_prompt("spin", "No placeholder") == "spin. No placeholder"
    assert worker.build_prompt("", "{direction}") == worker.DEFAULT_MOTION
    assert worker.build_prompt("x", "<lora:a:1> {direction} go") == "x go <lora:a:1>"
    assert worker.build_prompt("", worker.DEFAULT_TEMPLATE) == worker.DEFAULT_MOTION


# ---- variable planner (#6867) -----------------------------------------------

def _check_var(segs, duration):
    assert segs[0].start == 0 and abs(segs[-1].end - duration) < 1e-6
    for a, b in zip(segs, segs[1:]):
        assert a.end == b.start
    for s in segs:
        assert planner.VAR_MIN - 1e-6 <= s.duration <= planner.VAR_MAX + 1e-6, s


@pytest.mark.parametrize("duration", [15.0, 15.1, 20.0, 31.0, 90.0, 183.7, 412.3])
def test_plan_variable_bounds(duration):
    segs = planner.plan_variable(duration, _beats(duration), [])
    _check_var(segs, duration)
    assert not any(s.forced for s in segs)


def test_plan_variable_short_song_is_one_clip():
    assert len(planner.plan_variable(4.0, [], [])) == 1
    assert len(planner.plan_variable(15.0, [], [])) == 1


def test_plan_variable_count_follows_the_song():
    # instrumental: no lyric reason to cut, so clips sit near the 8 s target
    segs = planner.plan_variable(96.0, _beats(96.0), [])
    assert 10 <= len(segs) <= 14
    # long vocal lines push cuts into the gaps, so clips get longer and fewer
    spans = [[g + 0.5, g + 12.5] for g in range(0, 96, 14)]
    segs2 = planner.plan_variable(98.0, _beats(98.0), spans)
    _check_var(segs2, 98.0)
    assert not any(s.forced for s in segs2)
    assert len(segs2) < len(segs)


def test_plan_variable_no_mid_lyric_cut_when_gaps_allow():
    spans = [[1, 9], [10.5, 19], [20.5, 33], [34.5, 44], [45.5, 58]]
    segs = planner.plan_variable(60.0, _beats(60.0), spans)
    _check_var(segs, 60.0)
    for s in segs[:-1]:
        assert not planner.in_vocals(s.end, spans), s


def test_plan_variable_forced_only_when_a_vocal_line_outruns_max():
    segs = planner.plan_variable(40.0, _beats(40.0), [[2, 38]])
    _check_var(segs, 40.0)
    assert any(s.forced for s in segs)


def test_plan_dict_estimate_uses_frames():
    segs = planner.plan_variable(60.0, _beats(60.0), [])
    d = planner.plan_dict(segs, "final")
    assert d["n_images"] == len(segs) == len(d["segments"])
    assert d["clip_min"] >= 5 and d["clip_max"] <= 15
    total = sum(int(planner.h3_length(s.duration) * 1.6) for s in segs)
    assert d["est_render_seconds"] == total
    assert d["clip_render_min"] <= d["clip_render_max"]
    assert planner.plan_dict(segs, "draft")["est_render_seconds"] < total


def test_plan_dict_marks_clips_with_singing():
    # intro 0-10 s, vocals 10-30 s, instrumental 30-45 s, vocals 45-60 s
    spans = [(10.0, 30.0), (45.0, 60.0)]
    segs = [planner.Segment(0, 9.5), planner.Segment(9.5, 20), planner.Segment(20, 30.5),
            planner.Segment(30.5, 44.5), planner.Segment(44.5, 60)]
    d = planner.plan_dict(segs, "draft", spans)
    assert [s["sings"] for s in d["segments"]] == [False, True, True, False, True]
    assert d["segments"][1]["vocals"] == 10.0 and d["vocal_clips"] == 3
    assert planner.vocal_seconds(29.5, 31, spans) == 0.5  # a sliver of a line isn't singing
    assert all(s["sings"] is False for s in planner.plan_dict(segs, "draft")["segments"])


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
    # clip 1 lip syncs; clip 2 has its own direction (plain-string rows are tested separately)
    clips = [{"path": imgs[0], "sing": False, "prompt": ""},
             {"path": imgs[1], "sing": True, "prompt": ""},
             {"path": imgs[2], "sing": False, "prompt": "close-up of <lora:q:1> the drummer"}]  # job LoRA z + own q
    job = MusicVideoJob(track_id=track.id, quality="draft", image_folder=str(tmp_path),
                        image_paths=json.dumps(clips), direction="neon <lora:z:1> city",
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
    slices = []

    def fake_slice(song, start, end, dest):
        slices.append((song, start, end, dest))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"wav")
        return dest

    monkeypatch.setattr(worker, "slice_song", fake_slice)

    calls = []

    def fake_run(name, params, **kw):
        calls.append((name, params, kw))
        fn = f"x{len(calls)}.mp4"
        (comfy_out / "audiplex_mv" / fn).write_bytes(b"clip")
        return {"outputs": [{"filename": fn, "subfolder": "audiplex_mv", "kind": "videos"}]}

    p = Pipeline()
    p.job, p.imgs, p.calls, p.run, p.base = job, imgs, calls, fake_run, tmp_path / "base"
    p.slices, p.song = slices, track.file_path
    return p


def _frames(p):
    return [worker.job_dir(p.base, p.job.id) / "frames" / f"frame_{i:03d}.png" for i in range(3)]


def test_process_job_end_to_end(session, pipeline):
    p = pipeline
    worker.process_job(session, p.job, p.base, run=p.run, sleep=lambda s: None)
    session.refresh(p.job)
    assert p.job.status == "done"
    assert p.job.clips_done == 3
    assert p.job.output_path and p.job.output_path.endswith(".mp4")
    assert (p.base / "videos" / f"music_video_{p.job.id:05d}.mp4").is_file()
    # H3 gets the cropped/resized frame, never the raw image
    assert [c[1]["first_frame"] for c in p.calls] == [str(f) for f in _frames(p)]
    for f in _frames(p):
        with Image.open(f) as im:
            assert im.size == (512, 288)
    assert all(c[0] == "h3" for c in p.calls)
    assert len({c[1]["seed"] for c in p.calls}) == 3
    plan = json.loads(p.job.plan_json)
    assert len(plan["segments"]) == 3
    for (_, params, _), seg in zip(p.calls, plan["segments"]):
        assert (params["length"] - 5) % 17 == 0
        assert params["length"] >= (seg["end"] - seg["start"]) * planner.FPS
    # only the "sing" clip gets its slice of the song as H3's soundtrack guide
    s1 = plan["segments"][1]
    wav = worker.job_dir(p.base, p.job.id) / "audio" / "clip_001.wav"
    assert p.slices == [(p.song, s1["start"], s1["end"], wav)]
    assert [c[1].get("soundtrack") for c in p.calls] == [None, str(wav), None]
    assert not (wav.parent / "clip_000.wav").exists()
    # per-clip direction overrides the job's text; the job's LoRA applies everywhere, the clip's adds
    prompts = [c[1]["prompt"] for c in p.calls]
    assert prompts[0].startswith("neon city.") and prompts[0].endswith(" <lora:z:1>")
    assert prompts[1] == prompts[0]
    assert prompts[2].startswith("close-up of the drummer.") and prompts[2].endswith(" <lora:z:1> <lora:q:1>")


def test_process_job_portrait_aspect(session, pipeline):
    p = pipeline
    p.job.aspect = "9:16"
    session.commit()
    worker.process_job(session, p.job, p.base, run=p.run, sleep=lambda s: None)
    for f in _frames(p):
        with Image.open(f) as im:
            assert im.size == (288, 512)
    assert {(c[1]["width"], c[1]["height"]) for c in p.calls} == {(288, 512)}


def test_aspect_column_migrates_on_an_old_db(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from audiplex.database import _migrate_music_video_aspect

    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE music_video_jobs (id INTEGER PRIMARY KEY, quality VARCHAR(10))"))
        conn.execute(text("INSERT INTO music_video_jobs (quality) VALUES ('draft')"))
    _migrate_music_video_aspect(eng)
    _migrate_music_video_aspect(eng)  # idempotent
    assert "aspect" in {c["name"] for c in inspect(eng).get_columns("music_video_jobs")}
    with eng.connect() as conn:
        assert conn.execute(text("SELECT aspect FROM music_video_jobs")).scalar() == "16:9"


def test_process_job_resumes_finished_clips(session, pipeline):
    p = pipeline
    done = worker.job_dir(p.base, p.job.id) / "clips" / "clip_000.mp4"
    done.parent.mkdir(parents=True)
    done.write_bytes(b"old")
    worker.process_job(session, p.job, p.base, run=p.run, sleep=lambda s: None)
    assert len(p.calls) == 2
    assert p.calls[0][1]["first_frame"] == str(_frames(p)[1])
    assert not _frames(p)[0].exists()
    assert done.read_bytes() == b"old"
    session.refresh(p.job)
    assert p.job.status == "done"


def test_process_job_old_plain_string_images(session, pipeline):
    """Jobs queued before lip sync hold plain path strings; they still render, no sing."""
    p = pipeline
    p.job.image_paths = json.dumps(p.imgs)
    session.commit()
    worker.process_job(session, p.job, p.base, run=p.run, sleep=lambda s: None)
    session.refresh(p.job)
    assert p.job.status == "done"
    assert len(p.calls) == 3 and p.slices == []
    assert all("soundtrack" not in c[1] for c in p.calls)


def test_clips_normalises():
    assert worker._clips(json.dumps(["a", {"path": "b", "sing": 1}, {"path": "c", "prompt": None}])) == [
        {"path": "a", "sing": False, "prompt": ""}, {"path": "b", "sing": True, "prompt": ""},
        {"path": "c", "sing": False, "prompt": ""}]


@pytest.mark.parametrize("size,expect", [((8, 16), (8, 4)), ((32, 9), (16, 9)), ((16, 9), (16, 9)), ((100, 100), (100, 56))])
def test_crop_to_aspect(size, expect):
    im = worker.crop_to_aspect(Image.new("RGB", size), 16 / 9)
    assert im.size == expect


def test_prepare_frame_crops_centre_no_stretch(tmp_path):
    """Portrait 8x16: top quarter red, middle half green, bottom quarter blue ->
    the 16:9 frame is the green middle, resized to 512x288 (not squashed)."""
    src = Image.new("RGB", (8, 16), (0, 255, 0))
    for y in range(4):
        for x in range(8):
            src.putpixel((x, y), (255, 0, 0))
            src.putpixel((x, 15 - y), (0, 0, 255))
    src.save(tmp_path / "p.png")
    out = worker.prepare_frame(str(tmp_path / "p.png"), 512, 288, tmp_path / "f" / "frame.png")
    with Image.open(out) as im:
        assert im.size == (512, 288)
        assert im.getpixel((256, 144)) == (0, 255, 0)
        assert im.getpixel((256, 2))[1] > 200 and im.getpixel((256, 285))[1] > 200  # no red/blue bands


def test_slice_song_ffmpeg_args(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(worker, "_ffmpeg", lambda *a: seen.append(a))
    out = worker.slice_song("s.mp3", 1.5, 7.25, tmp_path / "a" / "clip.wav")
    assert out == tmp_path / "a" / "clip.wav" and out.parent.is_dir()
    (args,) = seen
    assert args[:4] == ("-ss", "1.500", "-to", "7.250") and "s.mp3" in args and args[-1] == str(out)


def test_render_clip_soundtrack_param(tmp_path, monkeypatch):
    comfy_out = tmp_path / "out"
    (comfy_out / "x").mkdir(parents=True)
    (comfy_out / "x" / "v.mp4").write_bytes(b"v")
    monkeypatch.setattr(worker, "COMFY_OUTPUT", comfy_out)
    seen = []

    def run(name, params, **kw):
        seen.append(params)
        return {"outputs": [{"filename": "v.mp4", "subfolder": "x"}]}

    kw = dict(image="i.png", seconds=5, quality="draft", prompt="p", port=1, prefix="x", run=run)
    worker.render_clip(dest=tmp_path / "c0.mp4", **kw)
    worker.render_clip(dest=tmp_path / "c1.mp4", soundtrack=tmp_path / "a.wav", **kw)
    assert "soundtrack" not in seen[0]
    assert seen[1]["soundtrack"] == str(tmp_path / "a.wav") and seen[1]["first_frame"] == "i.png"


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
    # #6867: song analysis is faked; tests mark a track analyzed via api.cache
    cache, started = {}, []
    monkeypatch.setattr(mv_router.analysis, "cached", lambda path, d: cache.get(str(path)))
    monkeypatch.setattr(mv_router, "_start_analysis", lambda path, d: started.append(path))

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
        c.cache = cache
        c.started = started
        yield c


def _imgs(*names):
    return [{"name": n} for n in names]


def _ready(api, t, duration):
    api.cache[t.file_path] = {"duration": duration, "tempo": 120.0,
                              "beats": _beats(duration), "vocal_spans": []}


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
    assert r.json()["last_folder"] is None
    assert r.json()["last_prompt_template"] == worker.DEFAULT_TEMPLATE
    assert r.json()["default_prompt_template"] == worker.DEFAULT_TEMPLATE
    assert api.get("/api/music-video/estimate/9999", headers=api.h).status_code == 404
    assert api.get(f"/api/music-video/estimate/{t.id}?quality=bogus", headers=api.h).status_code == 400


def test_list_images(api, folder):
    r = api.get("/api/music-video/images", params={"folder": str(folder)}, headers=api.h)
    assert r.status_code == 200
    body = r.json()
    assert body["images"] == [{"name": "a.png", "width": 600, "height": 400},
                              {"name": "b.PNG", "width": 600, "height": 400}]
    assert body["subfolders"] == ["sub"]
    assert api.get("/api/music-video/images", params={"folder": "pics"}, headers=api.h).status_code == 400
    assert api.get("/api/music-video/images", params={"folder": ""}, headers=api.h).status_code == 400
    missing = str(folder / "nope")
    assert api.get("/api/music-video/images", params={"folder": missing}, headers=api.h).status_code == 404


def test_list_images_caches_sizes(api, folder, monkeypatch):
    url, prm = "/api/music-video/images", {"folder": str(folder)}
    api.get(url, params=prm, headers=api.h)
    reads = []
    real = mv_router._read_size
    monkeypatch.setattr(mv_router, "_read_size", lambda p: reads.append(p) or real(p))
    assert api.get(url, params=prm, headers=api.h).json()["images"][0]["width"] == 600
    assert reads == []  # unchanged files come from the cache
    _png(folder / "a.png", (300, 500))
    imgs = api.get(url, params=prm, headers=api.h).json()["images"]
    assert len(reads) == 1 and imgs[0] == {"name": "a.png", "width": 300, "height": 500}


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
    with Image.open(io.BytesIO(r.content)) as im:  # 600x400 source -> the 16:9 crop H3 will get
        assert im.size[1] == 270 and abs(im.size[0] / im.size[1] - 16 / 9) < 0.02


def test_thumb_aspect(api, folder):
    r = api.get("/api/music-video/thumb", headers=api.h,
                params={"folder": str(folder), "name": "a.png", "aspect": "9:16"})
    assert r.status_code == 200
    with Image.open(io.BytesIO(r.content)) as im:
        assert abs(im.size[0] / im.size[1] - 9 / 16) < 0.02
    r = api.get("/api/music-video/thumb", headers=api.h,
                params={"folder": str(folder), "name": "a.png", "aspect": "2:1"})
    assert r.status_code == 400


@pytest.mark.parametrize("name", BAD_NAMES)
def test_create_job_rejects_bad_names(api, session, tmp_path, folder, name):
    t = _make_track(session, tmp_path, 10.0)  # needs 2 images
    body = {"track_id": t.id, "folder": str(folder), "images": _imgs("a.png", name)}
    r = api.post("/api/music-video/jobs", json=body, headers=api.h)
    assert r.status_code in (400, 404), (name, r.status_code)
    assert session.query(MusicVideoJob).count() == 0
    assert api.spawned == []


def test_plan_endpoint_states(api, session, tmp_path):
    t = _make_track(session, tmp_path, 90.0)
    r = api.get(f"/api/music-video/plan/{t.id}", headers=api.h).json()
    assert r["status"] == "analyzing" and r["est_analysis_seconds"] > 0
    assert api.started == [t.file_path]
    api.get(f"/api/music-video/plan/{t.id}", headers=api.h)
    assert api.started == [t.file_path]  # one analysis per song at a time
    _ready(api, t, 90.0)
    r = api.get(f"/api/music-video/plan/{t.id}?quality=final", headers=api.h).json()
    assert r["status"] == "ready"
    assert r["n_images"] == len(r["segments"]) and 6 <= r["n_images"] <= 18
    assert 5 <= r["clip_min"] <= r["clip_max"] <= 15
    assert r["est_render_seconds"] > 0
    assert api.get(f"/api/music-video/plan/{t.id}?quality=bogus", headers=api.h).status_code == 400
    mv_router._analysis_running.discard(t.file_path)


def test_plan_endpoint_failed_and_retry(api, session, tmp_path):
    t = _make_track(session, tmp_path, 30.0)
    mv_router._analysis_failed[t.file_path] = "demucs failed: boom"
    try:
        r = api.get(f"/api/music-video/plan/{t.id}", headers=api.h).json()
        assert r["status"] == "failed" and "boom" in r["detail"]
        r = api.get(f"/api/music-video/plan/{t.id}?retry=1", headers=api.h).json()
        assert r["status"] == "analyzing"
    finally:
        mv_router._analysis_failed.pop(t.file_path, None)
        mv_router._analysis_running.discard(t.file_path)


def test_create_job_needs_analysis(api, session, tmp_path, folder):
    t = _make_track(session, tmp_path, 16.0)
    r = api.post("/api/music-video/jobs", headers=api.h,
                 json={"track_id": t.id, "folder": str(folder), "images": _imgs("a.png", "b.PNG")})
    assert r.status_code == 409
    assert session.query(MusicVideoJob).count() == 0


def test_create_job_wrong_count_and_long_direction(api, session, tmp_path, folder):
    t = _make_track(session, tmp_path, 16.0)
    _ready(api, t, 16.0)  # plans to 2 clips
    for imgs in (_imgs("a.png"), _imgs("a.png", "b.PNG", "a.png")):
        r = api.post("/api/music-video/jobs", headers=api.h,
                     json={"track_id": t.id, "folder": str(folder), "images": imgs})
        assert r.status_code == 400
    r = api.post("/api/music-video/jobs", headers=api.h, json={
        "track_id": t.id, "folder": str(folder), "images": _imgs("a.png", "b.PNG"), "direction": "x" * 1001})
    assert r.status_code == 400
    r = api.post("/api/music-video/jobs", headers=api.h, json={
        "track_id": t.id, "folder": str(folder), "images": _imgs("a.png", "b.PNG"),
        "prompt_template": "x" * 2001})
    assert r.status_code == 400
    r = api.post("/api/music-video/jobs", headers=api.h, json={  # per-clip direction has the same cap
        "track_id": t.id, "folder": str(folder),
        "images": [{"name": "a.png", "prompt": "x" * 1001}, {"name": "b.PNG"}]})
    assert r.status_code == 400
    r = api.post("/api/music-video/jobs", headers=api.h, json={  # the old plain-string form is gone
        "track_id": t.id, "folder": str(folder), "images": ["a.png", "b.PNG"]})
    assert r.status_code == 422
    assert session.query(MusicVideoJob).count() == 0


def test_create_job_valid(api, session, tmp_path, folder):
    t = _make_track(session, tmp_path, 16.0)
    _ready(api, t, 16.0)
    r = api.post("/api/music-video/jobs", headers=api.h, json={
        "track_id": t.id, "folder": str(folder), "direction": "  moody  ",
        "images": [{"name": "a.png", "sing": True}, {"name": "b.PNG", "prompt": " the band bows "}],
        "prompt_template": " {direction} in slow motion "})
    assert r.status_code == 201
    assert r.json()["status"] == "queued" and r.json()["clips_total"] == 2
    assert r.json()["sing_count"] == 1
    assert len(api.spawned) == 1
    job = session.query(MusicVideoJob).one()
    assert job.direction == "moody"
    assert job.prompt_template == "{direction} in slow motion"
    plan = json.loads(job.plan_json)  # the worker renders exactly what Todd was shown
    assert [round(s["end"] - s["start"]) for s in plan["segments"]] == [8, 8]
    clips = json.loads(job.image_paths)
    assert [(c["sing"], c["prompt"]) for c in clips] == [(True, ""), (False, "the band bows")]
    paths = [c["path"] for c in clips]
    root = str(folder.resolve())
    assert job.image_folder == root
    assert all(p.startswith(root) and "sibling" not in p for p in paths)
    assert api.get("/api/music-video/jobs", headers=api.h).json()[0]["sing_count"] == 1
    est = api.get(f"/api/music-video/estimate/{t.id}", headers=api.h).json()
    assert est["last_folder"] == root  # remembered from his last job, never a default
    assert est["last_prompt_template"] == "{direction} in slow motion"
    assert est["aspects"] == list(planner.ASPECTS) and est["last_aspect"] == "16:9"


def test_create_job_aspect(api, session, tmp_path, folder):
    t = _make_track(session, tmp_path, 16.0)
    _ready(api, t, 16.0)
    body = {"track_id": t.id, "folder": str(folder), "images": _imgs("a.png", "b.PNG"), "aspect": "2:1"}
    assert api.post("/api/music-video/jobs", headers=api.h, json=body).status_code == 400
    body["aspect"] = "9:16"
    r = api.post("/api/music-video/jobs", headers=api.h, json=body)
    assert r.status_code == 201 and r.json()["aspect"] == "9:16"
    assert session.query(MusicVideoJob).one().aspect == "9:16"
    assert api.get(f"/api/music-video/estimate/{t.id}", headers=api.h).json()["last_aspect"] == "9:16"


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
