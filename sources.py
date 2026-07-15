"""Source fetchers - every source emits the same normalized item shape (drop-in design).

Sources (all free, no paid API):
  - Hacker News via Algolia (no auth, clean JSON)
  - Reddit, backend auto-selected: OAuth API (CI) -> RSS /new (residential) -> pullpush
  - GitHub issue search ("our tool can't do X, we do it by hand" lives in issue trackers;
    GITHUB_TOKEN raises the rate limit and is free in Actions)
  - Bluesky public search (no auth - the post-Twitter stream of operator complaints)

Failures are isolated at every level: one dead query, sub, or source never kills a run.
"""

from __future__ import annotations

import datetime as dt
import html
import os
import re
import sys
import time

import defusedxml.ElementTree as ET   # XXE / billion-laughs-safe parser for untrusted RSS
import requests

# Use the OS trust store (like curl) instead of only certifi's bundled CAs, so fetching works
# from behind a corporate/SSL-inspection proxy whose CA the OS trusts but certifi doesn't.
# Lives HERE (the network module) so it runs before any TLS connection regardless of entry point.
# No-op if truststore is missing or the platform has no usable store.
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

from industries import BASE_QUERIES

# --- Fetch knobs -------------------------------------------------------------
REDDIT_PAGE = 25
HN_PAGE = 25
GITHUB_PAGE = 25
BLUESKY_PAGE = 25
REDDIT_SLEEP = 1.0        # polite pause between reddit calls
GITHUB_SLEEP = 7.0        # unauthenticated GitHub search = 10 req/min; token raises to 30
QUERIES_PER_SUB = 3       # pay-signal queries per sub on the Reddit OAuth search API
ANON_GIVEUP = 8           # anonymous Reddit: give up after this many subs in a row return nothing
HTTP_TIMEOUT = 30
USER_AGENT = "python:ask-my-market:0.4 (by /u/DavidKorochik)"   # reddit requires a unique descriptive UA

# Reddit RSS /new is unfiltered recent posts (no server-side query), so we filter
# locally for pay-signal / manual-judgment language before spending classification.
PAY_SIGNAL_KEYWORDS = [
    "paying someone", "pay someone", "paid someone", "hired", "hire someone", "hire a",
    " va ", "virtual assistant", "freelancer", "outsource", "assistant to", "someone to",
    "wish there was", "why is there no", "is there a tool", "is there any tool", "any software",
    "spend hours", "hours every", "hours a day", "so much time", "waste of time", "time-consuming",
    "by hand", "manually", "manual process", "tedious", "keep track of", "keeping track",
    "reconcile", "reconciling", "chase ", "chasing", "triage", "categorize", "categorise",
    "monitor", "monitoring", "reviewing every", "one by one", "every single",
]

# One session reused across all sequential fetch requests (connection reuse - avoids a fresh TLS
# handshake per call - and sets the UA reddit requires once as a default header). Fetch is
# single-threaded, so sharing a Session is safe.
_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = USER_AGENT


def make_item(source_type, item_id, title, body, url, where, created_iso):
    return {
        "source_type": source_type,   # "reddit" | "hackernews" | "github" | "bluesky"
        "id": item_id,
        "title": (title or "").strip(),
        "body": (body or "").strip(),
        "url": url,
        "where": where,               # subreddit / "Hacker News" / repo / "Bluesky"
        "created": created_iso,
    }


def _iso(ts):
    try:
        return dt.datetime.fromtimestamp(float(ts), dt.timezone.utc).date().isoformat()
    except Exception:
        return ""


def _strip_html(text):
    if not text:
        return ""
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


def _has_pay_signal(item):
    blob = " " + (item["title"] + " " + item["body"]).lower() + " "
    return any(k in blob for k in PAY_SIGNAL_KEYWORDS)


def _in_github_ci():
    """Reddit blocks GitHub Actions runner IPs on every ANONYMOUS endpoint (.json 403, RSS 429,
    pullpush 429). Detect CI so we skip the anonymous grind there - it returns ~nothing after ~30 min
    of backoff - instead of pretending to scan. OAuth (per-account rate limit) still works from CI."""
    return os.environ.get("GITHUB_ACTIONS", "").lower() == "true"


# =============================================================================
# Hacker News (Algolia)
# =============================================================================


