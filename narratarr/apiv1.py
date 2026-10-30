"""Narratarr's public API, version 1, mounted at /api/v1.

For apps that request books on someone's behalf: search Audible's catalog, ask for a book
(as an audiobook, or since 0.7.0 as an ebook), and follow it until it's in the library. The web UI uses its own internal API, which can
change in any release; this one doesn't. Within v1, things are only ever added (new
endpoints, new fields, new values where a list says more may come). Anything that would
break an app goes into a v2 alongside it. Reference: docs/api.md, and /api/v1/docs.

Security, in short:
- Apps sign in with a username and password once and get tokens (tokens.py). Tokens only
  work here, never on the internal API, so an app can't change settings or users.
- Every route needs a signed-in person unless it's on the `public` router. The test suite
  checks that.
- "Don't ask for a password on my home network" doesn't apply: apps always sign in.
- No CORS: browsers on other sites can't call this, and the main app's same-site check
  refuses cross-site writes anyway. Native apps don't need CORS.
- Sign-in and refresh attempts share the web sign-in's limit per address; searching and
  requesting have limits per person.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections import deque
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import audible, auth, db, tokens, updates, users, wanted

log = logging.getLogger("narratarr.api.v1")

API_VERSION = 1
ASIN = r"^[A-Z0-9]{10}$"
STATUSES = ("wanted", "downloading", "available", "failed")
STAGES = {"queued": "queued", "resolving": "preparing", "caching": "preparing",
          "downloading": "downloading", "organizing": "importing"}


# ---- errors ------------------------------------------------------------------------------
# Every error is {"error": {"code": "...", "message": "..."}}. Codes are stable; messages are
# written for people and may change.


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict | None = None,
                 fields: list[dict] | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.headers = status, code, message, headers or {}
        self.fields = fields


def _error(status: int, code: str, message: str, headers: dict | None = None, **extra) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, **extra}}, status_code=status, headers=headers)


def _unauthorized(code: str, message: str) -> ApiError:
    challenge = 'Bearer realm="narratarr"' + (f', error="{code}"' if code in ("invalid_token", "token_expired") else "")
    return ApiError(401, code, message, {"WWW-Authenticate": challenge})


# ---- limits ------------------------------------------------------------------------------


class Limiter:
    """At most `limit` hits per key in `window` seconds."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self._hits: dict[object, deque] = {}

    def _live(self, key: object, now: float) -> deque:
        hits = self._hits.get(key)
        if hits is None:
            if len(self._hits) >= 10_000:  # bounded however many people and addresses there are
                self._forget_stale(now)
            hits = self._hits[key] = deque()
        while hits and now - hits[0] > self.window:
            hits.popleft()
        return hits

    def _forget_stale(self, now: float) -> None:
        for key in [k for k, h in self._hits.items() if not h or now - h[-1] > self.window]:
            del self._hits[key]
        if len(self._hits) >= 10_000:  # all live: drop the stalest half rather than everyone's count
            for key in sorted(self._hits, key=lambda k: self._hits[k][-1])[: len(self._hits) // 2]:
                del self._hits[key]

    def check(self, key: object) -> None:
        """Refuse with 429 if this key is out of hits, without using one."""
        now = time.monotonic()
        hits = self._live(key, now)
        if len(hits) >= self.limit:
            wait = int(self.window - (now - hits[0])) + 1
            raise ApiError(429, "rate_limited", f"Too many requests. Try again in {wait} seconds.",
                           {"Retry-After": str(wait)})

    def add(self, key: object) -> None:
        now = time.monotonic()
        self._live(key, now).append(now)

    def take(self, key: object) -> None:
        """Use a hit, or refuse with 429."""
        self.check(key)
        self.add(key)


search_limit = Limiter(60, 60)  # searches and book lookups, per person
request_limit = Limiter(30, 3600)  # new requests per person per hour
# Refresh tokens are 256 random bits, so guessing them is hopeless; this only stops floods.
# It's separate from the sign-in limit, so junk refreshes can't lock anyone out of signing in.
refresh_failures = Limiter(60, 600)  # per address


# ---- who's calling -------------------------------------------------------------------------


def _ip(request: Request) -> str:
    return auth.client_ip(request)


async def caller(request: Request) -> dict:
    """The signed-in person: from an access token, or from a web session (handy for trying the API out)."""
    header = request.headers.get("authorization")
    if header is not None:
        scheme, _, credential = header.strip().partition(" ")
        if scheme.lower() != "bearer" or not credential.strip():
            raise _unauthorized("invalid_token", "Send the access token as: Authorization: Bearer <token>")
        try:
            user, grant = await asyncio.to_thread(tokens.authenticate, credential.strip(), _ip(request))
        except tokens.TokenError as e:
            raise _unauthorized(e.code, str(e))
    else:
        user, grant = users.session_user(request.cookies.get(auth.COOKIE, "")), None
        if user is None:
            raise _unauthorized("unauthorized", "Sign in first: POST /api/v1/auth/login, then send the access "
                                                "token as Authorization: Bearer <token>.")
    request.state.user, request.state.grant = user, grant
    return user


Caller = Annotated[dict, Depends(caller)]


# ---- what the API returns --------------------------------------------------------------------


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _http_url(url: str) -> str | None:
    return url if isinstance(url, str) and url.startswith(("https://", "http://")) else None


def _names(author: str) -> list[str]:
    return [a.strip() for a in (author or "").split(",") if a.strip()]


def _series(series: list[dict]) -> list[dict]:
    return [{"asin": s.get("asin") or None, "name": s.get("title") or "", "position": s.get("sequence") or None}
            for s in series or [] if s.get("title")]


def public_status(status: str) -> str:
    if status in ("wanted", "failed"):
        return status
    return "available" if status == "completed" else "downloading"


def request_out(b: dict, names: dict[int, str]) -> dict:
    """One book Narratarr has: what it is, and how far it's got."""
    status = public_status(b["status"])
    by = b.get("requested_by")
    return {
        "id": b["id"],
        "asin": b.get("asin") or None,
        "format": wanted.kind_of(b),
        "title": b["title"],
        "authors": _names(b["author"]),
        "narrators": list(b.get("narrators") or []),
        "series": _series(b.get("series") or []),
        "cover_url": _http_url(b.get("cover_url") or ""),
        "status": status,
        "stage": STAGES.get(b["status"]) if status == "downloading" else None,
        "progress": round(float(b.get("progress") or 0), 1) if status == "downloading" else None,
        "message": (b.get("error") if status == "failed" else b.get("search_note") if status == "wanted"
                    else b.get("detail")) or None,
        "requested_by": {"id": by, "username": names[by]} if by in names else None,
        "requested_at": _iso(b["added_at"]),
        "updated_at": _iso(b["updated_at"]),
        "available_at": _iso(b.get("completed_at")) if status == "available" else None,
    }


def book_out(b: dict, have: dict | None, names: dict[int, str], ebook: dict | None = None) -> dict:
    """A book in Audible's catalog, and Narratarr's requests for it (audiobook and ebook) if there are any."""
    return {
        "asin": b["asin"],
        "title": b["title"],
        "subtitle": b.get("subtitle") or None,
        "authors": _names(b["author"]),
        "narrators": list(b.get("narrators") or []),
        "series": _series(b.get("series") or []),
        "cover_url": _http_url(b.get("cover_url") or ""),
        "description": b.get("description") or None,
        "runtime_minutes": b.get("minutes") or None,
        "release_date": b.get("released") or None,
        "language": b.get("language") or None,
        "abridged": bool(b.get("abridged")),
        "request": request_out(have, names) if have else None,
        "ebook_request": request_out(ebook, names) if ebook else None,
    }


def _book_out(b: dict, books: list[dict], names: dict[int, str]) -> dict:
    have = wanted.in_library(b["asin"], b["title"], b["author"], books)
    ebook = wanted.in_library(b["asin"], b["title"], b["author"], books, "ebook")
    return book_out(b, have, names, ebook)


def _usernames() -> dict[int, str]:
    return {u["id"]: u["username"] for u in users.all_users()}


def _audible_error(e: Exception) -> ApiError:
    return ApiError(502, "upstream_unavailable", str(e) or "Audible's catalog didn't answer. Try again shortly.")


# ---- response shapes ----------------------------------------------------------------------------
# Declared so FastAPI checks every response against them, and so /api/v1/docs shows exact
# types. tests/test_api_v1.py compares them with docs/openapi-v1.json: fields may be added,
# never removed or changed.

Status = Literal["wanted", "downloading", "available", "failed"]
Format = Literal["audiobook", "ebook"]
Stage = Literal["queued", "preparing", "downloading", "importing"]


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserOut(_Out):
    id: int
    username: str
    role: Literal["admin", "listener"]
    created_at: float


class TokensOut(_Out):
    access_token: str
    refresh_token: str
    token_type: Literal["Bearer"]
    expires_in: int = Field(description="Seconds until the access token expires.")
    refresh_expires_in: int = Field(description="Seconds the refresh token works for if unused.")
    user: UserOut


class AppOut(_Out):
    id: int
    name: str


class MeOut(_Out):
    user: UserOut
    app: AppOut | None = Field(description="The app this token belongs to; null for a web session.")


class StatusOut(_Out):
    app: Literal["narratarr"]
    version: str
    api_version: int


class SeriesOut(_Out):
    asin: str | None
    name: str
    position: str | None = Field(description='As Audible writes it: "1", "2.5", "1-3".')


class RequesterOut(_Out):
    id: int
    username: str


class RequestOut(_Out):
    id: int
    asin: str | None = Field(description="Null for books that didn't come from Audible's catalog.")
    format: Format = Field(description="audiobook or ebook. A book wanted in both formats is two requests. "
                                       "Added in Narratarr 0.7.0; more formats may be added.")
    title: str
    authors: list[str]
    narrators: list[str]
    series: list[SeriesOut]
    cover_url: str | None
    status: Status = Field(description="wanted: looking for a release. downloading: on its way. "
                                       "available: in the library. failed: see message.")
    stage: Stage | None = Field(description="While downloading: where it's got to.")
    progress: float | None = Field(description="While downloading: 0 to 100.")
    message: str | None = Field(description="For people: why it failed, the last search, or download detail.")
    requested_by: RequesterOut | None
    requested_at: str = Field(description="ISO 8601, UTC.")
    updated_at: str
    available_at: str | None


class BookOut(_Out):
    asin: str
    title: str
    subtitle: str | None
    authors: list[str]
    narrators: list[str]
    series: list[SeriesOut]
    cover_url: str | None
    description: str | None
    runtime_minutes: int | None
    release_date: str | None = Field(description="YYYY-MM-DD.")
    language: str | None
    abridged: bool
    request: RequestOut | None = Field(description="Narratarr's request for this book as an audiobook, if it has one.")
    ebook_request: RequestOut | None = Field(description="Narratarr's request for this book as an ebook, if it "
                                                               "has one. Added in Narratarr 0.7.0.")


class SearchOut(_Out):
    items: list[BookOut]
    page: int
    pages: int


class RequestsOut(_Out):
    items: list[RequestOut]
    total: int
    limit: int
    offset: int


class ErrorDetail(_Out):
    code: str = Field(description="Stable: unauthorized, invalid_credentials, invalid_token, token_expired, "
                                  "forbidden, not_found, method_not_allowed, validation_error, conflict, "
                                  "setup_required, rate_limited, upstream_unavailable, internal_error. "
                                  "More may be added.")
    message: str = Field(description="For people; may change.")
    fields: list[dict] | None = None


class ErrorOut(_Out):
    error: ErrorDetail


ERRORS = {code: {"model": ErrorOut} for code in (400, 401, 403, 404, 409, 422, 429, 502)}


# ---- bodies -----------------------------------------------------------------------------------


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")  # passwords are taken exactly as typed, spaces and all

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)
    client: str = Field(min_length=1, max_length=tokens.NAME_MAX,
                        description='Your app and device, shown to the person: "Fable Frog on iPhone".')


