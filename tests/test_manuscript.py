import pytest

from noveltool.manuscript import (Manuscript, ManuscriptError, count_chars,
    range_for_lines, split_text, validate_range)
from noveltool.revision import RevisionEngine


@pytest.mark.parametrize('text', ['', '  缩进\n\n第二段\n\n\n', '\n\n甲 😀\n乙\t\n', '甲\r\n乙\r', 'e\u0301　🎈'])
@pytest.mark.parametrize('mode', ['auto', 'line', 'blankline'])
def test_split_is_lossless(text, mode):
    parts = split_text(text, mode)
    assert ''.join(parts) == text
    assert all(parts)


def test_length_and_lines():
    assert count_chars(' 你 好，\n😀！\t') == 5
    assert range_for_lines('甲\n😀\n尾', 2, 2) == (2, 4)
    assert range_for_lines('甲\n', 2, 2) == (2, 2)
    with pytest.raises(ManuscriptError):
        range_for_lines('甲', 0, 1)


@pytest.mark.parametrize('start,end', [(-1, 1), (0, 99), (3, 2), (True, 1), (0.0, 1)])
def test_bad_ranges(start, end):
    with pytest.raises(ManuscriptError):
        validate_range('abc', start, end)


@pytest.mark.parametrize('text', ['\0', '\ud800'])
def test_unstorable_characters_rejected(text):
    with pytest.raises(ManuscriptError):
        split_text(text)


def test_prefix_and_suffix_are_not_lost():
    state = RevisionEngine.import_text(Manuscript(), 'abcdef\n\nghijkl\n\n尾', 0).manuscript
    plan = RevisionEngine.replace(state, 3, 11, 'XYZ', 1)
    assert plan.manuscript.text == state.text[:3] + 'XYZ' + state.text[11:]
    assert plan.manuscript.blocks[-1].id == state.blocks[-1].id
    assert state.text == 'abcdef\n\nghijkl\n\n尾'
