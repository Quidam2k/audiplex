"""Tests for M4B metadata extraction."""

from unittest.mock import MagicMock, patch

import pytest

from audiplex.utils.metadata import (
    BookMetadata,
    _roman_to_int,
    _word_to_int,
    apply_override,
    compute_file_hash,
    extract_metadata,
    load_overrides,
    normalize_author,
    normalize_series,
    parse_series_from_album,
)


class TestNormalizeAuthor:
    def test_none_passes_through(self):
        assert normalize_author(None) is None

    def test_empty_returns_none(self):
        assert normalize_author("") is None
        assert normalize_author("   ") is None

    def test_single_author_strips(self):
        assert normalize_author("  Brandon Sanderson  ") == "Brandon Sanderson"

    def test_comma_separated_keeps_first(self):
        assert (
            normalize_author("Brandon Sanderson, Steven Michael Bohls")
            == "Brandon Sanderson"
        )

    def test_semicolon_separated(self):
        assert normalize_author("Neil Gaiman; Terry Pratchett") == "Neil Gaiman"

    def test_ampersand_separated(self):
        assert normalize_author("Dave Barry & Ridley Pearson") == "Dave Barry"

    def test_translator_suffix_is_dropped(self):
        # Real example from the library
        assert (
            normalize_author("Cixin Liu, Ken Liu - translator") == "Cixin Liu"
        )

    def test_word_and(self):
        assert normalize_author("Terry Pratchett and Stephen Baxter") == "Terry Pratchett"


class TestParseSeriesFromAlbum:
    def test_none_and_empty(self):
        assert parse_series_from_album(None) == (None, None)
        assert parse_series_from_album("") == (None, None)
        assert parse_series_from_album("   ") == (None, None)

    def test_colon_form_with_unabridged(self):
        # Real example: real library entries with this exact shape.
        assert parse_series_from_album(
            "Caliban's War: The Expanse, Book 2 (Unabridged)"
        ) == ("The Expanse", "2")
        assert parse_series_from_album(
            "A Feast for Crows: A Song of Ice and Fire, Book 4 (Unabridged)"
        ) == ("A Song of Ice and Fire", "4")

    def test_colon_form_without_unabridged(self):
        assert parse_series_from_album(
            "Some Title: My Series, Book 3"
        ) == ("My Series", "3")

    def test_decimal_book_number(self):
        # Mistborn-style 0.5/1.5 novellas.
        assert parse_series_from_album(
            "Mistborn: Secret History: Mistborn, Book 3.5 (Unabridged)"
        ) == ("Mistborn", "3.5")

    def test_parenthesized_form(self):
        # Real example: "(The Scholomance, Book 1)" embedded inside a subtitle.
        assert parse_series_from_album(
            "A Deadly Education: A Novel (The Scholomance, Book 1) (Unabridged)"
        ) == ("The Scholomance", "1")

    def test_standalone_title_returns_none(self):
        # Plain book titles must NOT become 1-book "series".
        assert parse_series_from_album("A Civil Campaign (Unabridged)") == (None, None)
        assert parse_series_from_album("A Clash of Kings (Unabridged)") == (None, None)
        assert parse_series_from_album("Behave: The Biology of Humans") == (None, None)

    def test_collapses_whitespace_in_series(self):
        assert parse_series_from_album(
            "Title: My  Cool   Series, Book 1 (Unabridged)"
        ) == ("My Cool Series", "1")

    # --- New patterns ---

    @pytest.mark.parametrize("album,expected", [
        ("Curse of the Blue Tattoo: Bloody Jack #2 (Unabridged)", ("Bloody Jack", "2")),
        ("Under the Jolly Roger: Bloody Jack #3 (Unabridged)", ("Bloody Jack", "3")),
        ("In the Belly of the Bloodhound: Bloody Jack #4 (Unabridged)", ("Bloody Jack", "4")),
        ("Mississippi Jack: Bloody Jack #5 (Unabridged)", ("Bloody Jack", "5")),
    ])
    def test_hash_form(self, album, expected):
        assert parse_series_from_album(album) == expected

    @pytest.mark.parametrize("album,expected", [
        ("Dragonsong: Harper Hall Trilogy, Volume 1 (Unabridged)", ("Harper Hall Trilogy", "1")),
        ("Dragonsinger: Harper Hall Trilogy, Volume 2 (Unabridged)", ("Harper Hall Trilogy", "2")),
        ("Dragondrums: Harper Hall Trilogy, Volume 3 (Unabridged)", ("Harper Hall Trilogy", "3")),
    ])
    def test_volume_form(self, album, expected):
        assert parse_series_from_album(album) == expected

    @pytest.mark.parametrize("album,expected", [
        ("Hard Magic: Book I of the Grimnoir Chronicles (Unabridged)", ("Grimnoir Chronicles", "1")),
        ("Spellbound: Book II of the Grimnoir Chronicles (Unabridged)", ("Grimnoir Chronicles", "2")),
        ("Warbound: Book III of the Grimnoir Chronicles (Unabridged)", ("Grimnoir Chronicles", "3")),
        ("Some Title: Book IV of Great Series (Unabridged)", ("Great Series", "4")),
        ("Title: Book IX of Legacy (Unabridged)", ("Legacy", "9")),
        ("Title: Book X of Legacy (Unabridged)", ("Legacy", "10")),
    ])
    def test_roman_numeral_form(self, album, expected):
        assert parse_series_from_album(album) == expected

    @pytest.mark.parametrize("album,expected", [
        ("Quicksilver: Book One of The Baroque Cycle (Unabridged)", ("Baroque Cycle", "1")),
        ("The Confusion: Book Two of The Baroque Cycle (Unabridged)", ("Baroque Cycle", "2")),
        ("The System of the World: Book Three of The Baroque Cycle (Unabridged)", ("Baroque Cycle", "3")),
        ("Title: Book Five of the Chronicles (Unabridged)", ("Chronicles", "5")),
        ("Title: Book Twelve of Something", ("Something", "12")),
    ])
    def test_word_number_form(self, album, expected):
        assert parse_series_from_album(album) == expected

    @pytest.mark.parametrize("album,expected", [
        ("The Final Empire: Mistborn Book 1 (Unabridged)", ("Mistborn", "1")),
        ("The Well of Ascension: Mistborn Book 2 (Unabridged)", ("Mistborn", "2")),
    ])
    def test_series_book_n_form(self, album, expected):
        assert parse_series_from_album(album) == expected

    def test_new_patterns_dont_match_standalone(self):
        assert parse_series_from_album("Apex (Unabridged)") == (None, None)
        assert parse_series_from_album("The Great Novel (Unabridged)") == (None, None)


