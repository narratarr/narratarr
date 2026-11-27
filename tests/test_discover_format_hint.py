"""Discover says out loud that it does not rank by format.

The report behind this: a search on Discover showed an MP3 first while the same book's M4B sat at
the top of "Other releases". Nothing was broken — Discover lists results in the source's own order
and treats Format as a filter, while automatic picking and Other releases rank with an M4B
preference (`wanted._preference`). Two different lists, two different rankers, one confusing
silence. The fix is a sentence, and this file pins that it stays a sentence: no ranking change, no
default change, nothing that reaches the API.

There is no JS test harness in this repo, so these assertions read `static/app.js` the same way
tests/test_ratings.py does.
"""

from pathlib import Path

APP_JS = (Path(__file__).parent.parent / "narratarr/static/app.js").read_text()
HINT = "Automatic picking and Other releases prefer M4B."


def _refine_block() -> str:
    """The refine() function body, which builds the filter/sort bar."""
    start = APP_JS.index("function refine()")
    end = APP_JS.index("function storePicker()", start)
    return APP_JS[start:end]


def test_the_hint_sits_in_the_same_bar_as_the_format_control():
    block = _refine_block()
    assert HINT in block, "the hint must be part of the discover filter bar"
    # Declared once, then interpolated into the bar; the interpolation is what places it.
    format_at = block.index('data-r="fmt"')
    slot_at = block.index("${m4bHint}")
    assert slot_at > format_at, "the hint belongs beside the Format control, not above it"
    assert 'class="muted fmt-hint"' in block, "it is muted helper text, not a control"


def test_the_hint_is_only_for_audiobooks_from_an_indexer():
    block = _refine_block()
    assert 'd.kind === "audiobook"' in block, "ebooks prefer EPUB, so this sentence would be wrong"
    assert '["prowlarr", "torznab", "audiobookbay"].includes(src.name)' in block, (
        "catalog sources (Audible, LibriVox) have no format preference to explain"
    )


def test_the_sentence_appears_once_and_says_one_thing():
    assert APP_JS.count(HINT) == 1, "one sentence, one place"
    # It must not promise a consequence or a ranking change it does not make.
    lowered = HINT.lower()
    for word in ("will show", "will rank", "first", "only", "filtered out"):
        assert word not in lowered, f"the hint must describe, not promise: {word!r}"


def test_the_hint_does_not_change_what_discover_shows():
    """Scope guard: the fix is text. Format still defaults to All, and the API is untouched."""
    assert 'fmt: ""' in APP_JS, "Format must still default to All formats"
    block = _refine_block()
    # No server-side sort parameter is sent for the hint, and no client sort is added.
    assert "rating" in block  # the existing sorts are still the only ones
    assert "m4bHint" in block and "m4bHint" not in APP_JS.replace(block, ""), (
        "the hint expression must be local to the filter bar"
    )