class Refresh(_Body):
    refresh_token: str = Field(min_length=1, max_length=200)


class NewRequest(_Body):
    asin: str = Field(pattern=ASIN, description="The book's Audible ASIN.")
    format: Format = Field("audiobook", description="audiobook (the default) or ebook. To want both, make two "
                                                    "requests. Added in Narratarr 0.7.0.")


# ---- the app ------------------------------------------------------------------------------------

v1 = FastAPI(
    title="Narratarr API",
    version=str(API_VERSION),
    description="Search Audible's catalog, request audiobooks and ebooks, and follow them until they're in the library. "
                "See docs/api.md in the Narratarr repository for the full guide.",
    docs_url="/docs",
    redoc_url=None,
    openapi_url="/openapi.json",
)
public = APIRouter(responses=ERRORS)
private = APIRouter(dependencies=[Depends(caller)], responses=ERRORS)  # everything here needs a signed-in person


@v1.middleware("http")
async def no_caching(request: Request, call_next):
    response = await call_next(request)
    if not request.url.path.endswith(("/docs", "/openapi.json")):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Narratarr-Api-Version"] = str(API_VERSION)
    return response


@v1.exception_handler(ApiError)
async def _api_error(request: Request, e: ApiError):
    extra = {"fields": e.fields} if e.fields is not None else {}
    return _error(e.status, e.code, e.message, e.headers, **extra)


