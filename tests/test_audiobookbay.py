from narratarr.sources.audiobookbay import AudiobookBay, parse_details, parse_listing, split_title

BASE = "https://audiobookbay.lu"


def test_listing_parses_results_and_pagination(fixture_html):
    page = parse_listing(fixture_html("abb_search.html"), BASE)
    assert page.total_pages == 27
    assert len(page.items) == 9

    dune = page.items[1]
    assert dune.url == f"{BASE}/abss/dune-frank-herbert-2/"
    assert (dune.title, dune.author) == ("Dune", "Frank Herbert")
    assert dune.cover_url.startswith("https://m.media-amazon.com/")
    assert dune.format == "M4B"
    assert dune.size == "1.12 GB"
    assert dune.language == "English"
    assert dune.categories == ["Adults", "Classic", "Sci-Fi"]


def test_listing_handles_author_first_titles(fixture_html):
    page = parse_listing(fixture_html("abb_search.html"), BASE)
    philosophy = page.items[2]
    assert philosophy.author == "Kevin S. Decker and William Irwin (editors)"
    assert philosophy.title.startswith("Dune and Philosophy")


def test_details_extracts_hash_trackers_and_metadata(fixture_html):
    d = parse_details(fixture_html("abb_detail.html"), f"{BASE}/abss/dune-frank-herbert-2/")
    assert d.info_hash == "061850ead3eb6f1c5c6d8420211b4bbf2d4ee3e2"
    assert "udp://tracker.opentrackr.org:1337/announce" in d.trackers
    assert len(d.trackers) == len(set(d.trackers))
    assert d.author == "Frank Herbert"
    assert d.narrators[0] == "Scott Brick"
    assert d.length == "21 hours and 2 minutes"
    assert d.bitrate == "125 kbps"
    assert "Arrakis" in d.description
    assert d.posted == "2025-12-24"


def test_magnet_link(fixture_html):
    d = parse_details(fixture_html("abb_detail.html"), f"{BASE}/abss/dune-frank-herbert-2/")
    magnet = d.magnet()
    assert magnet.startswith("magnet:?xt=urn:btih:061850ead3eb6f1c5c6d8420211b4bbf2d4ee3e2&dn=Dune")
    assert "tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce" in magnet


def test_split_title_without_separator():
    assert split_title("Just A Title") == ("Just A Title", "")


def test_owns_only_book_pages_on_configured_host():
    abb = AudiobookBay(BASE)
    assert abb.owns(f"{BASE}/abss/dune-frank-herbert-2/")
    assert not abb.owns("https://evil.example/abss/dune/")
    assert not abb.owns(f"{BASE}/member/login")


def test_posts_hidden_as_base64_are_read_too(fixture_html):
    """AudiobookBay sends some posts base64-encoded in a hidden .re-ab element, for its script to show."""
    import base64
    import re

    html = fixture_html("abb_search.html")
    normal = parse_listing(html, BASE)
    posts = re.findall(r'<div class="post">.*?<div class="postInfo">.*?</div>\s*</div>', html, re.S)
    assert posts, "the fixture has posts to hide"
    first = posts[0]
    hidden = base64.b64encode(first.encode()).decode()
    html = html.replace(first, f'<div class="re-ab" style="display:none">{hidden}</div>', 1)
    assert first not in html
    revealed = parse_listing(html, BASE)
    assert [i.url for i in revealed.items] == [i.url for i in normal.items]


def test_a_re_ab_block_that_is_not_a_post_does_not_break_the_page(fixture_html):
    html = fixture_html("abb_search.html").replace("<body", '<body><div class="re-ab">not base64 at all!!</div', 1)
    assert parse_listing(html, BASE).items
