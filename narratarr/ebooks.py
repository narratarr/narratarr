"""Ebook files: which ones count, which to keep, and what an EPUB says about itself.

An ebook is one file. When a download brings several (an EPUB, a MOBI and a PDF of
the same book), Narratarr keeps the best one, in the order below. EPUB comes first:
Audiobookshelf, most reading apps and e-readers other than older Kindles open it.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree

EBOOK_TYPES = {
    ".epub": "application/epub+zip",
    ".azw3": "application/vnd.amazon.ebook",
    ".azw": "application/vnd.amazon.ebook",
    ".mobi": "application/x-mobipocket-ebook",
    ".pdf": "application/pdf",
    ".fb2": "application/x-fictionbook+xml",
    ".cbz": "application/vnd.comicbook+zip",
    ".cbr": "application/vnd.comicbook-rar",
    ".djvu": "image/vnd.djvu",
}
PREFERENCE = list(EBOOK_TYPES)  # best first
FORMATS = {ext.lstrip(".").upper() for ext in EBOOK_TYPES}

MAX_OPF_BYTES = 2 << 20  # an EPUB's metadata file; real ones are a few KB
MAX_COVER_BYTES = 20 << 20
_SIDECARS = re.compile(r"^(cover\.(jpe?g|png|webp)|desc\.txt|reader\.txt)$", re.I)


def is_ebook(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in EBOOK_TYPES and not p.name.startswith(("._", "."))


def ebook_files(folder: Path) -> list[Path]:
    if folder.is_file():
        return [folder] if is_ebook(folder) else []
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.rglob("*") if is_ebook(p)), key=lambda p: str(p).lower())


def best(files: list[Path]) -> Path | None:
    """The one to keep: the preferred format, then the biggest (a full edition beats a sample)."""
    if not files:
        return None
    return min(files, key=lambda p: (PREFERENCE.index(p.suffix.lower()), -p.stat().st_size, str(p).lower()))


def sidecar(p: Path) -> bool:
    """Files Narratarr writes next to a book for Audiobookshelf: cover, description, narrator."""
    return bool(_SIDECARS.match(p.name))


_JUNK = {"@eadir", "thumbs.db", "desktop.ini", "metadata.json", "metadata.abs", "#recycle"}


def clutter(p: Path) -> bool:
    """What systems and apps leave in any folder: .DS_Store, Synology's @eaDir, Audiobookshelf's metadata.json."""
    return p.name.startswith(".") or p.name.lower() in _JUNK


# ---- EPUB metadata ---------------------------------------------------------------------------
# An EPUB is a zip: META-INF/container.xml names the package file (OPF), which holds the
# title, authors, description and which image is the cover.

_NS = {"c": "urn:oasis:names:tc:opendocument:xmlns:container", "opf": "http://www.idpf.org/2007/opf",
       "dc": "http://purl.org/dc/elements/1.1/"}


def _read(zf: zipfile.ZipFile, name: str, limit: int) -> bytes | None:
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.file_size > limit:
        return None
    with zf.open(info) as f:
        data = f.read(limit + 1)
    return data if len(data) <= limit else None


def _package(zf: zipfile.ZipFile) -> tuple[str, ElementTree.Element] | None:
    container = _read(zf, "META-INF/container.xml", MAX_OPF_BYTES)
    if not container:
        return None
    root = ElementTree.fromstring(container)
    rootfile = root.find(".//c:rootfile", _NS)
    opf_path = rootfile.get("full-path") if rootfile is not None else None
    opf = _read(zf, opf_path, MAX_OPF_BYTES) if opf_path else None
    if not opf:
        return None
    return opf_path, ElementTree.fromstring(opf)


def _text(el: ElementTree.Element | None) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def _plain(html: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>|</p>", "\n", html or "")
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">")
    return "\n\n".join(p for p in (" ".join(line.split()) for line in text.split("\n")) if p)


def epub_meta(path: Path) -> dict:
    """{title, author, description, language} from an EPUB; empty values when it doesn't say."""
    out = {"title": "", "author": "", "description": "", "language": ""}
    try:
        with zipfile.ZipFile(path) as zf:
            found = _package(zf)
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError, ValueError, KeyError):
        return out
    if not found:
        return out
    meta = found[1].find("opf:metadata", _NS)
    if meta is None:
        return out
    authors = [_text(c) for c in meta.findall("dc:creator", _NS)]
    out["title"] = _text(meta.find("dc:title", _NS))[:300]
    out["author"] = ", ".join(a for a in authors if a)[:300]
    out["description"] = _plain(_text(meta.find("dc:description", _NS)))[:5000]
    out["language"] = _text(meta.find("dc:language", _NS))[:20]
    return out


