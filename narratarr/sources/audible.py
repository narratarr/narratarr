"""Audible catalog as a Discover tab: find any book and add it to Wanted.

Audible is only where the book details come from. Nothing downloads from
Audible; Narratarr then looks for the book on Prowlarr (see wanted.py).
"""

from __future__ import annotations

from .. import audible as catalog
from .base import Category, Details, Page

# Audible's US genre IDs; other stores number their genres differently.
US_GENRES = [
    ("18580606011", "Sci-Fi & Fantasy"),
    ("18574597011", "Mystery & Thriller"),
    ("18574426011", "Literature & Fiction"),
    ("18580518011", "Romance"),
    ("18573518011", "History"),
    ("18571951011", "Biographies"),
    ("18574784011", "Self-development"),
    ("18572029011", "Business"),
    ("18580540011", "Science"),
    ("24427740011", "Comedy"),
    ("18580715011", "Teen"),
    ("18572091011", "Children"),
]


def to_details(b: dict) -> Details:
    series = b["series"][0] if b["series"] else None
    label = f"{series['title']} #{series['sequence']}" if series and series["sequence"] else (series or {}).get("title", "")
    return Details(
        url=b["url"],
        title=b["title"],
        author=b["author"],
        cover_url=b["cover_url"],
        language=b["language"],
        categories=[label] if label else [],
        posted=b["released"],
        narrators=b["narrators"],
        description=b["description"],
        length=b["length"],
        asin=b["asin"],
        series=b["series"],
        released=b["released"],
    )


class AudibleCatalog:
    name = "audible"
    label = "Audible catalog"
    description = "Find any book and add it to Wanted. Narratarr then looks for it on your indexers."
    default_label = "Bestsellers"
    delivery = "wanted"

    def owns(self, url: str) -> bool:
        return bool(catalog.asin_from_url(url))

    def categories(self) -> list[Category]:
        genres = US_GENRES if catalog.region() == "us" else []
        return [Category("new", "New releases")] + [Category(slug, name) for slug, name in genres]

    async def latest(self, page: int = 1, category: str | None = None) -> Page:
        params = {
            "num_results": 40, "page": page - 1, "response_groups": catalog.GROUPS,  # Audible counts from 0
            "products_sort_by": "-ReleaseDate" if category == "new" else "BestSellers",
        }
        if category and category != "new":
            params["category_id"] = category
        data = await catalog._get(f"{catalog.api_base()}/products", params) or {}
        books = catalog._keep([catalog.to_book(p) for p in data.get("products") or []], catalog.region())
        return Page(items=[to_details(b) for b in books], page=page, total_pages=10)

    async def search(self, query: str, page: int = 1) -> Page:
        books, total = await catalog.search_books(query, page)
        return Page(items=[to_details(b) for b in books], page=page, total_pages=total)

    async def details(self, url: str) -> Details:
        b = await catalog.book(catalog.asin_from_url(url))
        if not b:
            raise ValueError("Audible doesn't have that book any more.")
        d = to_details(b)
        d.description = d.description or b["subtitle"]
        return d