class TestNormalizeSeries:
    def test_none_passes_through(self):
        assert normalize_series(None) is None

    def test_strips_whitespace(self):
        assert normalize_series("  The Stormlight Archive  ") == "The Stormlight Archive"

    def test_collapses_internal_whitespace(self):
        assert normalize_series("The  Stormlight   Archive") == "The Stormlight Archive"

    def test_empty_returns_none(self):
        assert normalize_series("") is None
        assert normalize_series("   ") is None


class TestComputeFileHash:
    def test_deterministic(self, tmp_path):
        f = tmp_path / "test.bin"
        f.write_bytes(b"hello world")
        h1 = compute_file_hash(str(f))
        h2 = compute_file_hash(str(f))
        assert h1 == h2

    def test_different_files(self, tmp_path):
        f1 = tmp_path / "a.bin"
        f1.write_bytes(b"aaa")
        f2 = tmp_path / "b.bin"
        f2.write_bytes(b"bbb" * 100)
        assert compute_file_hash(str(f1)) != compute_file_hash(str(f2))


class TestExtractMetadata:
    def test_extracts_tags(self, tmp_path):
        """Test metadata extraction with mocked mutagen."""
        fake_path = str(tmp_path / "book.m4b")
        (tmp_path / "book.m4b").write_bytes(b"fake")

        mock_mp4 = MagicMock()
        mock_mp4.tags = {
            "\xa9nam": ["My Audiobook"],
            "\xa9ART": ["Jane Author"],
            "aART": ["John Narrator"],
            "\xa9alb": ["Great Series"],
            "trkn": [(3, 10)],
        }
        mock_mp4.info.length = 7200.0

        with patch("audiplex.utils.metadata.MP4", return_value=mock_mp4):
            meta = extract_metadata(fake_path)

        assert meta.title == "My Audiobook"
        assert meta.author == "Jane Author"
        assert meta.narrator == "John Narrator"
        # Album tag with no series pattern → series is None; raw retained.
        assert meta.series is None
        assert meta.series_raw == "Great Series"
        # Falls back to embedded track number when album tag has no Book N.
        assert meta.series_sequence == "3"
        assert meta.duration_seconds == 7200.0
        assert meta.file_size == 4  # len(b"fake")

    def test_missing_tags_uses_filename(self, tmp_path):
        """Falls back to filename for title when no tags."""
        fake_path = str(tmp_path / "My Cool Book.m4b")
        (tmp_path / "My Cool Book.m4b").write_bytes(b"fake")

        mock_mp4 = MagicMock()
        mock_mp4.tags = {}
        mock_mp4.info.length = 100.0

        with patch("audiplex.utils.metadata.MP4", return_value=mock_mp4):
            meta = extract_metadata(fake_path)

        assert meta.title == "My Cool Book"

    def test_no_tags_object(self, tmp_path):
        """Handle MP4 file with tags=None."""
        fake_path = str(tmp_path / "notags.m4b")
        (tmp_path / "notags.m4b").write_bytes(b"fake")

        mock_mp4 = MagicMock()
        mock_mp4.tags = None
        mock_mp4.info.length = 50.0

        with patch("audiplex.utils.metadata.MP4", return_value=mock_mp4):
            meta = extract_metadata(fake_path)

        assert meta.title == "notags"
        assert meta.author is None
        assert meta.has_cover is False