def fetch_hn(queries):
    """Hacker News via Algolia. Zero auth, clean JSON, fresh. Stories + comments."""
    items = []
    base = "https://hn.algolia.com/api/v1/search_by_date"
    for q in queries:
        for tag in ("story", "comment"):
            try:
                r = _SESSION.get(base, params={"query": q, "tags": tag, "hitsPerPage": HN_PAGE},
                                 timeout=HTTP_TIMEOUT)
                r.raise_for_status()
                hits = r.json().get("hits", [])
            except Exception as e:
                print(f"  [hn] '{q}' ({tag}) failed: {e}", file=sys.stderr)
                continue
            for h in hits:
                oid = h.get("objectID")
                if not oid:
                    continue
                url = f"https://news.ycombinator.com/item?id={oid}"
                if tag == "story":
                    title, body = (h.get("title") or h.get("story_title") or ""), (h.get("story_text") or "")
                else:
                    title, body = (h.get("story_title") or ""), (h.get("comment_text") or "")
                items.append(make_item("hackernews", url, title, _strip_html(body), url,
                                       "Hacker News", h.get("created_at", "")[:10]))
    return items


# =============================================================================
# Reddit (OAuth -> RSS -> pullpush, auto-selected by environment)
# =============================================================================


def fetch_reddit(industries, sample=False):
    """Reddit across the taxonomy, with a backend that AUTO-SELECTS by environment:

    - OAuth API (REDDIT_CLIENT_ID/SECRET set): authenticated, so it works from ANY IP -
      required in CI, where reddit blocks anonymous requests from datacenter IPs. Restores
      server-side pay-signal query search.
    - RSS /new (no creds, e.g. a local run from a residential IP): one request per sub,
      unfiltered recent -> filtered locally for pay-signal language.
    - pullpush.io: last-ditch per-sub fallback.
    """
    token = _reddit_token()
    if not token and _in_github_ci():
        print("  [reddit] no OAuth creds on GitHub Actions - reddit blocks runner IPs on every "
              "anonymous endpoint (.json/RSS/pullpush all 429/403), so anonymous fetch returns "
              "~nothing after ~30 min of backoff. Skipping reddit this run (other sources still "
              "scanned). Set REDDIT_CLIENT_ID/SECRET - OAuth is rate-limited per-account and works "
              "from any IP.", file=sys.stderr)
        return []
    print(f"  [reddit] backend: {'OAuth API (authenticated)' if token else 'RSS /new (anonymous)'}")
    sectors = industries[:2] if sample else industries
    n = 2 if sample else QUERIES_PER_SUB
    items = []
    empty_streak = 0   # circuit breaker for the anonymous path
    for sec in sectors:
        subs = sec["subreddits"][:1] if sample else sec["subreddits"]
        queries = (sec.get("queries", [])[:1] + BASE_QUERIES)[:n]
        for sub in subs:
            got = _fetch_one_sub(sub["name"], token, queries)
            items.extend(got)
            # Anonymous from a blocked IP (e.g. a residential run degrading mid-scan): RSS+pullpush
            # both 429, so every sub returns nothing after ~28s of backoff. Grinding all 166 would
            # blow any timeout, so bail once a run of subs is clearly getting nowhere. OAuth (token)
            # is not circuit-broken - it fails fast per-sub already.
            if not token:
                empty_streak = empty_streak + 1 if not got else 0
                if empty_streak >= ANON_GIVEUP:
                    print(f"  [reddit] {empty_streak} subs in a row returned nothing "
                          f"(IP rate-limited); giving up on reddit for this run. Set REDDIT_CLIENT_ID/"
                          f"SECRET for the OAuth backend, which works from any IP.", file=sys.stderr)
                    return items
    return items


