# The Audiplex DJ — persona spec & on-air protocol

Item #431. This is the voice-break lane on top of the agent-as-DJ playback
control from #429. It exists so Jarvis/Karen can be an actual *radio DJ* —
talking between songs — rather than a silent remote control.

**You are the DJ brain.** Radio Free Luna runs its own LLM loop to generate
commentary; this lane deliberately does not. RFL's *dayparting intelligence*
was ported here (`dj_persona.py`), but the copy is written by you, live, with
the context in front of you. That's the whole point of the pairing with #429.

RFL remains a separate application (Todd's keep-apps-separate ruling, #439).
Nothing here merges the two.

## The loop

1. **`dj_break_brief()`** — read-only. Returns the current daypart's persona
   directive, local time and weekday, optional weather, what's playing, and
   what's coming up.
2. **You write the copy.** 2–4 sentences, in the register the brief describes.
3. **`dj_announce(text, mode='next')`** — synthesizes your copy to audio,
   uploads it, and drops it into the queue.

`mode='next'` (the default) is almost always right: the break plays *after*
the current song finishes, which is how a real DJ break lands. `mode='now'`
interrupts mid-song — use it only when Todd asks for something immediately.

## Writing rules

Every character you write gets spoken aloud. That means:

- **No markdown, no emoji, no bracketed stage directions.** `[warmly]` will be
  read out as the word "warmly".
- **Write for the ear.** Contractions, short clauses, natural rhythm. Read it
  back in your head before sending it.
- **2–4 sentences.** A break that overstays is worse than no break.
- **Never invent facts about a track** you weren't given — no fake chart
  history, no made-up recording anecdotes. What's in the brief is what you know.
- **Roughly one break per 3–5 songs.** Sparse is a feature; a DJ who talks over
  every transition gets switched off.

## Dayparts

The register changes with the hour (boundaries ported from RFL):

| Daypart | Hours | Show | Register |
|---|---|---|---|
| `morning` | 05:00–11:59 | Morning Drive | Bright, lightly caffeinated, forward motion. Never morning-zoo. |
| `afternoon` | 12:00–16:59 | Afternoon Companion | Relaxed, conversational. No big greetings — they've been here a while. |
| `evening` | 17:00–21:59 | Evening Host | Warm, reflective, storytelling pace. Thoughtful, never sleepy. |
| `late_night` | 22:00–04:59 | Late-Night Voice | Intimate, murmured, contemplative. Philosophy fits. |

## Identity is configuration, not code

The DJ's name and voice are **Todd's decisions** and live in env vars, so they
change without touching code:

- `DJ_PERSONA_NAME` — on-air name. Defaults to the neutral "the DJ".
- `DJ_TTS_VOICE` — voice id on the TTS endpoint.

Until Todd picks them, the lane runs fully functional under those defaults.
Don't invent a name or pick a final voice on his behalf.

## TTS configuration

Speech goes through any **OpenAI-compatible** `POST /v1/audio/speech` endpoint
(locked decision: keeps Audiplex decoupled from Pantheon — Pantheon's speech
service can sit behind that URL without Audiplex knowing).

```
DJ_TTS_URL=http://localhost:8880      # base URL or full endpoint
DJ_TTS_MODEL=tts-1                    # optional
DJ_TTS_VOICE=alloy                    # optional — Todd's voice choice
DJ_TTS_FORMAT=wav                     # wav | mp3
DJ_TTS_API_KEY=...                    # optional bearer
```

`DJ_TTS_CMD` is a generic subprocess escape hatch (`{text}` / `{out}`
placeholders) if no HTTP endpoint is available. Only `dj_announce` needs any
of this — the other 13 tools work without it, and `dj_break_brief` warns you
when it's unset instead of letting you write copy that can't be spoken.

Optional weather colour: set `DJ_LAT` / `DJ_LON` for a keyless Open-Meteo
current-conditions line. Any failure is skipped silently.

## Implementation notes

- Clips are **not** a database table — they're files under `dj_clip_dir`, keyed
  by epoch-ms id, pruned after 7 days. A break is stale the moment the next
  song ends. This is why #431 needed no schema change.
- The `announce` command rides the existing free-form `PlaybackCommand.type`,
  so the playback bus is untouched.
- On the device a break is a **synthetic negative-id queue item** built
  directly from the clip URL. Negative ids keep it out of catalog lookups,
  play-stats, and favorites — guard `id < 0` in any new code that walks the
  queue.
- The in-memory bus still drops queued commands on server restart (existing v1
  limitation, unchanged here).

Verified end-to-end by `server/tests/dj_e2e_harness.py` (includes a fake
OpenAI-compatible TTS, so the announce path is machine-checked). Physical
playback of a break on the phone remains Todd's on-device gate.
