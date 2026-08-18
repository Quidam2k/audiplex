"""Deriving a real artist and title from a clumsily named file (#3037 / #953).

Todd's library is a flat dump of YouTube rips. The files are not untagged —
they are tagged with the WRONG THING. `artist` holds the uploading channel,
not the performer:

    ©nam = All You Need Is Love (Remastered 2009)
    ©ART = The Beatles - Topic            <- a channel
    ©cmt = https://www.youtube.com/watch?v=_7xMfIp-irg

So there are two independent streams of evidence about who made a track: the
CHANNEL and the "Artist - Title" convention inside the video title. This module
weighs them and reports a confidence, because a wrong artist is worse than a
blank one — identity keys derive from normalized(title, artist), so a bad guess
silently poisons ratings and cooldown, while a blank one merely limits them.

WHY THE TITLE OUTRANKS THE CHANNEL
----------------------------------
Not a preference — the live library forced it. The three verified-correct
work-level merges from #9265 each pair two rips whose channels DISAGREE or are
wrong, and whose title-parsed artist is identical:

    bury a friend      SyrebralVibes  vs BillieEilishVEVO -> "Billie Eilish"
    We Know The Way    DisneyMusicVEVO both, which yields "DisneyMusic" — WRONG
    Concrete Wall      LavelaL        vs Proximity Chill  -> "Zee Avi"

Let the channel win and two of those three merges break outright and the third
gets an artist that does not exist. Hence: dash-parse first, channel second.

WHAT IS NEVER STRIPPED
----------------------
Promotional noise ("Official Video", a bitrate tail) is junk and goes. VERSION
markers ("Live", "Remaster", "Remix", "Acoustic") are MEANING and stay — they
are exactly what identity.py uses to keep a live cut from inheriting the studio
take's rating. Stripping them here would quietly undo #943.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Auto-apply only. Everything below this is recorded and flagged, never written
# to a track — see the module docstring on why a bad guess costs more than none.
HIGH = "high"
MEDIUM = "medium"
LOW = "low"
UNRESOLVED = "unresolved"

APPLIED = "applied"
PENDING_REVIEW = "pending_review"
REJECTED = "rejected"

SOURCE_PARSER = "parser"
SOURCE_CONSENSUS = "consensus"
SOURCE_RFL = "rfl"
SOURCE_MANUAL = "manual"

# "01 - Pseudo Silk Kimono" is a track number and a title. Applied ONLY to
# titles derived from a filename, and only once the dash split has already been
# tried and failed — otherwise it would shorten "4 Non Blondes - What's Up",
# which is the same digit-eating bug the dry run caught once already.
_FILENAME_INDEX = re.compile(r"^[\s\-_.]*\d{1,3}[\s\-_.]+(?=\S)")

# The ripper cannot put these characters in a filename, so it substitutes
# look-alikes. Tags keep the real character; only filenames need undoing, which
# is why _demangle is applied to stems and never to tag values.
_FILENAME_SUBSTITUTIONS = {
    "¿": "?",   # ¿  <- ?
    "⁄": "/",   # ⁄  <- /
    "¦": "|",   # ¦  <- |
}
# ':' becomes '_', but only the ": " form is safe to undo — a bare underscore
# is an ordinary filename character and "Nine_Inch_Nails" must survive.
_UNDERSCORE_COLON = re.compile(r"_(?=\s)")

# Every file carries an encoder tail: "(128kbit_AAC)", "(152kbit_Opus)".
_BITRATE_TAIL = re.compile(r"\s*[\(\[]\s*\d+\s*kbit[_\s]*\w+\s*[\)\]]\s*$", re.IGNORECASE)

# Promotional noise only. Deliberately NOT here: live, remaster, remix, acoustic,
# demo, unplugged, instrumental, edit, version, mono, stereo, reprise, session.
# Those distinguish one recording from another and identity.py depends on them.
_PROMO_WORDS = (
    r"official\s+music\s+video",
    r"official\s+lyric\s+video",
    r"official\s+video",
    r"official\s+audio",
    r"official\s+visualizer",
    r"music\s+video",
    r"lyric\s+video",
    r"lyrics?",
    r"visualizer",
    r"official",
    r"free",
    r"hd",
    r"hq",
    r"4k",
    r"full\s+hd",
    r"audio",
)
_PROMO_BRACKET = re.compile(
    r"\s*[\(\[]\s*(?:" + "|".join(_PROMO_WORDS) + r")\s*[\)\]]",
    re.IGNORECASE,
)

# Trailing "| Riot Games Music | Netflix" segments. Only trailing ones, and only
# while they keep matching — the leading segment is the actual title and is
# never touched, so 'Arcane: Season 2 | "Paint The Town Blue"' keeps its quote.
_TRAILING_PIPE = re.compile(r"\s*\|\s*[^|]{1,40}$")
_PIPE_JUNK = re.compile(
    r"^(?:"
    + "|".join(_PROMO_WORDS)
    + r"|netflix|riot\s+games(?:\s+music)?|vevo|topic|.*\brecords\b|.*\bmusic\b"
    + r")$",
    re.IGNORECASE,
)

# " - ", " – ", " — ". Requires the spaces: "Jay-Z" and "Wham!-era" are not
# separators, and a hyphen with no space around it is part of a name far more
# often than it is a divider.
_DASH_SPLIT = re.compile(r"\s+[-–—]\s+")

_TOPIC_SUFFIX = re.compile(r"\s*-\s*Topic\s*$", re.IGNORECASE)
_VEVO_SUFFIX = re.compile(r"VEVO\s*$")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

_YOUTUBE_URL = re.compile(r"(?:youtube\.com|youtu\.be)/", re.IGNORECASE)

# An artist name this long is a sentence, and a title split on its first dash
# would leave nothing. Both bounds are about rejecting a bad split, not about
# policing real names.
_MIN_ARTIST_LEN = 2
_MAX_ARTIST_LEN = 60

# "01. " / "03 - " is a track number. A bare leading digit is NOT: dropping it
# turned "4 Non Blondes" into "Non Blondes", so the punctuation is required.
_LEADING_TRACK_NUM = re.compile(r"^\s*\d{1,3}\s*[-.\)]\s+")

# A performer credit longer than this is usually a sentence that got split in
# the wrong place — "Cups Pitch Perfect's When I'm Gone - Anna Kendrick" is
# title-then-artist, the reverse of the convention. Five words still admits
# real credits like "Joanne Shenandoah, A. Paul Ortega".
_MAX_ARTIST_WORDS = 5


def is_youtube_rip(comment: str | None) -> bool:
    """True when the file carries a YouTube URL in its comment tag.

    This is the flag that decides whether the `artist` tag means "performer" or
    "uploading channel". Evidence from the file itself, not a guess from where
    the file happens to sit — a properly tagged album dropped into the flat
    dump still gets its real tags trusted.
    """
    return bool(comment and _YOUTUBE_URL.search(comment))


def demangle(stem: str) -> str:
    """Undo the ripper's filename-safe character substitutions."""
    out = stem
    for bad, good in _FILENAME_SUBSTITUTIONS.items():
        out = out.replace(bad, good)
    return _UNDERSCORE_COLON.sub(":", out)


