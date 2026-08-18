"""Tests for the tag-repair ladder (#3037).

The cases with real names in them are taken from Todd's live library, not
invented — particularly the three that decide precedence between the video
title and the uploading channel.
"""

import pytest

from audiplex.tag_repair import (
    HIGH,
    LOW,
    MEDIUM,
    UNRESOLVED,
    channels_agree,
    demangle,
    is_youtube_rip,
    propose,
    read_channel,
    split_artist_title,
    strip_junk,
)

YT = "https://www.youtube.com/watch?v=HUHC9tYz8ik"


class TestRipDetection:
    def test_youtube_url_marks_a_rip(self):
        assert is_youtube_rip(YT)
        assert is_youtube_rip("https://youtu.be/abc123")

    def test_no_url_is_not_a_rip(self):
        assert not is_youtube_rip(None)
        assert not is_youtube_rip("")
        assert not is_youtube_rip("Ripped from CD")


class TestDemangle:
    def test_restores_ripper_substitutions(self):
        assert demangle("01 - Are You Alive¿ ⁄ Battlestar") == "01 - Are You Alive? / Battlestar"
        assert demangle("Arcane; Season 2 ¦ Music Video") == "Arcane; Season 2 | Music Video"

    def test_underscore_before_space_was_a_colon(self):
        assert demangle("ADHD Relief Music_ Poly-rhythmic") == "ADHD Relief Music: Poly-rhythmic"

    def test_bare_underscore_is_left_alone(self):
        # An ordinary filename character. "Nine_Inch_Nails" is not "Nine:Inch:Nails".
        assert demangle("Nine_Inch_Nails") == "Nine_Inch_Nails"


class TestStripJunk:
    def test_drops_bitrate_tail(self):
        assert strip_junk("Alegria (128kbit_AAC)") == "Alegria"
        assert strip_junk("Daisy (152kbit_Opus)") == "Daisy"

    def test_drops_promotional_brackets(self):
        assert strip_junk("bury a friend (Official Music Video)") == "bury a friend"
        assert strip_junk("bury a friend (Lyrics)") == "bury a friend"
        assert strip_junk("Pallid Eyes [OFFICIAL AUDIO]") == "Pallid Eyes"

    def test_drops_stacked_promotional_brackets(self):
        assert strip_junk("Word Up (Relaid Audio) (Official Music Video)") == "Word Up (Relaid Audio)"

    def test_drops_free_so_duplicate_rips_can_meet(self):
        # The Concrete Wall pair differs only by this tag. Stripping it is what
        # lets two rips of one recording pool their play history.
        assert strip_junk("Concrete Wall (Pumpkin Remix) [Free]") == "Concrete Wall (Pumpkin Remix)"

    @pytest.mark.parametrize(
        "title",
        [
            "Barracuda (2004 Remaster)",
            "Hayling (Max Cooper Remix)",
            "Layla (Acoustic)",
            "Rasputin (Sopot Festival 1979)",
            "Song For Olabi (Live)",
            "Wish You Were Here (Instrumental)",
        ],
    )
    def test_version_markers_are_never_stripped(self, title):
        """These distinguish one recording from another and identity.py depends
        on them. Stripping them would let a live cut inherit the studio take's
        rating, which is the loss #943 exists to prevent."""
        assert strip_junk(title) == title

    def test_drops_trailing_channel_pipes_but_keeps_the_title(self):
        assert (
            strip_junk('Arcane: Season 2 | "Paint The Town Blue" | Music Video | Netflix')
            == 'Arcane: Season 2 | "Paint The Town Blue"'
        )

    def test_leading_pipe_segment_is_never_touched(self):
        assert strip_junk("Playground | Arcane League of Legends | Riot Games Music").startswith(
            "Playground"
        )


