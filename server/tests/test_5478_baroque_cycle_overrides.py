"""#5478: the four Baroque Cycle books after Quicksilver get their series from the override file."""
import pytest

from audiplex.utils.metadata import apply_override, load_overrides


@pytest.mark.parametrize("title,seq", [
    ("King of the Vagabonds", "2"),
    ("Odalisque", "3"),
    ("The Confusion", "4"),
    ("The System of the World", "6"),
])
def test_baroque_cycle_override(title, seq):
    assert apply_override(title, "Neal Stephenson", load_overrides()) == ("Baroque Cycle", seq)


def test_other_author_same_title_not_matched():
    assert apply_override("Odalisque", "Someone Else", load_overrides()) == (None, None)
