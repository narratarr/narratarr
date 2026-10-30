# Narratarr API v1

For apps that request books on someone's behalf: search Audible's catalog, ask for a book as an audiobook or an ebook, and follow it until it's in the library. Narratarr then finds a release, downloads it and files it, and media servers like Audiobookshelf pick it up from there.

- **Base URL:** `https://<your Narratarr>/api/v1`
- **Interactive reference:** `/api/v1/docs` on any Narratarr 0.6.0 or later
- **Machine-readable contract:** [`openapi-v1.json`](openapi-v1.json) (OpenAPI 3.1)

## Stability

v1 keeps working for as long as Narratarr does. Within v1, changes are **only additions**:

- New endpoints, new optional query parameters, and new fields in responses can appear. Ignore fields you don't know.
- Nothing is removed or renamed. No field changes type, no response field becomes optional, and no request gains a required field.
- Error **codes** are stable, but new ones can be added. Error **messages** are written for people and can change, so don't match on them.

Anything that would break an app goes into a `/api/v2`, served alongside v1. Narratarr's test suite checks every release against the committed contract, so a breaking change fails the build. The web UI uses a separate internal API (`/api/...` without `v1`). That one changes freely and doesn't accept app tokens, so don't build on it.

Check `GET /api/v1/status` for `api_version` to see what a server speaks.

## Before you start

- **Use HTTPS.** Tokens are bearer credentials, so anyone who can read them can use them. Over plain `http://` they can be read on the way, unless it's a home network you trust.
- **Hostnames.** To defend against DNS rebinding, Narratarr only answers to IP addresses, plain machine names and local domains (`.local`, `.lan`, `.home`, `.internal`, `.home.arpa`, `.ts.net`). It answers to a public domain like `narratarr.example.com` only once the server lists it in `NARRATARR_HOSTS`; otherwise it replies `421`. Anyone who opens Narratarr through a reverse proxy has already done this.
- **No CORS.** The API is for native apps and servers. Browsers on other websites can't call it.

## Signing in

Ask for the person's Narratarr username and password once, trade them for tokens, and then forget the password.

```http
POST /api/v1/auth/login
Content-Type: application/json

{"username": "sam", "password": "…", "client": "Fable Frog on Sam's iPhone"}
```

```json
{
  "access_token": "nrt_at_…",
  "refresh_token": "nrt_rt_…",
  "token_type": "Bearer",
  "expires_in": 3600,
  "refresh_expires_in": 7776000,
  "user": {"id": 2, "username": "sam", "role": "listener", "created_at": 1793300000.0}
}
```

`client` is how the app appears in the person's account, where they can sign it out. Name the app and the device. It can be up to 64 characters, and line breaks and control characters are removed.

Send the access token with every request:

```http
GET /api/v1/requests
Authorization: Bearer nrt_at_…
```

### Tokens

| | Lifetime | Notes |
|---|---|---|
| Access token | 1 hour | Send it as `Authorization: Bearer`. It only works on `/api/v1`. |
| Refresh token | 90 days, renewed each time it's used | Each one works **once**. |

