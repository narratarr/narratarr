"""Whether a candidate really is the book that was asked for.

One place, because "wrong is worse than blank" is only true if every provider agrees on what wrong
means. A blank card is visible; a confident 4.6 on the wrong novel is not. So each provider hands
its candidates here and gets a yes or a no, and there is no scoring, no ranking and no "closest
match" — a candidate either clears the bar or the book stays unrated.

What is refused:

* a **near title** — "Etna Rising" is not "Etna";
* a **series neighbour** — "Etna (Book 2)" carries a volume marker, so it is a different book;
* a **surname-only** author — "Yoon" does not identify "Paul Yoon";
* anything below `MIN_COUNT` ratings, which `make_rating` enforces.
"""

from __future__ import annotations

import html
import re
import unicodedata

MIN_COUNT = 50

_DIGIT = re.compile(r"\d")
_TRAILING_GROUP = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")


def normalise(text: str) -> str:
    """Case, accents, ampersands and punctuation out; single spaces between words."""
    t = unicodedata.normalize("NFKD", html.unescape(text or ""))
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.replace("&", " and ")
    t = re.sub(r"[^0-9a-z]+", " ", t.lower())
    return " ".join(t.split())


def _strip_trailing_group(raw: str) -> tuple[str, str]:
    """Split "Etna (Book 2)" into ("Etna", "Book 2")."""
    m = _TRAILING_GROUP.search(raw or "")
    if not m:
        return raw or "", ""
    return (raw or "")[: m.start()], (raw or "")[m.start():].strip("()[] ")


def _core(raw: str) -> str:
    """Drop a trailing bracket group that is noise. A group with a digit in it is a volume marker
    and stays, so it can fail the comparison rather than be tidied away."""
    base, group = _strip_trailing_group(raw)
    if group and not _DIGIT.search(group):
        return base
    return raw or ""


def title_matches(query: str, candidate: str) -> bool:
    """Same book, allowing only cosmetic differences.

    Allowed: case, accents, punctuation ("Catch 22" / "Catch-22"), a trailing "(A Novel)", and a
    colon subtitle that carries no number ("Etna: A Novel").

    Refused: a near title ("Etna Rising"), a numbered volume marker ("Etna (Book 2)",
    "Etna: Book 2", "Etna (The Etna Saga, #1)"), and the next book in a series.
    """
    q = normalise(_core(query))
    if not q:
        return False
    if normalise(_core(candidate)) == q:
        return True
    base, colon, rest = (candidate or "").partition(":")
    if colon and not _DIGIT.search(rest) and normalise(_core(base)) == q:
        return True
    return False


def author_matches(query: str, candidates: list[str] | str) -> bool:
    """Every word of the name we asked for must appear in one of the names we were given.

    A surname on its own is not enough: "Yoon" matches half a bookshop. That is why the query's
    tokens must be a subset of a single candidate name rather than merely overlapping it.
    """
    asked = normalise(query).split()
    if not asked:
        return True  # no author to check against; the title has to carry it
    if isinstance(candidates, str):
        candidates = [candidates]
    for name in candidates or []:
        have = set(normalise(name).split())
        if have and set(asked) <= have:
            return True
    return False


def strict_match(query_title: str, query_author: str, candidate_title: str,
                 candidate_authors: list[str] | str) -> bool:
    """The single gate. Both halves must clear; a near miss is a no."""
    return title_matches(query_title, candidate_title) and author_matches(query_author, candidate_authors)