def _fetch_one_sub(name, token, queries):
    """With a token (CI): use ONLY the OAuth API. Each query is isolated so one failure keeps the
    others' results, and there is NO anonymous fallback - RSS/pullpush 403/429 from CI IPs, so
    falling back per-sub would just burn ~28s/sub of backoff toward the job timeout for nothing.
    Without a token (local/residential run): RSS /new -> pullpush, both pay-signal-filtered."""
    if token:
        out = []
        for q in queries:
            try:
                out.extend(_reddit_oauth_search(name, q, token))
            except Exception as e:
                print(f"  [reddit] r/{name} q={q!r} oauth failed ({e})", file=sys.stderr)
            time.sleep(REDDIT_SLEEP)
        return out
    try:
        rows = _reddit_rss(name)
        time.sleep(REDDIT_SLEEP)
        return [it for it in rows if _has_pay_signal(it)]
    except Exception as e:
        print(f"  [reddit] r/{name} rss failed ({e}); trying pullpush", file=sys.stderr)
    try:
        return [it for it in _reddit_pullpush(name, None) if _has_pay_signal(it)]
    except Exception as e:
        print(f"  [reddit] r/{name} pullpush failed: {e}", file=sys.stderr)
        return []


def _reddit_token():
    """Application-only OAuth (client_credentials). Returns a bearer token, or None if no
    creds / it fails - the caller then falls back to anonymous RSS."""
    # strip: a trailing newline/space from pasting a secret into the GitHub UI otherwise
    # produces a silent 401 invalid_client, indistinguishable from "no creds configured".
    cid = (os.environ.get("REDDIT_CLIENT_ID") or "").strip()
    secret = (os.environ.get("REDDIT_CLIENT_SECRET") or "").strip()
    if not (cid and secret):
        return None
    try:
        r = _SESSION.post("https://www.reddit.com/api/v1/access_token",
                          auth=(cid, secret), data={"grant_type": "client_credentials"},
                          timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json().get("access_token")
    except Exception as e:
        print(f"  [reddit] OAuth token request failed ({e}); using anonymous", file=sys.stderr)
        return None


def _reddit_oauth_search(sub, query, token):
    """Authenticated in-subreddit search via oauth.reddit.com (works from any IP). Retries on
    429 (honoring x-ratelimit-reset) and 5xx - OAuth is ~100 QPM. raw_json=1 stops reddit
    HTML-encoding entities (&amp;, &#39;) in titles/bodies."""
    url = f"https://oauth.reddit.com/r/{sub}/search"
    params = {"q": query, "restrict_sr": 1, "sort": "new", "limit": REDDIT_PAGE,
              "type": "link", "raw_json": 1}
    headers = {"Authorization": f"bearer {token}"}
    r = None
    for delay in (0, 3, 8):
        if delay:
            time.sleep(delay)
        r = _SESSION.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
        if r.status_code != 429 and r.status_code < 500:
            break
        if r.status_code == 429:
            time.sleep(min(float(r.headers.get("x-ratelimit-reset", 0) or 0), 15))
    r.raise_for_status()
    return [_reddit_row(c.get("data", {})) for c in r.json().get("data", {}).get("children", [])]


_ATOM = "{http://www.w3.org/2005/Atom}"


def _reddit_rss(sub):
    """Fetch + parse a subreddit's Atom /new feed. Short backoff if the CDN 429s."""
    url = f"https://www.reddit.com/r/{sub}/new/.rss"
    r = None
    for delay in (0, 3, 8):
        if delay:
            time.sleep(delay)
        r = _SESSION.get(url, params={"limit": REDDIT_PAGE}, timeout=HTTP_TIMEOUT)
        if r.status_code != 429:
            break
    r.raise_for_status()
    return _parse_reddit_rss(r.text, sub)


def _parse_reddit_rss(xml_text, sub):
    """Reddit /new.rss is Atom. Pull title, permalink, body (HTML content), id, date."""
    root = ET.fromstring(xml_text)
    out = []
    for e in root.findall(f"{_ATOM}entry"):
        title = e.findtext(f"{_ATOM}title", default="")
        content = e.findtext(f"{_ATOM}content", default="")
        eid = e.findtext(f"{_ATOM}id", default="")
        updated = (e.findtext(f"{_ATOM}updated", default="") or "")[:10]
        link_el = e.find(f"{_ATOM}link")
        url = link_el.get("href") if link_el is not None else ""
        out.append(make_item("reddit", url or eid, title, _strip_html(content),
                             url, "r/" + sub, updated))
    return out


def _reddit_pullpush(sub, query):
    """pullpush.io - no-auth Pushshift successor. Rate-limits hard; back off and
    retry so a transient 429 doesn't silently drop a whole (valuable) query."""
    params = {"subreddit": sub, "size": REDDIT_PAGE, "sort": "desc", "sort_type": "created_utc"}
    if query is not None:
        params["q"] = query
    r = None
    for delay in (0, 2, 5, 10):  # first try, then escalating backoff on 429
        if delay:
            time.sleep(delay)
        r = _SESSION.get("https://api.pullpush.io/reddit/search/submission/",
                         params=params, timeout=HTTP_TIMEOUT)
        if r.status_code != 429:
            break
    r.raise_for_status()
    return [_reddit_row(d) for d in r.json().get("data", [])]


def _reddit_row(d):
    permalink = d.get("permalink", "")
    url = ("https://www.reddit.com" + permalink) if permalink else (d.get("url") or "")
    return make_item("reddit", url or d.get("id", ""), d.get("title", ""), d.get("selftext", ""),
                     url, "r/" + str(d.get("subreddit", "")), _iso(d.get("created_utc")))


# =============================================================================
# GitHub issue search
# =============================================================================


def fetch_github(queries):
    """Search GitHub issues for pay-signal phrases. The complaint is attached to the failing
    product itself - the purest paying_for_bad_tool evidence there is. Uses GITHUB_TOKEN when
    present (30 searches/min; free + automatic in Actions), else anonymous (10/min, hence
    GITHUB_SLEEP). Two rate-limit refusals in a row -> stop the source, keep what we have."""
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    items, refusals = [], 0
    for q in queries:
        try:
            for attempt in range(2):   # one Retry-After-honoring retry per query, then move on
                r = _SESSION.get("https://api.github.com/search/issues",
                                 params={"q": f"{q} is:issue", "sort": "created", "order": "desc",
                                         "per_page": GITHUB_PAGE},
                                 headers=headers, timeout=HTTP_TIMEOUT)
                if r.status_code not in (403, 429):   # rate-limited / abuse-flagged
                    break
                refusals += 1
                print(f"  [github] '{q}' rate-limited ({r.status_code})", file=sys.stderr)
                if refusals >= 2:
                    print("  [github] rate-limited twice; stopping this source for the run "
                          "(set GITHUB_TOKEN for 3x the limit)", file=sys.stderr)
                    return items
                try:
                    wait = int(r.headers.get("retry-after") or 30)
                except ValueError:
                    wait = 30
                time.sleep(min(wait, 60))
            r.raise_for_status()
            refusals = 0
            for it in r.json().get("items", []):
                url = it.get("html_url") or ""
                repo = "/".join(url.split("/")[3:5]) if url else ""
                items.append(make_item("github", url, it.get("title", ""),
                                       (it.get("body") or "")[:3000], url,
                                       repo or "GitHub", (it.get("created_at") or "")[:10]))
        except Exception as e:
            print(f"  [github] '{q}' failed: {e}", file=sys.stderr)
        if not token:
            time.sleep(GITHUB_SLEEP)
    return items


# =============================================================================
# Bluesky public search
# =============================================================================


def fetch_bluesky(queries):
    """Bluesky public appview search - no auth, plain JSON. In-the-moment complaints from the
    post-Twitter crowd. at:// URIs are converted to clickable bsky.app permalinks."""
    items = []
    for q in queries:
        try:
            r = _SESSION.get("https://api.bsky.app/xrpc/app.bsky.feed.searchPosts",
                             params={"q": q, "limit": BLUESKY_PAGE, "sort": "latest"},
                             timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            posts = r.json().get("posts", [])
        except Exception as e:
            print(f"  [bluesky] '{q}' failed: {e}", file=sys.stderr)
            continue
        for p in posts:
            rec = p.get("record", {}) or {}
            text = rec.get("text", "")
            handle = (p.get("author", {}) or {}).get("handle", "")
            uri = p.get("uri", "")
            if not (text and handle and uri):
                continue
            url = _bsky_url(uri, handle)
            # posts have no title; first line stands in so the classifier sees the hook first
            title = text.split("\n", 1)[0][:120]
            items.append(make_item("bluesky", url, title, text, url, "Bluesky",
                                   (rec.get("createdAt") or "")[:10]))
    return items


def _bsky_url(at_uri, handle):
    """at://did:plc:xyz/app.bsky.feed.post/3kabc -> https://bsky.app/profile/handle/post/3kabc"""
    rkey = at_uri.rstrip("/").split("/")[-1]
    return f"https://bsky.app/profile/{handle}/post/{rkey}"