Both start with a fixed prefix (`nrt_at_`, `nrt_rt_`) so secret scanners can spot a leaked one. Treat them as opaque; their length can change. Store the refresh token in the Keychain (or your platform's equivalent), not in plain preferences.

### Refreshing

When a request fails with `401` and code `token_expired`, or a little before `expires_in` runs out, trade the refresh token for a new pair:

```http
POST /api/v1/auth/refresh
Content-Type: application/json

{"refresh_token": "nrt_rt_…"}
```

The reply has the same shape as the login reply. **Replace both stored tokens with the new ones.** The old refresh token is now spent.

Refresh tokens rotate, and a spent one must never be sent again:

- **If a spent refresh token is used again,** Narratarr assumes someone copied it and signs the app out. All its tokens stop working, including the newest ones, and the person has to sign in again.
- **A retry within 2 minutes is allowed,** so a reply lost to a dropped connection doesn't sign anyone out. The retry gets a fresh pair, and the pair from the lost reply is retired. Using either token of a retired pair signs the app out, because it means two parties had the old refresh token. A retry no longer counts once a token from the earlier reply has been used, and more than three retries of the same refresh token sign the app out.
- **Run one refresh at a time.** If several requests hit `token_expired` together, refresh once and retry them all with the new access token.
- **Refresh about once an hour, not more.** An app can refresh 30 times in 10 minutes. Beyond that it gets `429 rate_limited`, but its current tokens keep working.

A refresh token stops working after 90 days without use, after `POST /auth/logout`, when the person signs the app out in Narratarr, when their password changes, or when their account is removed. The reply is then `401 invalid_token`: send the person back to sign-in.

### Signing out

```http
POST /api/v1/auth/logout
Authorization: Bearer nrt_at_…
```

Replies `204`. Every token from this sign-in stops working.

### Who am I

`GET /api/v1/me` returns `{"user": {…}, "app": {"id": 7, "name": "Fable Frog on Sam's iPhone"}}`. A user's `role` is `admin` or `listener`; both can search, request and follow requests.

## Finding a book

```http
GET /api/v1/search?q=project%20hail%20mary&page=1
```

```json
{
  "items": [
    {
      "asin": "B08G9PRS1K",
      "title": "Project Hail Mary",
      "subtitle": null,
      "authors": ["Andy Weir"],
      "narrators": ["Ray Porter"],
      "series": [],
      "cover_url": "https://m.media-amazon.com/images/I/….jpg",
      "description": "Ryland Grace is the sole survivor…",
      "runtime_minutes": 970,
      "release_date": "2021-05-04",
      "language": "English",
      "abridged": false,
      "request": null,
      "ebook_request": null
    }
  ],
  "page": 1,
  "pages": 3
}
```

The search covers Audible's catalog for the server's region, with one edition per title and books only (no podcasts). `q` is 1–200 characters and `page` is 1–10. When Narratarr already has a book, `request` holds it (see below), so you can show "Requested" or "In library" next to the result. `ebook_request` is the same for the book's ebook (since 0.7.0).

`GET /api/v1/books/{asin}` returns one book in the same shape.

## Requesting a book

```http
POST /api/v1/requests
Content-Type: application/json

{"asin": "B08G9PRS1K"}
```

- **`201 Created`** means it's new. The reply is the request, and `Location` gives its URL. Narratarr starts looking for a release straight away and keeps looking on its schedule.
- **`200 OK`** means Narratarr already has it, whether requested, downloading or in the library. That includes the same book from another source or another edition. The reply is the existing request. Asking twice is always safe.

### Ebooks

Since Narratarr 0.7.0, a request can be for the ebook instead:

```http
POST /api/v1/requests
Content-Type: application/json

{"asin": "B08G9PRS1K", "format": "ebook"}
```

`format` is `audiobook` (the default, and what every request was before 0.7.0) or `ebook`. A book wanted in both formats is two requests, each with its own id and status, so to ask for both, send both. Having one format doesn't count as having the other. The ASIN is still the Audible one: it says which book, and Narratarr finds the ebook on the server's indexers. When both formats arrive, Narratarr files them in one folder, so Audiobookshelf shows the book with a player and a reader.

A request looks like this:

```json
{
  "id": 42,
  "asin": "B08G9PRS1K",
  "format": "audiobook",
  "title": "Project Hail Mary",
  "authors": ["Andy Weir"],
  "narrators": ["Ray Porter"],
  "series": [],
  "cover_url": "https://m.media-amazon.com/images/I/….jpg",
  "status": "downloading",
  "stage": "downloading",
  "progress": 42.0,
  "message": "3.1 MB/s",
  "requested_by": {"id": 2, "username": "sam"},
  "requested_at": "2026-10-29T18:04:11Z",
  "updated_at": "2026-10-29T18:09:52Z",
  "available_at": null
}
```

| `status` | Meaning |
|---|---|
| `wanted` | Narratarr is looking for a release. `message` says how the last search went. |
| `downloading` | On its way. `stage` is `queued`, `preparing`, `downloading` or `importing`, and `progress` runs from 0 to 100. |
| `available` | In the library. `available_at` says since when. |
| `failed` | It didn't work. `message` says why. Narratarr's admin can retry it. |

`asin` is `null` for books that didn't come from Audible's catalog, such as imports or AudiobookBay. `format` is `audiobook` or `ebook`; ignore requests in formats you don't know, since more may be added. An ebook has no narrators. Times are ISO 8601 in UTC. `message` is for people.

### Following requests

```http
GET /api/v1/requests?status=wanted,downloading&mine=true&format=audiobook&limit=50&offset=0
```

```json
{"items": [ … ], "total": 3, "limit": 50, "offset": 0}
```

The list includes everything Narratarr has (everyone shares one library), most recently changed first.

- `status` takes one or more of the four statuses, comma-separated.
- `mine=true` keeps only books this person requested.
- `format` is `audiobook` (the default), `ebook` or `all`. The default keeps lists exactly as they were before 0.7.0, so apps that don't know about ebooks never see one.
- `limit` is 1–100 and `offset` starts at 0.

`GET /api/v1/requests/{id}` returns one request. Polling every 15–30 seconds while something is downloading, and less often otherwise, is plenty.

### Cancelling

```http
DELETE /api/v1/requests/{id}
```

Replies `204`. This stops a download in progress and removes the request. Only the person who requested it, or an admin, can cancel it (`403 forbidden` otherwise). Books already in the library can't be removed through the API (`409 conflict`).

## Errors

Every error has the same shape:

```json
{"error": {"code": "token_expired", "message": "That access token has expired. Use the refresh token to get a new one."}}
```

| HTTP | `code` | What to do |
|---|---|---|
| 401 | `unauthorized` | No token sent. Sign in. |
| 401 | `invalid_credentials` | Wrong username or password. |
| 401 | `token_expired` | Refresh, then retry. |
| 401 | `invalid_token` | The token is unknown, revoked or spent. Sign in again. |
| 403 | `forbidden` | Signed in, but not allowed (for example, cancelling someone else's request). |
| 404 | `not_found` | No such book, request or endpoint. |
| 405 | `method_not_allowed` | |
| 409 | `conflict` | It can't be done in its current state. |
| 409 | `setup_required` | The server has no accounts yet. Its owner opens Narratarr in a browser first. |
| 422 | `validation_error` | `fields` lists what was wrong. Values sent are never echoed back. |
| 429 | `rate_limited` | Wait `Retry-After` seconds. Tokens you hold keep working. |
| 502 | `upstream_unavailable` | Audible's catalog didn't answer. Try again shortly. |
| 500 | `internal_error` | Narratarr's log has the details. |

`401` replies carry a `WWW-Authenticate: Bearer` header, with `error="token_expired"` or `error="invalid_token"` where that applies.

## Limits

- **Sign-in:** 8 attempts per address in 10 minutes, shared with the web sign-in. IPv6 addresses count by /64, because one machine can have millions of them. On top of that there are 30 attempts per account from addresses it hasn't signed in from before, however many addresses they come from. The home network and networks that have signed in to the account before are never held back by the account limit, so knowing a username isn't enough to lock its owner out. Every attempt counts until it succeeds, so a burst of simultaneous guesses can't slip past the limit, and correct sign-ins don't count.
- **Refresh:** 30 per app in 10 minutes. Separately, 60 failed refreshes per IP address in 10 minutes; junk refresh tokens never count toward the sign-in limit.
- **Search and book lookups:** 60 a minute per person.
- **New requests:** 30 an hour per person. Asking for a book Narratarr already has doesn't count.
- **Apps per person:** 25. Signing in a 26th signs out the one unused for longest.

Behind a reverse proxy, every request seems to come from the proxy, so limits per address would apply to everyone at once. Set `NARRATARR_TRUSTED_PROXIES` to the proxy's address or range (for example `172.18.0.0/16`), and Narratarr uses the client address the proxy passes in `X-Forwarded-For`. That header is ignored from anyone else, because anyone can forge it. List only the proxy itself, not a whole network where clients connect directly, or those clients could pick their own address. Narratarr logs a warning if proxy headers arrive from an address that isn't listed.

Every response is sent with `Cache-Control: no-store`.

## Trying it out

```sh
NARRATARR=https://narratarr.example.com
TOKENS=$(curl -s "$NARRATARR/api/v1/auth/login" -H 'Content-Type: application/json' \
  -d '{"username":"sam","password":"…","client":"curl on my laptop"}')
ACCESS=$(echo "$TOKENS" | jq -r .access_token)

curl -s "$NARRATARR/api/v1/search?q=dune" -H "Authorization: Bearer $ACCESS" | jq '.items[0]'
curl -s "$NARRATARR/api/v1/requests" -H "Authorization: Bearer $ACCESS" \
  -H 'Content-Type: application/json' -d '{"asin":"B002V1OF70"}' | jq
```

If you're signed in to Narratarr in the same browser, `/api/v1/docs` lets you try every endpoint from the page.

## Changes

- **v1, Narratarr 0.6.0:** first release: sign-in, search, books, requests.
- **v1, Narratarr 0.7.0:** ebooks. `format` on new requests and on every request, `ebook_request` on books, and a `format` filter on `GET /requests` (audiobooks by default).