@v1.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, e: StarletteHTTPException):
    code = {404: "not_found", 405: "method_not_allowed"}.get(e.status_code, "error")
    message = {404: "There's nothing here.", 405: "That method isn't allowed here."}.get(e.status_code, str(e.detail))
    return _error(e.status_code, code, message, getattr(e, "headers", None))


@v1.exception_handler(RequestValidationError)
async def _invalid(request: Request, e: RequestValidationError):
    # Say which fields were wrong and why, without echoing what was sent (it may be a password).
    fields = [{"field": ".".join(str(p) for p in err["loc"] if p not in ("body", "query", "path")) or None,
               "message": err["msg"]} for err in e.errors()]
    return _error(422, "validation_error", "Some of what was sent isn't valid.", fields=fields)


@v1.exception_handler(Exception)
async def _crashed(request: Request, e: Exception):
    log.exception("API v1 error on %s %s", request.method, request.url.path)
    return _error(500, "internal_error", "Something went wrong in Narratarr. Its log has the details.")


# ---- public ----------------------------------------------------------------------------------------


@public.get("/status", tags=["Server"], response_model=StatusOut, summary="Is this Narratarr, and which API does it speak?")
async def status():
    return {"app": "narratarr", "version": updates.current_version(), "api_version": API_VERSION}