class TestSplit:
    def test_splits_on_first_spaced_dash(self):
        assert split_artist_title("Billie Eilish - bury a friend") == (
            "Billie Eilish",
            "bury a friend",
        )

    def test_title_keeps_its_own_later_dashes(self):
        assert split_artist_title("Zee Avi - Concrete Wall - Live") == (
            "Zee Avi",
            "Concrete Wall - Live",
        )

    def test_hyphen_without_spaces_is_not_a_separator(self):
        assert split_artist_title("Jay-Z Song") is None

    def test_rejects_a_numeric_artist(self):
        assert split_artist_title("01 - Blitzkrieg Bop") is None

    def test_no_dash_at_all(self):
        assert split_artist_title("Baba O'Riley") is None


class TestChannel:
    def test_topic_channel_is_the_artist(self):
        reading = read_channel("The Beatles - Topic")
        assert (reading.artist, reading.kind) == ("The Beatles", "topic")

    def test_vevo_channel_is_decamelcased(self):
        reading = read_channel("BillieEilishVEVO")
        assert (reading.artist, reading.kind) == ("Billie Eilish", "vevo")

    def test_anything_else_is_a_plain_account(self):
        assert read_channel("SyrebralVibes").kind == "plain"

    def test_blank_is_nothing(self):
        assert read_channel("") is None
        assert read_channel(None) is None

    def test_agreement_ignores_spacing_and_case(self):
        assert channels_agree(read_channel("BillieEilishVEVO"), "Billie Eilish")
        assert not channels_agree(read_channel("SyrebralVibes"), "Billie Eilish")


class TestLadder:
    def test_title_and_channel_agreeing_is_high(self):
        result = propose(
            title_tag="Ashnikko - Special (Official Video)",
            artist_tag="Ashnikko",
            comment=YT,
        )
        assert (result.artist, result.title, result.confidence) == (
            "Ashnikko",
            "Special",
            HIGH,
        )
        assert result.auto_applicable

    def test_title_alone_is_high_and_beats_a_disagreeing_channel(self):
        result = propose(
            title_tag="Billie Eilish - bury a friend (Lyrics)",
            artist_tag="SyrebralVibes",
            comment=YT,
        )
        assert (result.artist, result.title) == ("Billie Eilish", "bury a friend")
        assert result.confidence == HIGH
        assert "SyrebralVibes" in result.evidence  # the losing claim is recorded

    def test_title_beats_a_label_vevo_channel(self):
        """DisneyMusicVEVO is the case that settles the precedence rule: trust
        the channel and Moana is filed under an artist called "DisneyMusic"."""
        result = propose(
            title_tag='Lin-Manuel Miranda, Opetaia Foa\'i - We Know The Way (From "Moana")',
            artist_tag="DisneyMusicVEVO",
            comment=YT,
        )
        assert result.artist == "Lin-Manuel Miranda, Opetaia Foa'i"
        assert result.confidence == HIGH

    def test_topic_channel_alone_is_high(self):
        result = propose(
            title_tag="All You Need Is Love (Remastered 2009)",
            artist_tag="The Beatles - Topic",
            comment=YT,
        )
        assert (result.artist, result.confidence) == ("The Beatles", HIGH)
        # The version marker survives — it is the difference between two takes.
        assert result.title == "All You Need Is Love (Remastered 2009)"

    def test_vevo_alone_is_only_medium_and_is_not_auto_applied(self):
        result = propose(title_tag="Some Song", artist_tag="DisneyMusicVEVO", comment=YT)
        assert result.confidence == MEDIUM
        assert not result.auto_applicable

    def test_plain_channel_alone_is_low_and_is_not_auto_applied(self):
        result = propose(title_tag="Barracuda", artist_tag="Heart", comment=YT)
        assert (result.artist, result.confidence) == ("Heart", LOW)
        assert not result.auto_applicable

    def test_podcast_lands_in_the_review_bucket_with_nothing_invented(self):
        """Ruling on Q1: flag it, name no performer that isn't on the tin."""
        result = propose(
            title_tag="Ariel Ekblaw: Space Colonization | Lex Fridman Podcast #271",
            artist_tag="Lex Fridman",
            comment=YT,
        )
        assert result.confidence == LOW
        assert not result.auto_applicable
        assert result.artist == "Lex Fridman"

    def test_nothing_usable_is_unresolved(self):
        result = propose(title_tag="Coyote Dance", artist_tag=None, comment=YT)
        assert result.confidence == UNRESOLVED
        assert result.artist is None
        assert result.title == "Coyote Dance"

    def test_untagged_file_falls_back_to_its_filename(self):
        """The 13 Opus rips carry no tags whatsoever — the name is all there is."""
        result = propose(
            title_tag=None,
            artist_tag=None,
            filename_stem="Emily Jane White - Pallid Eyes [OFFICIAL AUDIO] (152kbit_Opus)",
        )
        assert (result.artist, result.title) == ("Emily Jane White", "Pallid Eyes")
        assert result.confidence == HIGH

    def test_a_real_artist_tag_on_a_non_rip_is_believed(self):
        """No YouTube URL means the artist tag means the artist. This is the
        correctly tagged album that used to land under "Various Artists"."""
        result = propose(title_tag="Kayleigh", artist_tag="Marillion", comment=None)
        assert (result.artist, result.title, result.confidence) == (
            "Marillion",
            "Kayleigh",
            HIGH,
        )