def epub_cover(path: Path) -> tuple[bytes, str] | None:
    """The cover image inside an EPUB, as (bytes, mime type)."""
    try:
        with zipfile.ZipFile(path) as zf:
            found = _package(zf)
            if not found:
                return None
            opf_path, pkg = found
            items = pkg.findall("opf:manifest/opf:item", _NS)
            # EPUB 3 marks the cover in the manifest; EPUB 2 points at it from a <meta>.
            item = next((i for i in items if "cover-image" in (i.get("properties") or "").split()), None)
            if item is None:
                ref = next((m.get("content") for m in pkg.findall("opf:metadata/opf:meta", _NS)
                            if m.get("name") == "cover"), None)
                item = next((i for i in items if ref and i.get("id") == ref), None)
            if item is None or not (item.get("media-type") or "").startswith("image/"):
                return None
            name = posixpath.normpath(posixpath.join(posixpath.dirname(opf_path), item.get("href") or ""))
            data = _read(zf, name, MAX_COVER_BYTES)
            return (data, item.get("media-type")) if data else None
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError, ValueError, KeyError):
        return None


# ---- reading in the browser -------------------------------------------------------------------
# The reader shows an EPUB one spine item (chapter file) at a time, fetched from inside the zip.
# Nothing is unpacked to disk.

MAX_MEMBER_BYTES = 64 << 20
READABLE_TYPES = {  # what a chapter may load from inside its book; anything else is sent as a download
    "application/xhtml+xml", "text/html", "text/css", "image/jpeg", "image/png", "image/gif", "image/webp",
    "image/svg+xml", "font/otf", "font/ttf", "font/woff", "font/woff2", "application/font-woff",
    "application/font-sfnt", "application/vnd.ms-opentype", "application/x-font-ttf", "application/x-font-otf",
}
_GUESS = {".xhtml": "application/xhtml+xml", ".html": "text/html", ".htm": "text/html", ".css": "text/css",
          ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
          ".svg": "image/svg+xml", ".otf": "font/otf", ".ttf": "font/ttf", ".woff": "font/woff", ".woff2": "font/woff2"}


def _resolve(base: str, href: str) -> tuple[str, str]:
    """A link inside the book, as (zip member name, #fragment)."""
    href, _, fragment = (href or "").partition("#")
    name = posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(href))) if href else base
    return name.lstrip("/"), fragment


