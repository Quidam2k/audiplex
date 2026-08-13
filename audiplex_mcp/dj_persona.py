"""Dayparted DJ persona + on-air context (item #431).

The DAYPART_PERSONAS table and the hour boundaries are ported from Radio Free
Luna (src/dj/commentary_generator.py, src/context/temporal.py) so the two
projects share a voice without sharing code — RFL stays a standalone app per
Todd's keep-apps-separate ruling (#439).

The important difference from RFL: RFL runs its own LLM loop to generate
commentary. Here **the agent IS the DJ brain** (#429). This module hands the
agent a brief — what daypart it is, what's playing, what the weather's doing —
and the agent writes the copy itself. Nothing here generates prose.

Persona identity is deliberately NOT hardcoded: the show name comes from
DJ_PERSONA_NAME and the voice from DJ_TTS_VOICE, both with neutral defaults,
so Todd's naming/voice decision drops in via config without a code change.

Config:
  DJ_PERSONA_NAME  the DJ's on-air name (default "the DJ")
  DJ_LAT / DJ_LON  optional coordinates for a keyless Open-Meteo weather line
"""

from __future__ import annotations

import datetime
import os

import httpx

# Hour boundaries ported verbatim from RFL's TemporalContext.get_time_of_day.
_DAYPART_BOUNDS = [(5, 12, "morning"), (12, 17, "afternoon"), (17, 22, "evening")]

DAYPART_PERSONAS: dict[str, dict[str, str]] = {
    "morning": {
        "name": "Morning Drive",
        "directive": (
            "It's the morning show. Write bright, welcoming, lightly caffeinated copy: "
            "shorter sentences, forward motion, a sense of the day opening up. "
            "Greet listeners starting their day — coffee, commutes, sunrise, plans. "
            "Energy is up, but warm and human, never zany morning-zoo shtick."
        ),
    },
    "afternoon": {
        "name": "Afternoon Companion",
        "directive": (
            "It's mid-afternoon. Write relaxed, conversational copy — a steady companion "
            "through the middle of the day. Unhurried but engaged, with gentle observations "
            "about the day in progress. No big greetings; the listener has been here a while."
        ),
    },
    "evening": {
        "name": "Evening Host",
        "directive": (
            "It's evening. Write warm, reflective copy at a storytelling pace — the day is "
            "winding down and listeners are settling in. Golden-hour imagery, a sense of "
            "homecoming, invitations to slow down. Thoughtful, never sleepy."
        ),
    },
    "late_night": {
        "name": "Late-Night Voice",
        "directive": (
            "It's late at night. Write intimate, murmured, contemplative copy for night owls, "
            "insomniacs, and long-haul drivers. Long thoughts welcome; sentences can drift. "
            "Speak softly, like the only voice on the dial. Philosophy fits here."
        ),
    },
}

# House style, independent of daypart. Kept here (not in the agent's head) so
# every break sounds like the same show.
STYLE_RULES = (
    "2-4 sentences, spoken aloud — write for the ear, not the page. "
    "No stage directions, no emoji, no markdown, no bracketed cues: every "
    "character you write gets synthesized into speech. Never invent facts about "
    "a track you weren't given. Roughly one break per 3-5 songs."
)


def persona_name() -> str:
    return os.environ.get("DJ_PERSONA_NAME") or "the DJ"


def time_of_day(now: datetime.datetime | None = None) -> str:
    hour = (now or datetime.datetime.now()).hour
    for start, end, label in _DAYPART_BOUNDS:
        if start <= hour < end:
            return label
    return "late_night"


def daypart(now: datetime.datetime | None = None) -> dict[str, str]:
    return DAYPART_PERSONAS[time_of_day(now)]


_WEATHER_CODES = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "freezing fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
    95: "thunderstorms", 96: "thunderstorms with hail",
}


async def weather_line() -> str | None:
    """One-line current conditions from keyless Open-Meteo, or None.

    Entirely optional colour for the copy — any failure (no coordinates, no
    network, changed API) returns None silently rather than blocking a break.
    """
    lat, lon = os.environ.get("DJ_LAT"), os.environ.get("DJ_LON")
    if not lat or not lon:
        return None
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,weather_code",
                    "temperature_unit": "fahrenheit",
                },
            )
        cur = resp.json().get("current") or {}
        temp = cur.get("temperature_2m")
        desc = _WEATHER_CODES.get(cur.get("weather_code"))
        if temp is None and not desc:
            return None
        bits = [b for b in (desc, f"{round(temp)}F" if temp is not None else None) if b]
        return ", ".join(bits)
    except Exception:
        return None
