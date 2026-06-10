"""Tests for CUE file parsing."""

import pytest

from audiplex.cue_parser import parse_cue_file, parse_timestamp


class TestParseTimestamp:
    def test_zero(self):
        assert parse_timestamp("00:00:00") == 0.0

    def test_minutes_and_seconds(self):
        assert parse_timestamp("02:30:00") == 150.0

    def test_with_frames(self):
        # 50 frames at 75fps = 0.6667 seconds
        result = parse_timestamp("00:00:50")
        assert abs(result - 50 / 75.0) < 0.001

    def test_large_value(self):
        # 32:10:25 = 32*60 + 10 + 25/75 = 1930.333...
        result = parse_timestamp("32:10:25")
        assert abs(result - (32 * 60 + 10 + 25 / 75.0)) < 0.001

    def test_invalid_format(self):
        with pytest.raises(ValueError):
            parse_timestamp("invalid")

    def test_whitespace(self):
        result = parse_timestamp("  01:00:00  ")
        assert result == 60.0


class TestParseCueFile:
    def test_standard_cue(self, sample_cue_file):
        chapters = parse_cue_file(sample_cue_file)

        assert len(chapters) == 4
        assert chapters[0].index == 0
        assert chapters[0].title == "Opening Credits"
        assert chapters[0].start_seconds == 0.0

        assert chapters[1].index == 1
        assert chapters[1].title == "Chapter 1 - The Beginning"
        assert abs(chapters[1].start_seconds - (2 * 60 + 30 + 50 / 75.0)) < 0.001

        assert chapters[2].index == 2
        assert chapters[2].title == "Chapter 2 - The Middle"

        assert chapters[3].index == 3
        assert chapters[3].title == "Chapter 3 - The End"

    def test_empty_file(self, tmp_path):
        cue = tmp_path / "empty.cue"
        cue.write_text("", encoding="utf-8")
        chapters = parse_cue_file(str(cue))
        assert chapters == []

    def test_no_titles(self, tmp_path):
        """Chapters without TITLE lines get default names."""
        content = '''FILE "book.MP3" MP3
  TRACK 01 AUDIO
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    INDEX 01 05:00:00
'''
        cue = tmp_path / "no_titles.cue"
        cue.write_text(content, encoding="utf-8")
        chapters = parse_cue_file(str(cue))

        assert len(chapters) == 2
        assert chapters[0].title == "Chapter 1"
        assert chapters[1].title == "Chapter 2"

    def test_rem_lines_ignored(self, tmp_path):
        content = '''REM GENRE Audiobook
REM COMMENT "Ripped by Libation"
FILE "book.MP3" MP3
  TRACK 01 AUDIO
    REM This is a comment
    TITLE "Chapter One"
    INDEX 01 00:00:00
'''
        cue = tmp_path / "rem.cue"
        cue.write_text(content, encoding="utf-8")
        chapters = parse_cue_file(str(cue))

        assert len(chapters) == 1
        assert chapters[0].title == "Chapter One"

    def test_utf8_bom(self, tmp_path):
        """Handle UTF-8 BOM that some tools write."""
        content = '''FILE "book.MP3" MP3
  TRACK 01 AUDIO
    TITLE "Kapitel Eins"
    INDEX 01 00:00:00
'''
        cue = tmp_path / "bom.cue"
        # Write with BOM
        cue.write_bytes(b'\xef\xbb\xbf' + content.encode("utf-8"))
        chapters = parse_cue_file(str(cue))

        assert len(chapters) == 1
        assert chapters[0].title == "Kapitel Eins"