def strip_junk(text: str | None) -> str:
    """Drop promotional noise. Version markers survive — see module docstring."""
    if not text:
        return ""
    out = _BITRATE_TAIL.sub("", text).strip()

    previous = None
    while previous != out:  # "(Lyrics) (Official Video)" needs more than one pass
        previous = out
        out = _PROMO_BRACKET.sub("", out).strip()
        out = _BITRATE_TAIL.sub("", out).strip()

    previous = None
    while previous != out:
        previous = out
        match = _TRAILING_PIPE.search(out)
        if match and _PIPE_JUNK.match(match.group(0).lstrip(" |").strip()):
            out = out[: match.start()].strip()

    return out.strip(" -–—|").strip()


def split_artist_title(text: str) -> tuple[str, str] | None:
    """"Billie Eilish - bury a friend" -> ("Billie Eilish", "bury a friend").

    Splits on the FIRST spaced dash. A title with its own dash keeps it:
    "Zee Avi - Concrete Wall - Live" yields the artist and "Concrete Wall -
    Live", which is what identity.py wants to see.
    """
    if not text:
        return None
    parts = _DASH_SPLIT.split(text, maxsplit=1)
    if len(parts) != 2:
        return None
    artist, title = parts[0].strip(), parts[1].strip()
    artist = _LEADING_TRACK_NUM.sub("", artist).strip()
    if not (_MIN_ARTIST_LEN <= len(artist) <= _MAX_ARTIST_LEN):
        return None
    if not title:
        return None
    if artist.isdigit():
        return None
    return artist, title