class TestFalsePositivesFoundInTheDryRun:
    """Every case here was produced by the ladder against Todd's real library
    and was wrong. They are the reason for the dry run."""

    def test_a_leading_digit_is_part_of_the_name_not_a_track_number(self):
        result = propose(
            title_tag="4 Non Blondes - What's Up (Official Music Video)",
            artist_tag="4NonBlondesVEVO",
            comment=YT,
        )
        assert result.artist == "4 Non Blondes"  # not "Non Blondes"
        assert result.confidence == HIGH

    def test_a_real_track_number_prefix_is_still_dropped(self):
        assert split_artist_title("03. Miles Davis - So What") == (
            "Miles Davis",
            "So What",
        )

    def test_a_dash_after_a_track_number_is_not_an_artist_boundary(self):
        """"01 - Are You Alive? / Battlestar Galactica Main Title" is a title
        with a track number, not an artist named "01". Refusing the split sends
        it down to the channel, which is where the answer actually is."""
        assert split_artist_title("01 - Are You Alive? / Battlestar Galactica") is None

    def test_channel_buried_mid_artist_means_the_split_was_wrong(self):
        result = propose(
            title_tag="Ode to Josephine By Tumbledown House - NPR Tiny Desk Contest Submission 2018",
            artist_tag="Tumbledown House",
            comment=YT,
        )
        assert result.confidence == LOW
        assert not result.auto_applicable

    def test_a_sentence_length_artist_is_held_for_review(self):
        result = propose(
            title_tag="Cups Pitch Perfect's When I'm Gone - Anna Kendrick (Lyrics)",
            artist_tag="Pillow",
            comment=YT,
        )
        assert result.confidence == LOW
        assert not result.auto_applicable

    def test_a_long_but_corroborated_credit_still_applies(self):
        """The guard must not swallow real multi-artist credits."""
        result = propose(
            title_tag="Lin-Manuel Miranda, Opetaia Foa'i - We Know The Way",
            artist_tag="DisneyMusicVEVO",
            comment=YT,
        )
        assert result.confidence == HIGH

    def test_title_dash_artist_ordering_is_detected_and_swapped(self):
        """Two signals agree the halves are reversed: the channel matches the
        half after the dash and not the half before it."""
        result = propose(
            title_tag="I LOVE PARIS (Cole Porter) – Tatiana Eva-Marie & Avalon Jazz Band",
            artist_tag="Tatiana Eva-Marie & Avalon Jazz Band",
            comment=YT,
        )
        assert result.artist == "Tatiana Eva-Marie & Avalon Jazz Band"
        assert result.title == "I LOVE PARIS (Cole Porter)"
        assert result.confidence == HIGH

    def test_the_normal_ordering_is_not_swapped(self):
        result = propose(
            title_tag="Ashnikko - Special (Official Video)",
            artist_tag="Ashnikko",
            comment=YT,
        )
        assert (result.artist, result.title) == ("Ashnikko", "Special")
