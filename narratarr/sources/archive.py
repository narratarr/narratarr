"""Internet Archive sources: LibriVox and Old-Time Radio.

Both are curated, freely distributable collections hosted on archive.org, so
books download directly over HTTPS (no torrents, no Real-Debrid). We stick to
these collections on purpose: archive.org's general uploads include copyrighted
audiobooks posted without permission.

APIs: https://archive.org/advancedsearch.php and https://archive.org/metadata/<id>
"""

from __future__ import annotations

import re
from urllib.parse import quote, urlparse

import httpx
from bs4 import BeautifulSoup

from ..cache import Cache
from .base import Category, Details, Listing, Page

SEARCH = "https://archive.org/advancedsearch.php"
METADATA = "https://archive.org/metadata/{id}"
ROWS = 24
CACHE_SECONDS = 300
FIELDS = ["identifier", "title", "creator", "date", "language", "subject"]

# Which files to download, best first. LibriVox's "Audiobook" format is a single
# .m4b with chapter markers, which suits the player perfectly.
FORMAT_PREFERENCE = [
    ("Audiobook", "M4B"),
    ("VBR MP3", "MP3"),
    ("128Kbps MP3", "MP3"),
    ("64Kbps MP3", "MP3"),
    ("MP3", "MP3"),
    ("Ogg Vorbis", "OGG"),
    ("Flac", "FLAC"),
]

LANGUAGES = {
    "eng": "English",
    "spa": "Spanish",
    "fre": "French",
    "fra": "French",
    "ger": "German",
    "deu": "German",
    "ita": "Italian",
    "por": "Portuguese",
    "dut": "Dutch",
    "nld": "Dutch",
    "rus": "Russian",
    "chi": "Chinese",
    "zho": "Chinese",
    "jpn": "Japanese",
    "lat": "Latin",
    "grc": "Ancient Greek",
    "pol": "Polish",
    "fin": "Finnish",
    "swe": "Swedish",
    "dan": "Danish",
    "heb": "Hebrew",
    "hun": "Hungarian",
    "cze": "Czech",
}

_LUCENE_SPECIAL = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/]')
_cache: Cache[dict] = Cache(CACHE_SECONDS, 300)


def _first(v) -> str:
    if isinstance(v, list):
        return str(v[0]) if v else ""
    return str(v or "")


def _language(v) -> str:
    code = _first(v).strip()
    return LANGUAGES.get(code.lower(), code if len(code) > 3 else code.upper())


def _subjects(v) -> list[str]:
    items = v if isinstance(v, list) else re.split(r"[;,]", str(v or ""))
    skip = {"librivox", "audiobook", "audiobooks", "audio book", "old time radio", "otr", "otrr"}
    out = []
    for s in items:
        s = str(s).strip()
        if s and s.lower() not in skip and s.title() not in out:
            out.append(s.title())
    return out[:6]


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def _runtime(v: str) -> str:
    """'10:17:59' -> '10 hours and 18 minutes'."""
    parts = [int(p) for p in re.findall(r"\d+", v or "")]
    if len(parts) == 3:
        h, m = parts[0], parts[1] + (1 if parts[2] >= 30 else 0)
    elif len(parts) == 2:
        h, m = 0, parts[0]
    else:
        return ""
    return f"{h} hours and {m} minutes" if h else f"{m} minutes"


def _plain(html: str) -> str:
    html = re.sub(r"(?i)<br\s*/?>", "\n", html or "")
    html = re.sub(r"(?i)</p\s*>", "\n\n", html)
    text = BeautifulSoup(html, "html.parser").get_text()
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text)]
    return "\n\n".join(p for p in paras if p)


def pick_files(identifier: str, files: list[dict]) -> tuple[list[dict], str]:
    """Choose one consistent set of audio files, plus a format label."""
    for fmt, label in FORMAT_PREFERENCE:
        chosen = [
            f
            for f in files
            if f.get("format") == fmt
            and (fmt != "Audiobook" or f.get("name", "").lower().endswith(".m4b"))
        ]
        if chosen:
            return [
                {
                    "url": f"https://archive.org/download/{quote(identifier)}/{quote(f['name'])}",
                    "name": f["name"],
                    "size": int(f.get("size") or 0),
                }
                for f in chosen
            ], label
    return [], ""