class TestRomanToInt:
    @pytest.mark.parametrize("roman,expected", [
        ("I", 1), ("II", 2), ("III", 3), ("IV", 4), ("V", 5),
        ("VI", 6), ("VII", 7), ("VIII", 8), ("IX", 9), ("X", 10),
    ])
    def test_roman_numerals(self, roman, expected):
        assert _roman_to_int(roman) == expected

    def test_case_insensitive(self):
        assert _roman_to_int("iii") == 3
        assert _roman_to_int("Iv") == 4


class TestWordToInt:
    @pytest.mark.parametrize("word,expected", [
        ("one", 1), ("two", 2), ("three", 3), ("twelve", 12),
    ])
    def test_word_numbers(self, word, expected):
        assert _word_to_int(word) == expected

    def test_case_insensitive(self):
        assert _word_to_int("One") == 1
        assert _word_to_int("THREE") == 3


class TestOverrides:
    def test_apply_override_match(self):
        overrides = [
            {"title": "Apex", "author": "Ramez Naam", "series": "Nexus", "sequence": "3"},
        ]
        assert apply_override("Apex", "Ramez Naam", overrides) == ("Nexus", "3")

    def test_apply_override_case_insensitive(self):
        overrides = [
            {"title": "Apex", "author": "Ramez Naam", "series": "Nexus", "sequence": "3"},
        ]
        assert apply_override("apex", "ramez naam", overrides) == ("Nexus", "3")

    def test_apply_override_no_match(self):
        overrides = [
            {"title": "Apex", "author": "Ramez Naam", "series": "Nexus", "sequence": "3"},
        ]
        assert apply_override("Nexus", "Ramez Naam", overrides) == (None, None)

    def test_apply_override_author_disambiguation(self):
        overrides = [
            {"title": "Apex", "author": "Ramez Naam", "series": "Nexus", "sequence": "3"},
        ]
        assert apply_override("Apex", "Different Author", overrides) == (None, None)

    def test_apply_override_no_author_in_entry(self):
        overrides = [
            {"title": "Apex", "series": "Nexus", "sequence": "3"},
        ]
        assert apply_override("Apex", "Any Author", overrides) == ("Nexus", "3")

    def test_load_overrides_from_file(self):
        overrides = load_overrides()
        assert len(overrides) >= 1
        assert overrides[0]["title"] == "Apex"
