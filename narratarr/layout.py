"""The folder layout: where in the library a book goes, from a template like {author}/{series}/{title}."""

import re

DEFAULT = "{author}/{title}"
VARIABLES = {
    "author": "The author (the first one, if there are several)",
    "title": "The book's title",
    "narrator": "The narrator (up to two)",
    "series": "The series name",
    "series_number": "The book's number in its series, like 1 or 2.5",
    "year": "The year it came out",
}
_FIELD = re.compile(r"\{([^{}]*)\}")
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def unknown(template: str) -> list[str]:
    """Variables in the template that Narratarr doesn't fill in."""
    return [name for name in _FIELD.findall(template) if name not in VARIABLES]


def values(book: dict) -> dict[str, str]:
    """What each variable stands for, for this book. Empty when the book doesn't have it."""
    series = book.get("series") or []
    first = series[0] if series and isinstance(series[0], dict) else {}
    released = str(book.get("released") or "")
    return {
        "author": book.get("author") or "",
        "title": book.get("title") or "",
        "narrator": ", ".join((book.get("narrators") or [])[:2]),
        "series": str(first.get("title") or ""),
        "series_number": _number(first.get("sequence")),
        "year": released[:4] if released[:4].isdigit() else "",
    }


def _number(sequence) -> str:
    text = str(sequence or "").strip()
    match = re.match(r"\d+(?:\.\d+)?", text)
    return match.group(0) if match else text


FALLBACKS = {"author": "Unknown Author", "title": "Untitled", "narrator": "Unknown Narrator"}


def folders(template: str, book: dict) -> list[str]:
    """The folder names, in order. A variable the book doesn't have is left out along with the words and
    punctuation around it: "Vol. {series_number} - {title}" is just "Dune" for a book without a series, and a
    folder that was only {series} disappears."""
    filled = {k: _clean(v) for k, v in values(book).items()}
    parts = []
    for part in (template or DEFAULT).split("/"):
        if not part.strip():
            continue
        name = _clean(_fill(part, filled))
        if name:
            parts.append(name)
    return parts


def _fill(part: str, filled: dict[str, str]) -> str:
    bits = _FIELD.split(part)  # literal, name, literal, name, ..., literal
    literals, names = bits[0::2], bits[1::2]
    if not names:
        return part
    vals = [filled.get(n, "") or (FALLBACKS.get(n, "") if n in FALLBACKS else "") for n in names]
    keep = [True] * len(literals)  # literal i sits between variable i-1 and variable i
    i = 0
    while i < len(vals):
        if vals[i]:
            i += 1
            continue
        j = i
        while j < len(vals) and not vals[j]:
            j += 1
        # variables i..j-1 are empty: drop the literals around them (i..j) ...
        for k in range(i, j + 1):
            keep[k] = False
        # ... but keep one separator when there's something on both sides ("Frank Herbert - Dune")
        if i > 0 and j < len(vals):
            keep[j] = True
        i = j
    out = []
    for k, literal in enumerate(literals):
        if keep[k]:
            out.append(literal)
        if k < len(vals):
            out.append(vals[k])
    return "".join(out).strip(" -–—_.,;:#~") if not all(vals) else "".join(out)


def _clean(part: str) -> str:
    part = _UNSAFE.sub("", str(part)).strip().strip(".")
    return " ".join(part.split())[:150]