class ArchiveSource:
    delivery = "direct"

    def __init__(
        self,
        name: str,
        label: str,
        scope: str,
        categories: list[tuple[str, str, str]],
        description: str,
        client: httpx.AsyncClient | None = None,
    ):
        self.name = name
        self.label = label
        self.description = description
        self.default_label = "Most popular"
        self.scope = scope
        self._categories = {slug: (title, clause) for slug, title, clause in categories}
        self._client = client

    # ---- Source interface ----------------------------------------------------

    def owns(self, url: str) -> bool:
        u = urlparse(url)
        return u.netloc in ("archive.org", "www.archive.org") and u.path.startswith("/details/")

    def categories(self) -> list[Category]:
        return [Category("new", "Newest")] + [
            Category(slug, t) for slug, (t, _) in self._categories.items()
        ]

    async def latest(self, page: int = 1, category: str | None = None) -> Page:
        q, sort = self.scope, "downloads desc"
        if category == "new":
            sort = "publicdate desc"
        elif category in self._categories:
            q = f"{q} AND {self._categories[category][1]}"
        return await self._search(q, page, sort)

    async def search(self, query: str, page: int = 1) -> Page:
        terms = " ".join(_LUCENE_SPECIAL.sub(" ", query).split())
        if not terms:
            return Page(items=[], page=page, total_pages=1)
        return await self._search(f"{self.scope} AND ({terms})", page, None)

    async def find(self, title: str, author: str = "") -> Page:
        """Look for one specific book by its title and author fields (more precise than a free-text search)."""
        words = lambda v: " AND ".join(_LUCENE_SPECIAL.sub(" ", v).split())  # noqa: E731
        q = f"{self.scope} AND title:({words(title)})"
        surname = _LUCENE_SPECIAL.sub(" ", author).split()[-1:] if author else []
        if surname:
            q += f" AND creator:({surname[0]})"
        return await self._search(q, 1, "downloads desc")

    async def details(self, url: str) -> Details:
        if not self.owns(url):
            raise ValueError("URL does not belong to this source")
        identifier = urlparse(url).path.split("/")[2]
        data = await self._get_json(METADATA.format(id=quote(identifier)))
        meta = data.get("metadata") or {}
        if not meta:
            raise ValueError("That item isn't available on the Internet Archive.")
        files, fmt = pick_files(identifier, data.get("files") or [])
        return Details(
            url=f"https://archive.org/details/{identifier}",
            title=_first(meta.get("title")) or identifier,
            author=_first(meta.get("creator")),
            cover_url=f"https://archive.org/services/img/{identifier}",
            language=_language(meta.get("language")),
            categories=_subjects(meta.get("subject")),
            format=fmt,
            size=_fmt_size(sum(f["size"] for f in files)) if files else "",
            posted=_first(meta.get("date"))[:10],
            description=_plain(_first(meta.get("description"))),
            length=_runtime(_first(meta.get("runtime"))),
            files=files,
        )

    # ---- helpers -------------------------------------------------------------

    async def _get_json(self, url: str, params: list | None = None) -> dict:
        key = url + repr(params)
        hit = _cache.get(key)
        if hit is not None:
            return hit
        if self._client:
            resp = await self._client.get(url, params=params)
        else:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(20, connect=8), follow_redirects=True
            ) as c:
                resp = await c.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        _cache.set(key, data)
        return data

    async def _search(self, q: str, page: int, sort: str | None) -> Page:
        params = [("q", q), ("rows", ROWS), ("page", page), ("output", "json")]
        params += [("fl[]", f) for f in FIELDS]
        if sort:
            params.append(("sort[]", sort))
        data = await self._get_json(SEARCH, params)
        resp = data.get("response") or {}
        items = []
        for doc in resp.get("docs", []):
            ident = doc.get("identifier")
            if not ident:
                continue
            items.append(
                Listing(
                    url=f"https://archive.org/details/{ident}",
                    title=_first(doc.get("title")) or ident,
                    author=_first(doc.get("creator")),
                    cover_url=f"https://archive.org/services/img/{ident}",
                    language=_language(doc.get("language")),
                    categories=_subjects(doc.get("subject")),
                    posted=_first(doc.get("date"))[:10],
                )
            )
        total = max(1, -(-int(resp.get("numFound") or 0) // ROWS))
        return Page(items=items, page=page, total_pages=min(total, 400))


def librivox(client: httpx.AsyncClient | None = None) -> ArchiveSource:
    return ArchiveSource(
        "librivox",
        "LibriVox",
        "collection:librivoxaudio",
        [
            ("mystery", "Mystery", "subject:(mystery OR detective)"),
            ("scifi", "Sci-Fi", 'subject:("science fiction" OR scifi OR sci-fi)'),
            ("fantasy", "Fantasy", "subject:(fantasy OR fairy)"),
            ("adventure", "Adventure", "subject:adventure"),
            ("horror", "Horror & Ghost", "subject:(horror OR ghost OR gothic)"),
            ("romance", "Romance", "subject:romance"),
            ("humor", "Humor", "subject:(humor OR humour OR comedy)"),
            ("children", "Children", "subject:(children OR juvenile)"),
            ("poetry", "Poetry", "subject:poetry"),
            ("history", "History", "subject:(history OR historical)"),
            ("philosophy", "Philosophy", "subject:philosophy"),
            ("short", "Short Stories", 'subject:("short stories" OR "short story")'),
            ("drama", "Plays", "subject:(drama OR play OR plays)"),
            ("religion", "Religion", "subject:(religion OR religious OR bible)"),
        ],
        "Free public-domain audiobooks read by volunteers.",
        client,
    )


def oldtimeradio(client: httpx.AsyncClient | None = None) -> ArchiveSource:
    return ArchiveSource(
        "oldtimeradio",
        "Old-Time Radio",
        "collection:oldtimeradio",
        [
            ("detective", "Detective", "(detective OR crime OR police)"),
            ("western", "Westerns", "(western OR westerns OR cowboy)"),
            ("scifi", "Sci-Fi", '("science fiction" OR scifi OR "x minus one" OR "dimension x")'),
            ("mystery", "Mystery & Suspense", "(mystery OR suspense OR thriller)"),
            ("horror", "Horror", '(horror OR terror OR "inner sanctum")'),
            ("comedy", "Comedy", "(comedy OR comedian OR sitcom)"),
            ("drama", "Drama", "(drama OR theatre OR theater)"),
            ("adventure", "Adventure", "adventure"),
        ],
        "Classic radio dramas and comedies from the 1930s–60s.",
        client,
    )
