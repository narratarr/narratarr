"""Which languages someone wants their books in, and telling a book's language from what a source says.

Sources disagree on how they name a language ("ger", "de", "Deutsch", "German"), and indexers often
say nothing at all, leaving it to the release name ("Dune [German]", "Dune.Hörbuch.ungekürzt"). These
helpers turn all of that into one English name, so a preference like "German" can be checked anywhere.
"""

from __future__ import annotations

import re

# The languages offered in Settings, most-asked-for first.
COMMON = (
    "English", "German", "French", "Spanish", "Italian", "Dutch", "Portuguese", "Polish", "Swedish", "Danish",
    "Norwegian", "Finnish", "Czech", "Hungarian", "Russian", "Japanese", "Chinese", "Korean",
)

# Codes and native names → the English name.
_NAMES = {
    "en": "English", "eng": "English", "english": "English",
    "de": "German", "ger": "German", "deu": "German", "german": "German", "deutsch": "German",
    "fr": "French", "fre": "French", "fra": "French", "french": "French", "français": "French", "francais": "French",
    "es": "Spanish", "spa": "Spanish", "spanish": "Spanish", "español": "Spanish", "espanol": "Spanish", "castellano": "Spanish",
    "it": "Italian", "ita": "Italian", "italian": "Italian", "italiano": "Italian",
    "nl": "Dutch", "dut": "Dutch", "nld": "Dutch", "dutch": "Dutch", "nederlands": "Dutch",
    "pt": "Portuguese", "por": "Portuguese", "portuguese": "Portuguese", "português": "Portuguese", "portugues": "Portuguese",
    "pl": "Polish", "pol": "Polish", "polish": "Polish", "polski": "Polish",
    "sv": "Swedish", "swe": "Swedish", "swedish": "Swedish", "svenska": "Swedish",
    "da": "Danish", "dan": "Danish", "danish": "Danish", "dansk": "Danish",
    "no": "Norwegian", "nor": "Norwegian", "nob": "Norwegian", "norwegian": "Norwegian", "norsk": "Norwegian",
    "fi": "Finnish", "fin": "Finnish", "finnish": "Finnish", "suomi": "Finnish",
    "cs": "Czech", "cze": "Czech", "ces": "Czech", "czech": "Czech", "čeština": "Czech",
    "hu": "Hungarian", "hun": "Hungarian", "hungarian": "Hungarian", "magyar": "Hungarian",
    "ru": "Russian", "rus": "Russian", "russian": "Russian", "русский": "Russian",
    "ja": "Japanese", "jpn": "Japanese", "japanese": "Japanese", "日本語": "Japanese",
    "zh": "Chinese", "chi": "Chinese", "zho": "Chinese", "chinese": "Chinese", "中文": "Chinese", "mandarin": "Chinese",
    "ko": "Korean", "kor": "Korean", "korean": "Korean", "한국어": "Korean",
}

# Words that only ever mean one language, wherever they appear in a release name.
_NATIVE = {
    "deutsch": "German", "hörbuch": "German", "hoerbuch": "German", "ungekürzt": "German", "ungekuerzt": "German",
    "gekürzt": "German", "gekuerzt": "German", "hörspiel": "German", "hoerspiel": "German",
    "français": "French", "francais": "French", "livre audio": "French",
    "español": "Spanish", "espanol": "Spanish", "castellano": "Spanish", "audiolibro": "Spanish",
    "italiano": "Italian", "nederlands": "Dutch", "luisterboek": "Dutch",
    "português": "Portuguese", "audiolivro": "Portuguese", "polski": "Polish", "audiobook pl": "Polish",
    "svenska": "Swedish", "ljudbok": "Swedish", "dansk": "Danish", "lydbog": "Danish", "norsk": "Norwegian", "lydbok": "Norwegian",
    "suomi": "Finnish", "äänikirja": "Finnish", "magyar": "Hungarian", "hangoskönyv": "Hungarian",
}
# English names only count when set apart as a tag ("[German]", ".German.", " - German - "), so a title
# like "The German Girl" isn't taken for a German book; three-letter codes only inside brackets ("[GER]"),
# so "Hyperion.Dan.Simmons" isn't Danish.
_FULL = {k: v for k, v in _NAMES.items() if len(k) > 3 and k.isascii()}
_CODES = {k: v for k, v in _NAMES.items() if len(k) == 3 and k.isascii()}
_alt = lambda words: "|".join(sorted(map(re.escape, words), key=len, reverse=True))  # noqa: E731
_TAG_FULL = re.compile(r"(?:[\[(]|\b[._]|\s-\s)(" + _alt(_FULL) + r")(?:[\])]|[._]\b|\s-\s|$)", re.I)
_TAG_CODE = re.compile(r"[\[(](" + _alt(_CODES) + r")[\])]", re.I)


def name(value: str) -> str:
    """'ger', 'de', 'Deutsch' or 'german' → 'German'. Anything unknown is returned tidied, or ''."""
    v = str(value or "").strip()
    if not v:
        return ""
    return _NAMES.get(v.lower(), v.title() if len(v) > 3 else "")


def parse(setting: str) -> list[str]:
    """The Settings value ('German, English') as a list of names, in order, no repeats."""
    out = []
    for part in re.split(r"[,;]", setting or ""):
        n = name(part)
        if n and n not in out:
            out.append(n)
    return out


def detect(text: str, ignore: str = "") -> str:
    """The language a release name says it is in, or '' when it doesn't say.

    Words from `ignore` (the book's own title) never count, so 'The German Girl [English]' is English."""
    text = str(text or "")
    if ignore:
        for word in re.findall(r"\w+", ignore.lower()):
            text = re.sub(rf"(?i)\b{re.escape(word)}\b", " ", text)
    low = text.lower()
    for word, lang in _NATIVE.items():
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", low):
            return lang
    m = _TAG_FULL.search(text) or _TAG_CODE.search(text)
    return _NAMES[m.group(1).lower()] if m else ""


def allowed(language: str, wanted: list[str]) -> bool:
    """Whether a book in `language` is welcome. Unknown languages always are: we can't tell, so don't hide it."""
    if not wanted:
        return True
    lang = name(language)
    return not lang or lang in wanted