@public.post("/auth/login", tags=["Signing in"], response_model=TokensOut, summary="Sign an app in with a username and password")
async def login(body: Login, request: Request):
    if users.count() == 0:
        raise ApiError(409, "setup_required", "Narratarr has no accounts yet. Open it in a browser to create one.")
    ticket = auth.start_try(request, body.username)  # counted before checking, so a burst can't slip past
    if ticket is None:
        raise ApiError(429, "rate_limited", "Too many sign-in attempts. Wait ten minutes and try again.",
                       {"Retry-After": str(auth.LOGIN_WINDOW)})
    user = await asyncio.to_thread(users.authenticate, body.username, body.password)
    if not user:
        raise ApiError(401, "invalid_credentials", "That username and password don't match.")
    auth.forgive(ticket)
    pair = await asyncio.to_thread(tokens.issue, user["id"], body.client, _ip(request))
    log.info("%s signed in an app: %s", user["username"], tokens.clean_name(body.client))
    return {**pair, "user": user}


@public.post("/auth/refresh", tags=["Signing in"], response_model=TokensOut, summary="Trade a refresh token for a new pair of tokens")
async def refresh(body: Refresh, request: Request):
    ip = _ip(request)
    refresh_failures.check(ip)
    try:
        pair, user = await asyncio.to_thread(tokens.refresh, body.refresh_token, ip)
    except tokens.TokenError as e:
        if e.code == "rate_limited":
            raise ApiError(429, "rate_limited", str(e), {"Retry-After": str(tokens.REFRESH_WINDOW)})
        refresh_failures.add(ip)
        raise _unauthorized(e.code, str(e))
    return {**pair, "user": user}