class _NavParser(HTMLParser):
    """The table of contents in an EPUB 3 nav document: nested lists of links. Read as HTML, which forgives more than XML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []
        self.in_toc, self.depth, self.href, self.text = 0, -1, None, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "nav":
            kinds = f"{a.get('epub:type') or ''} {a.get('role') or ''}".split()
            if self.in_toc or "toc" in kinds or "doc-toc" in kinds:
                self.in_toc += 1
        elif self.in_toc and tag == "ol":
            self.depth += 1
        elif self.in_toc and tag == "a":
            self.href, self.text = a.get("href") or "", []

    def handle_endtag(self, tag):
        if tag == "nav" and self.in_toc:
            self.in_toc -= 1
        elif self.in_toc and tag == "ol":
            self.depth -= 1
        elif self.in_toc and tag == "a" and self.href is not None:
            title = " ".join("".join(self.text).split())
            if title:
                self.items.append({"href": self.href, "title": title[:200], "depth": max(self.depth, 0)})
            self.href = None

    def handle_data(self, data):
        if self.href is not None:
            self.text.append(data)


def _toc_from_nav(data: bytes) -> list[dict]:
    nav = _NavParser()
    nav.feed(data.decode("utf-8", "replace"))
    return nav.items


def _toc_from_ncx(data: bytes) -> list[dict]:
    ns = {"n": "http://www.daisy.org/z3986/2005/ncx/"}
    items: list[dict] = []

    def walk(el, depth):
        for point in el.findall("n:navPoint", ns):
            title = _text(point.find("n:navLabel/n:text", ns))
            content = point.find("n:content", ns)
            if title and content is not None:
                items.append({"href": content.get("src") or "", "title": title[:200], "depth": depth})
            walk(point, depth + 1)

    nav_map = ElementTree.fromstring(data).find("n:navMap", ns)
    if nav_map is not None:
        walk(nav_map, 0)
    return items


def _index(path: str, signature: tuple) -> dict:
    with zipfile.ZipFile(path) as zf:
        found = _package(zf)
        if not found:
            raise ValueError("This EPUB has no table of contents Narratarr can read.")
        opf_path, pkg = found
        manifest = {}
        for item in pkg.findall("opf:manifest/opf:item", _NS):
            name, _ = _resolve(opf_path, item.get("href") or "")
            manifest[item.get("id")] = {"name": name, "type": item.get("media-type") or "",
                                        "props": (item.get("properties") or "").split()}
        spine_el = pkg.find("opf:spine", _NS)
        spine = []
        for ref in spine_el.findall("opf:itemref", _NS) if spine_el is not None else []:
            item = manifest.get(ref.get("idref"))
            if item and item["type"] in ("application/xhtml+xml", "text/html") and item["name"] in zf.NameToInfo:
                # Its size lets the reader say how far through the whole book a page is.
                spine.append({"href": item["name"], "size": max(zf.NameToInfo[item["name"]].file_size, 1)})
        if not spine:
            raise ValueError("This EPUB has no pages Narratarr can show.")
        toc: list[dict] = []
        nav = next((i for i in manifest.values() if "nav" in i["props"]), None)
        ncx = manifest.get(spine_el.get("toc")) if spine_el is not None else None
        try:
            if nav and (data := _read(zf, nav["name"], MAX_OPF_BYTES)):
                toc = [{**t, "base": nav["name"]} for t in _toc_from_nav(data)]
            if not toc and ncx and (data := _read(zf, ncx["name"], MAX_OPF_BYTES)):
                toc = [{**t, "base": ncx["name"]} for t in _toc_from_ncx(data)]
        except (ElementTree.ParseError, ValueError):
            toc = []
        hrefs = {s["href"] for s in spine}
        entries = []
        for t in toc[:2000]:
            name, fragment = _resolve(t["base"], t["href"])
            if name in hrefs:
                entries.append({"title": t["title"], "href": name, "fragment": fragment, "depth": min(t["depth"], 4)})
        meta = pkg.find("opf:metadata", _NS)
        return {
            "title": _text(meta.find("dc:title", _NS)) if meta is not None else "",
            "spine": spine,
            "toc": entries,
            "rtl": spine_el is not None and spine_el.get("page-progression-direction") == "rtl",
            "types": {i["name"]: i["type"] for i in manifest.values()},
        }


_indexes: dict = {}


def epub_index(path: Path) -> dict:
    """The reading order (spine) and table of contents of an EPUB. Cached until the file changes."""
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime)
    if key not in _indexes:
        if len(_indexes) > 32:
            _indexes.clear()
        _indexes[key] = _index(str(path), key)
    return _indexes[key]


def epub_member(path: Path, name: str) -> tuple[bytes, str] | None:
    """One file from inside an EPUB, with the type the reader may treat it as. None if it isn't there."""
    types = epub_index(path)["types"]
    with zipfile.ZipFile(path) as zf:
        if name not in zf.NameToInfo or name.endswith("/"):
            return None
        data = _read(zf, name, MAX_MEMBER_BYTES)
    if data is None:
        return None
    kind = (types.get(name) or _GUESS.get(posixpath.splitext(name)[1].lower(), "")).split(";")[0].strip().lower()
    return data, kind if kind in READABLE_TYPES else "application/octet-stream"