@dataclass(frozen=True)
class ChannelReading:
    """What the uploader channel claims, and how much that claim is worth."""

    artist: str
    kind: str  # topic | vevo | plain


def read_channel(artist_tag: str | None) -> ChannelReading | None:
    """Interpret a YouTube channel name as an artist claim.

    "The Beatles - Topic" is YouTube's own auto-generated artist channel and is
    the best claim available. "BillieEilishVEVO" is usually right but not
    always — "DisneyMusicVEVO" is a label channel, and believing it would file
    Moana under an artist called DisneyMusic. Anything else is a person's
    account name and worth very little on its own.
    """
    if not artist_tag:
        return None
    tag = artist_tag.strip()
    if not tag:
        return None

    if _TOPIC_SUFFIX.search(tag):
        name = _TOPIC_SUFFIX.sub("", tag).strip()
        return ChannelReading(name, "topic") if name else None

    if _VEVO_SUFFIX.search(tag):
        name = _VEVO_SUFFIX.sub("", tag).strip()
        if not name:
            return None
        return ChannelReading(_CAMEL_BOUNDARY.sub(" ", name).strip(), "vevo")

    return ChannelReading(tag, "plain")


def _comparable(name: str) -> str:
    """Fold for agreement testing only. Whitespace goes too, so the run-together
    "BillieEilish" a VEVO channel yields matches the spaced "Billie Eilish" a
    title yields — the whole reason to compare them at all."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def channels_agree(channel: ChannelReading | None, artist: str) -> bool:
    """Do the channel and the title name the same act?

    Prefix, not substring. "BillieEilishVEVO" and "Billie Eilish" fold to the
    same thing, and "AshnikkoOfficial" starts with "Ashnikko" — but a channel
    buried in the MIDDLE of a parsed artist is the opposite of corroboration.
    "Tumbledown House" sits inside "Ode to Josephine By Tumbledown House"
    because the split landed in the wrong place; see channel_buried_in.
    """
    if channel is None or not artist:
        return False
    a, b = _comparable(channel.artist), _comparable(artist)
    if not a or not b:
        return False
    return a.startswith(b) or b.startswith(a)


def channel_buried_in(channel: ChannelReading | None, artist: str) -> bool:
    """True when the channel appears inside the parsed artist but not at its
    start — the signature of a dash split in the wrong place."""
    if channel is None or not artist:
        return False
    a, b = _comparable(channel.artist), _comparable(artist)
    if not a or not b or a == b:
        return False
    return a in b and not b.startswith(a)


def _over_long(artist: str) -> bool:
    return len(artist.split()) > _MAX_ARTIST_WORDS


@dataclass(frozen=True)
class Proposal:
    """What this file should be called, and the case for it.

    `evidence` is stored alongside the proposal on purpose. A repair row that
    says only "artist = Zee Avi" is unreviewable; one that says the title and
    two disagreeing channels were weighed can be argued with.
    """

    artist: str | None
    title: str | None
    confidence: str
    evidence: str

    @property
    def auto_applicable(self) -> bool:
        return self.confidence == HIGH and bool(self.artist) and bool(self.title)


def propose(
    *,
    title_tag: str | None,
    artist_tag: str | None,
    filename_stem: str | None = None,
    comment: str | None = None,
) -> Proposal:
    """Weigh every signal on one file and propose an artist and title.

    The ladder, strongest first:

      1. title parses as "Artist - Title" AND the channel agrees   -> high
      2. title parses as "Artist - Title"                          -> high
      3. channel is an auto-generated "- Topic" artist channel     -> high
      4. channel is a VEVO channel                                 -> medium
      5. channel is somebody's account name                        -> low
      6. nothing usable                                            -> unresolved

    Only `high` is ever written automatically. Everything else is recorded for
    review with whatever partial answer it has, so the residual is a worklist
    rather than a silent gap.
    """
    is_rip = is_youtube_rip(comment)
    channel = read_channel(artist_tag)

    raw_title = (title_tag or "").strip()
    from_filename = False
    if not raw_title and filename_stem:
        raw_title = demangle(filename_stem)
        from_filename = True
    cleaned = strip_junk(raw_title)

    # A non-rip's artist tag is a real artist tag. Nothing to weigh: believe it.
    if not is_rip and channel is not None and channel.kind == "plain":
        return Proposal(
            artist=channel.artist,
            title=cleaned or raw_title or None,
            confidence=HIGH,
            evidence=(
                "artist tag trusted verbatim: no YouTube URL in the comment "
                "tag, so this is not a rip and the tag means the performer"
            ),
        )

    split = split_artist_title(cleaned)

    if split is None and from_filename:
        # No artist hides in this name, so a leading number is a track index
        # rather than part of one. Safe to drop only now that the split has
        # been tried and refused.
        cleaned = _FILENAME_INDEX.sub("", cleaned).strip() or cleaned

    if split:
        artist, title = split
        title = strip_junk(title) or title
        if channels_agree(channel, artist):
            return Proposal(
                artist=artist,
                title=title,
                confidence=HIGH,
                evidence=(
                    f"title parsed as artist-dash-title and the {channel.kind} "
                    f"channel {channel.artist!r} agrees"
                ),
            )
        if channels_agree(channel, title) and not channels_agree(channel, artist):
            # "I LOVE PARIS (Cole Porter) – Tatiana Eva-Marie & Avalon Jazz
            # Band" is title-then-artist. Two signals say so: the channel is
            # the half AFTER the dash, and it is not the half before it. That
            # is corroboration, not a guess, so the halves swap.
            return Proposal(
                artist=title,
                title=artist,
                confidence=HIGH,
                evidence=(
                    f"title is written title-dash-artist: the {channel.kind} "
                    f"channel {channel.artist!r} matches the half after the "
                    "dash, not the half before it, so the two are swapped"
                ),
            )

        if channel_buried_in(channel, artist):
            return Proposal(
                artist=artist,
                title=title,
                confidence=LOW,
                evidence=(
                    f"title parsed as artist-dash-title, but the channel "
                    f"{channel.artist!r} sits INSIDE the parsed artist rather "
                    "than matching it — the dash almost certainly split in the "
                    "wrong place, so this needs a human"
                ),
            )

        if _over_long(artist):
            return Proposal(
                artist=artist,
                title=title,
                confidence=LOW,
                evidence=(
                    f"title parsed as artist-dash-title, but {artist!r} is too "
                    "many words to be a performer credit and no channel "
                    "corroborates it — likely title-then-artist, the reverse "
                    "of the convention"
                ),
            )

        detail = (
            f"; channel {channel.artist!r} ({channel.kind}) disagrees and loses "
            "— see #3037 on why the title outranks it"
            if channel
            else "; no channel to corroborate"
        )
        return Proposal(
            artist=artist,
            title=title,
            confidence=HIGH,
            evidence=f"title parsed as artist-dash-title{detail}",
        )

    if channel and channel.kind == "topic":
        return Proposal(
            artist=channel.artist,
            title=cleaned or None,
            confidence=HIGH,
            evidence=(
                "no dash in the title; channel is YouTube's auto-generated "
                f"'- Topic' artist channel for {channel.artist!r}"
            ),
        )

    if channel and channel.kind == "vevo":
        return Proposal(
            artist=channel.artist,
            title=cleaned or None,
            confidence=MEDIUM,
            evidence=(
                "no dash in the title; only a VEVO channel to go on, which is "
                "often the label rather than the artist (DisneyMusicVEVO)"
            ),
        )

    if channel:
        return Proposal(
            artist=channel.artist,
            title=cleaned or None,
            confidence=LOW,
            evidence=(
                f"no dash in the title; only the uploader account "
                f"{channel.artist!r}, which need not be the performer"
            ),
        )

    return Proposal(
        artist=None,
        title=cleaned or None,
        confidence=UNRESOLVED,
        evidence="no artist-dash-title in the title and no channel tag at all",
    )