# ---- signed in ----------------------------------------------------------------------------------------


@private.post("/auth/logout", tags=["Signing in"], status_code=204, summary="Sign this app out (its tokens stop working)")
async def logout(request: Request, me: Caller):
    if request.state.grant is not None:
        tokens.revoke(request.state.grant, me["id"])
    return Response(status_code=204)


@private.get("/me", tags=["Signing in"], response_model=MeOut, summary="Who the token belongs to")
async def me(request: Request, me: Caller):
    grant = next((g for g in tokens.grants(me["id"]) if g["id"] == request.state.grant), None)
    return {"user": me, "app": {"id": grant["id"], "name": grant["name"]} if grant else None}


@private.get("/search", tags=["Catalog"], response_model=SearchOut, summary="Search Audible's catalog")
async def search(me: Caller,
                 q: Annotated[str, Query(min_length=1, max_length=200, description="Title, author, narrator or series.")],
                 page: Annotated[int, Query(ge=1, le=10)] = 1):
    if not q.strip():
        raise ApiError(422, "validation_error", "Type something to search for.", fields=[{"field": "q", "message": "empty"}])
    search_limit.take(me["id"])
    try:
        found, pages = await audible.search_books(q.strip(), page)
    except audible.AudibleError as e:
        raise _audible_error(e)
    books, names = db.list_books(), _usernames()
    return {"items": [_book_out(b, books, names) for b in found], "page": page, "pages": pages}


@private.get("/books/{asin}", tags=["Catalog"], response_model=BookOut, summary="One book in Audible's catalog")
async def book(me: Caller, asin: Annotated[str, Path(pattern=ASIN)]):
    search_limit.take(me["id"])  # each lookup asks Audible
    try:
        b = await audible.book(asin)
    except audible.AudibleError as e:
        raise _audible_error(e)
    if not b:
        raise ApiError(404, "not_found", "Audible doesn't have a book with that ASIN.")
    return _book_out(b, db.list_books(), _usernames())


@private.post("/requests", tags=["Requests"], response_model=RequestOut, status_code=201, summary="Request a book",
              responses={200: {"model": RequestOut, "description": "Narratarr already has it"}})
async def create_request(body: NewRequest, me: Caller, response: Response):
    response.status_code = 200  # unless it turns out to be new
    kind = body.format
    existing = next((b for b in db.list_books() if b.get("asin") == body.asin and wanted.kind_of(b) == kind), None)
    if existing:
        return request_out(existing, _usernames())
    request_limit.take(me["id"])
    try:
        b = await audible.book(body.asin)
    except audible.AudibleError as e:
        raise _audible_error(e)
    if not b:
        raise ApiError(404, "not_found", "Audible doesn't have a book with that ASIN.")
    have = wanted.in_library(body.asin, b["title"], b["author"], kind=kind)
    if have:  # the same book from another source, or another edition of it
        return request_out(have, _usernames())
    try:
        added = wanted.add_wanted(b, kind)
    except sqlite3.IntegrityError:  # requested twice at the same moment
        added = None
    if added is None:
        same = db.get_by_url(wanted.url_for(b["url"], kind)) or wanted.in_library(body.asin, b["title"], b["author"],
                                                                                   kind=kind)
        if same is None:
            raise ApiError(409, "conflict", "That book couldn't be added. Try again.")
        return request_out(same, _usernames())
    db.update(added["id"], requested_by=me["id"])
    log.info("%s requested %s (%s, %s)", me["username"], b["title"], body.asin, kind)
    from . import api as core  # here, not at the top: api.py mounts this module

    core._background(core._search_in_background(added["id"]))
    response.status_code = 201
    response.headers["Location"] = f"/api/v1/requests/{added['id']}"
    return request_out(db.get(added["id"]), _usernames())


@private.get("/requests", tags=["Requests"], response_model=RequestsOut, summary="Requests and the books Narratarr has, newest first")
async def list_requests(
    me: Caller,
    status: Annotated[str | None, Query(description="Comma-separated: wanted, downloading, available, failed.")] = None,
    mine: Annotated[bool, Query(description="Only books this person requested.")] = False,
    format: Annotated[Literal["audiobook", "ebook", "all"],
                      Query(description="Which format to list: audiobook (the default, as before 0.7.0), ebook, "
                                        "or all.")] = "audiobook",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
):
    wanted_statuses = None
    if status:
        wanted_statuses = {s.strip() for s in status.split(",") if s.strip()}
        unknown = wanted_statuses - set(STATUSES)
        if unknown:
            raise ApiError(422, "validation_error", f"Unknown status: {', '.join(sorted(unknown))}.",
                           fields=[{"field": "status", "message": f"one or more of: {', '.join(STATUSES)}"}])
    books = db.list_books()
    books = [b for b in books
             if (not wanted_statuses or public_status(b["status"]) in wanted_statuses)
             and (not mine or b.get("requested_by") == me["id"])
             and format in ("all", wanted.kind_of(b))]
    books.sort(key=lambda b: b["updated_at"], reverse=True)
    names = _usernames()
    return {"items": [request_out(b, names) for b in books[offset:offset + limit]],
            "total": len(books), "limit": limit, "offset": offset}


def _request_or_404(request_id: int) -> dict:
    b = db.get(request_id)
    if not b:
        raise ApiError(404, "not_found", "There's no request with that id.")
    return b


@private.get("/requests/{request_id}", tags=["Requests"], response_model=RequestOut, summary="One request")
async def get_request(me: Caller, request_id: Annotated[int, Path(ge=1, le=2**62)]):
    return request_out(_request_or_404(request_id), _usernames())


@private.delete("/requests/{request_id}", tags=["Requests"], status_code=204, summary="Cancel a request")
async def cancel_request(me: Caller, request_id: Annotated[int, Path(ge=1, le=2**62)]):
    b = _request_or_404(request_id)
    if me["role"] != "admin" and b.get("requested_by") != me["id"]:
        raise ApiError(403, "forbidden", "Only the person who requested it, or an admin, can cancel this.")
    if b["status"] == "completed":
        raise ApiError(409, "conflict", "That book is already in the library. Remove it in Narratarr instead.")
    from . import api as core

    await core.remove(request_id, delete_files=False)  # stops a download in progress, too
    log.info("%s cancelled the request for %s", me["username"], b["title"])
    return Response(status_code=204)


v1.include_router(public)
v1.include_router(private)

# Paths an app may call without signing in. Every other route must need a signed-in person
# (tests/test_api_v1.py checks this against the routes above).
OPEN_PATHS = {"/status", "/auth/login", "/auth/refresh", "/docs", "/docs/oauth2-redirect", "/openapi.json"}


def contract() -> dict:
    """The published OpenAPI document, as committed to docs/openapi-v1.json."""
    return v1.openapi()


if __name__ == "__main__":  # python -m narratarr.apiv1 > docs/openapi-v1.json, after adding to the API
    import json

    print(json.dumps(contract(), indent=2, sort_keys=True))
