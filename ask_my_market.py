#!/usr/bin/env python3
"""
Ask My Market - a personal pain-discovery instrument (v2).

Not a product. A scanner that mines Reddit + Hacker News, ACROSS THE WHOLE
ECONOMY, for one specific shape of opportunity:

    A human being paid, or actively paying, to apply repeated judgment
    to a stream of information.

That is the shape of an agentic product hiding in the open. The judgment layer
(Claude) is opinionated toward one operator's filter. Every result carries the
industry and "where they gather", so the output doubles as an outreach list.

v2 adds: economy-wide industry taxonomy (industries.py), a `industry` tag on
every finding, cross-run memory (data/findings.json) so the dashboard ACCUMULATES
and tracks frequency-over-time, and a redesigned filterable dashboard suitable
for hosting on GitHub Pages via a scheduled Action.

Pipeline:
    load memory -> fetch (Reddit RSS + HN) -> prefilter -> skip already-seen
    -> Claude judgment (new only) -> competition web-search (high-fit) -> merge -> dashboard

Reddit is read via each subreddit's RSS /new feed (one request per sub, fresh, and
datacenter-tolerant where the .json API 403s and pullpush 429s); pullpush.io is a fallback.

Run:
    export ANTHROPIC_API_KEY=...
    python ask_my_market.py                     # scan -> classify new -> dashboard
    python ask_my_market.py --dry-run --sample  # fetch only, no API cost
    python ask_my_market.py --no-web-search     # skip the live competition web search
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import html
import json
import os
import sys
import time
import webbrowser
from pathlib import Path

import defusedxml.ElementTree as ET   # XXE / billion-laughs-safe parser for untrusted RSS
import requests

from industries import BASE_QUERIES, HN_QUERIES, INDUSTRIES

# =============================================================================
# CONFIG  -  edit freely (industries.py holds the sector/subreddit/query taxonomy)
# =============================================================================

# Fast + cheap classifier. Swap here to trade cost for depth.
MODEL = "claude-haiku-4-5-20251001"

# --- Competition check ----------------------------------------------------
# For high-fit ideas, ask Claude to LIVE web-search for existing competitors and
# then sanity-check the search against its own prior knowledge. FLAG ONLY - it
# NEVER changes fit_score/verdict; it's informational (a dashboard column + filter),
# so an overcrowded market is visible but you make the call.
COMPETITION_MODEL = "claude-haiku-4-5-20251001"   # swap to "claude-sonnet-5" for deeper analysis
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
COMPETITION_VERDICTS = ("worth_a_call", "watch")  # only research competitors for these (skips ignored)
COMPETITION_WORKERS = 4

# --- fit_score weights  -  TUNE THESE. Scoring is done in Python (not the model)
# so this dict is the single source of truth and the model never does math.
WEIGHTS = {
    "wtp_paying_a_human": 35,       # pays a PERSON/VA to do it by hand. Strongest signal.
    "wtp_paying_for_bad_tool": 20,  # pays for a tool that fails them ("X can't do Y").
    "wtp_just_complaining": 0,      # venting, no money moving.
    "judgment_on_stream": 25,       # watching a feed + repeated judgment. THE edge.
    "solo_shippable": 15,           # one engineer can ship a paid v1 in evenings.
    "time_to_pay_weeks": 10,        # buyer pays in weeks (good), not quarters.
    "funded_team_ignores": 10,      # market too niche for a VC-backed team to bother.
    "firsthand_domain": 5,          # dev tooling / security / founder-ops / compliance / investing.
}

WORTH_A_CALL_AT = 65   # fit_score >= this -> worth_a_call
WATCH_AT = 40          # >= this -> watch, else skip

# --- Fetch / cost knobs ---------------------------------------------------
MAX_ITEMS = 200            # cap on how many NEW survivors get classified per run
QUERIES_PER_SUB = 3        # pay-signal queries per sub when using the Reddit OAuth search API
REDDIT_PAGE = 25
HN_PAGE = 25
REDDIT_SLEEP = 1.0        # polite pause between reddit calls
CLASSIFY_WORKERS = 5
BODY_TRUNC = 1500
MIN_CHARS = 40
HTTP_TIMEOUT = 30
USER_AGENT = "python:ask-my-market:0.3 (by /u/DavidKorochik)"   # reddit requires a unique descriptive UA

DATA_PATH = "data/findings.json"   # cross-run memory (committed by the Action)

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

# Short industry labels the model is nudged toward (free-text, but consistent).
SECTOR_HINTS = sorted({i["sector"] for i in INDUSTRIES})

# =============================================================================
# Normalized item  -  every source emits this shape (drop-in source design)
# =============================================================================


def make_item(source_type, item_id, title, body, url, where, created_iso):
    return {
        "source_type": source_type,   # "reddit" | "hackernews"
        "id": item_id,
        "title": (title or "").strip(),
        "body": (body or "").strip(),
        "url": url,
        "where": where,               # subreddit or "Hacker News"
        "created": created_iso,
    }


def _iso(ts):
    try:
        return dt.datetime.fromtimestamp(float(ts), dt.timezone.utc).date().isoformat()
    except Exception:
        return ""


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# =============================================================================
# Fetchers  -  each returns list[item]; failures are isolated so one dead
# source never kills a run.
# =============================================================================


def fetch_hn(queries):
    """Hacker News via Algolia. Zero auth, clean JSON, fresh. Stories + comments."""
    items = []
    base = "https://hn.algolia.com/api/v1/search_by_date"
    for q in queries:
        for tag in ("story", "comment"):
            try:
                r = requests.get(base, params={"query": q, "tags": tag, "hitsPerPage": HN_PAGE},
                                 headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
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


def _strip_html(text):
    if not text:
        return ""
    import re
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


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
    print(f"  [reddit] backend: {'OAuth API (authenticated)' if token else 'RSS /new (anonymous)'}")
    sectors = industries[:2] if sample else industries
    n = 2 if sample else QUERIES_PER_SUB
    items = []
    for sec in sectors:
        subs = sec["subreddits"][:1] if sample else sec["subreddits"]
        queries = (sec.get("queries", [])[:1] + BASE_QUERIES)[:n]
        for sub in subs:
            items.extend(_fetch_one_sub(sub["name"], token, queries))
    return items


def _fetch_one_sub(name, token, queries):
    """OAuth query-search (already pay-signal) -> RSS /new + local filter -> pullpush + filter."""
    if token:
        try:
            out = []
            for q in queries:
                out.extend(_reddit_oauth_search(name, q, token))
                time.sleep(REDDIT_SLEEP)
            return out
        except Exception as e:
            print(f"  [reddit] r/{name} oauth failed ({e}); trying rss", file=sys.stderr)
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


def _has_pay_signal(item):
    blob = " " + (item["title"] + " " + item["body"]).lower() + " "
    return any(k in blob for k in PAY_SIGNAL_KEYWORDS)


def _reddit_token():
    """Application-only OAuth (client_credentials). Returns a bearer token, or None if no
    creds / it fails - the caller then falls back to anonymous RSS."""
    cid, secret = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    if not (cid and secret):
        return None
    try:
        r = requests.post("https://www.reddit.com/api/v1/access_token",
                          auth=(cid, secret), data={"grant_type": "client_credentials"},
                          headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json().get("access_token")
    except Exception as e:
        print(f"  [reddit] OAuth token request failed ({e}); using anonymous", file=sys.stderr)
        return None


def _reddit_oauth_search(sub, query, token):
    """Authenticated in-subreddit search via oauth.reddit.com (works from any IP)."""
    r = requests.get(f"https://oauth.reddit.com/r/{sub}/search",
                     params={"q": query, "restrict_sr": 1, "sort": "new", "limit": REDDIT_PAGE, "type": "link"},
                     headers={"Authorization": f"bearer {token}", "User-Agent": USER_AGENT},
                     timeout=HTTP_TIMEOUT)
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
        r = requests.get(url, params={"limit": REDDIT_PAGE},
                         headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
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
        r = requests.get("https://api.pullpush.io/reddit/search/submission/",
                         params=params, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
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
# Prefilter  -  dedup + drop un-judgeable noise, balanced across sources
# =============================================================================


def prefilter(items, limit):
    seen = set()
    by_bucket = {}
    for it in items:
        key = it["url"] or it["id"]
        if not key or key in seen:
            continue
        if len(it["title"]) + len(it["body"]) < MIN_CHARS:
            continue
        seen.add(key)
        # Round-robin by COMMUNITY (subreddit / "Hacker News"), not by source, so the
        # MAX_ITEMS cap spreads across INDUSTRIES instead of letting a few big subs
        # (or fresh HN) dominate. HN is one bucket -> a minority on the reddit-heavy
        # seed run, and the sole source on incremental runs (reddit skipped once seeded).
        by_bucket.setdefault(it["where"], []).append(it)
    queues = list(by_bucket.values())
    kept, i = [], 0
    while queues and len(kept) < limit:
        q = queues[i % len(queues)]
        kept.append(q.pop(0))
        if not q:
            queues.remove(q)
        else:
            i += 1
    return kept


# =============================================================================
# Judgment layer  -  Claude classifies; Python scores.
# =============================================================================

SYSTEM_PROMPT = f"""You are the discovery co-founder for a solo founder-engineer who builds agentic \
investigation systems for a living. You share his exact, opinionated filter. You are NOT a neutral \
pain-finder. You hunt one specific shape, ACROSS ANY INDUSTRY (not just tech):

  A human being paid, or actively paying, to apply repeated judgment to a stream of information.

That is an agentic product hiding in the open. Be RUTHLESS. Most items are noise and must be skipped. \
If you are torn, you skip. A tool that flags everything is useless.

For the one item given, return ONLY a strict JSON object (no prose, no markdown fences) with EXACTLY \
these keys:

  "pain":               one sentence, the real problem in the person's own framing.
  "quote":              verbatim from the text, UNDER 15 words, the single sharpest line. "" if none.
  "industry":           a short 1-3 word lowercase label for the sector this pain lives in
                        (e.g. accounting, trucking, healthcare, legal, real estate, construction,
                        restaurants, insurance, recruiting, dev tooling, msp). Known sectors include:
                        {", ".join(SECTOR_HINTS)}. Pick the closest, or coin a short label.
  "wtp_tier":           one of:
                          "paying_a_human"       -> hires/pays a person/VA/freelancer/assistant to do it by hand. STRONGEST.
                          "paying_for_bad_tool"  -> uses/pays for an existing tool that fails them ("X can't do Y").
                          "just_complaining"     -> venting, no evidence money moves.
  "judgment_on_stream": true/false. Does solving it mean watching a STREAM of information and applying
                        REPEATED judgment (monitoring, triaging, categorizing, reconciling, flagging, summarizing)?
  "solo_shippable":     true/false. Could ONE engineer ship a paid v1 in evenings, no team?
  "time_to_pay":        "weeks" | "quarters" | "unknown".
  "funded_team_ignores":true/false. Is the market niche enough (a few million, boring) that a VC-backed
                        team would not bother? (For us that is GOOD.)
  "firsthand_domain":   true/false. Is it in: dev tooling, security/SOC/incident response, founder-ops,
                        regulatory/tax compliance, or investing?
  "where_they_gather":  concrete place to find these people (subreddit / forum / community). ALWAYS fill it.

Return the JSON object and nothing else."""

VALID_WTP = {"paying_a_human", "paying_for_bad_tool", "just_complaining"}
VALID_TTP = {"weeks", "quarters", "unknown"}


def compute_score(c):
    """Deterministic fit_score from classified fields. Single source of truth = WEIGHTS."""
    s = 0
    wtp = c.get("wtp_tier")
    if wtp == "paying_a_human":
        s += WEIGHTS["wtp_paying_a_human"]
    elif wtp == "paying_for_bad_tool":
        s += WEIGHTS["wtp_paying_for_bad_tool"]
    if c.get("judgment_on_stream"):
        s += WEIGHTS["judgment_on_stream"]
    if c.get("solo_shippable"):
        s += WEIGHTS["solo_shippable"]
    if c.get("time_to_pay") == "weeks":
        s += WEIGHTS["time_to_pay_weeks"]
    if c.get("funded_team_ignores"):
        s += WEIGHTS["funded_team_ignores"]
    if c.get("firsthand_domain"):
        s += WEIGHTS["firsthand_domain"]
    return min(s, 100)


def verdict_for(score):
    if score >= WORTH_A_CALL_AT:
        return "worth_a_call"
    if score >= WATCH_AT:
        return "watch"
    return "skip"


def _extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start:end + 1])


def _coerce(c):
    """Validate at the boundary. Never trust the model's shape blindly."""
    if c.get("wtp_tier") not in VALID_WTP:
        c["wtp_tier"] = "just_complaining"
    if c.get("time_to_pay") not in VALID_TTP:
        c["time_to_pay"] = "unknown"
    for k in ("judgment_on_stream", "solo_shippable", "funded_team_ignores", "firsthand_domain"):
        c[k] = bool(c.get(k))
    c["pain"] = str(c.get("pain") or "").strip()
    c["quote"] = str(c.get("quote") or "").strip()
    c["where_they_gather"] = str(c.get("where_they_gather") or "").strip()
    c["industry"] = (str(c.get("industry") or "other").strip().lower() or "other")[:30]
    return c


def classify_item(client, item):
    """Classify one item. Returns the enriched record, or None on failure (isolated)."""
    body = item["body"][:BODY_TRUNC]
    user = f"Title: {item['title']}\n\nBody: {body}\n\nSource: {item['where']}"
    for attempt in range(2):
        try:
            msg = client.messages.create(model=MODEL, max_tokens=500, temperature=0,
                                         system=SYSTEM_PROMPT, messages=[{"role": "user", "content": user}])
            c = _coerce(_extract_json(msg.content[0].text))
            break
        except Exception as e:
            if attempt == 1:
                print(f"  [classify] gave up on {item['url']}: {e}", file=sys.stderr)
                return None
            time.sleep(1)
    score = compute_score(c)
    c["fit_score"] = score
    c["verdict"] = verdict_for(score)
    c["source_url"] = item["url"]
    c["source_type"] = item["source_type"]
    c["where_they_gather"] = c["where_they_gather"] or item["where"]
    c.update(_default_competition())   # every record carries competition fields; high-fit ones get filled
    return c


def classify_all(client, items):
    results, done = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=CLASSIFY_WORKERS) as pool:
        futures = [pool.submit(classify_item, client, it) for it in items]
        for fut in concurrent.futures.as_completed(futures):
            done += 1
            r = fut.result()
            if r:
                results.append(r)
            print(f"\r  classified {done}/{len(items)}", end="", flush=True)
    print()
    return results


# =============================================================================
# Competition check  -  is this idea already an overcrowded market? (flag only)
# =============================================================================

VALID_COMPETITION = {"open_field", "some_players", "crowded", "saturated", "not_checked"}


def _default_competition():
    return {"competition_level": "not_checked", "competitors": [],
            "comp_rationale": "", "comp_confidence": "unknown", "comp_sanity": ""}


def _all_text(msg):
    """Join ALL text blocks in order. Web-search answers split cited claims (e.g. the
    competitor names we want) across multiple text blocks, so last-block-only would drop them."""
    return "".join(b.text for b in msg.content
                   if getattr(b, "type", None) == "text" and getattr(b, "text", None))


def _coerce_competition(c):
    if c.get("competition_level") not in VALID_COMPETITION:
        c["competition_level"] = "not_checked"
    comps = c.get("competitors")
    comps = comps if isinstance(comps, list) else []
    c["competitors"] = [str(x).strip()[:100] for x in comps if str(x).strip()][:6]
    c["comp_rationale"] = str(c.get("rationale") or c.get("comp_rationale") or "").strip()[:240]
    c["comp_confidence"] = c.get("confidence") if c.get("confidence") in {"low", "medium", "high"} else "unknown"
    c["comp_sanity"] = str(c.get("sanity_check") or c.get("comp_sanity") or "").strip()[:240]
    # keep only our normalized keys
    return {k: c[k] for k in ("competition_level", "competitors", "comp_rationale", "comp_confidence", "comp_sanity")}


COMPETITION_PROMPT = """A solo founder is weighing whether to build a product that solves this pain:

  "{pain}"  (industry: {industry})

Assess the COMPETITIVE landscape - is this already a served, crowded market, or an open field?

{search_instr}Then SANITY-CHECK yourself: compare what you found against your own prior knowledge. Do they
agree? If web results and your knowledge disagree, say so and lower your confidence.

Return ONLY a strict JSON object (no prose, no fences):
  "competition_level": one of "open_field" (no/weak players), "some_players" (a few, beatable),
                       "crowded" (many established), "saturated" (dominated by strong incumbents).
  "competitors":       up to 6 real named products/tools/services that already address this ("Name - what it does").
  "rationale":         one sentence justifying the level.
  "confidence":        "low" | "medium" | "high".
  "sanity_check":      one sentence - did web search and your own knowledge agree? note any gap."""


def _ask_competition(client, record, use_web):
    """One assessment pass. Web branch gets a bigger token budget (server-injected search
    results eat into it) and a pause_turn loop (long web-search turns pause and must continue)."""
    instr = ("Use web search to find REAL existing products/tools/services that already solve this. "
             if use_web else "Judge from your own knowledge of the market. ")
    prompt = COMPETITION_PROMPT.format(pain=record.get("pain", ""),
                                       industry=record.get("industry", ""), search_instr=instr)
    messages = [{"role": "user", "content": prompt}]
    kwargs = {"model": COMPETITION_MODEL, "max_tokens": 2500 if use_web else 900}
    if use_web:
        kwargs["tools"] = [WEB_SEARCH_TOOL]
    msg = None
    for _ in range(4):   # continue across pause_turn (long-running web-search turns)
        msg = client.messages.create(messages=messages, **kwargs)
        if msg.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": msg.content})
    if msg.stop_reason == "max_tokens":
        print(f"  [competition] response truncated (max_tokens) for {record.get('source_url')}", file=sys.stderr)
    return _coerce_competition(_extract_json(_all_text(msg)))


def assess_competition(client, record, use_web=True):
    """Research competitors for one high-fit idea. Web search primary; on any error
    (web search disabled/errored/truncated) fall back to a model-knowledge-only pass -
    logged, so a silent always-fallback can't hide."""
    try:
        return _ask_competition(client, record, use_web)
    except Exception as e:
        if use_web:
            print(f"  [competition] web search failed for {record.get('source_url')} ({e}); "
                  f"falling back to model knowledge", file=sys.stderr)
            try:
                return _ask_competition(client, record, use_web=False)
            except Exception as e2:
                e = e2
        print(f"  [competition] gave up on {record.get('source_url')}: {e}", file=sys.stderr)
        return None


def attach_competition(client, results, use_web=True):
    """Gate to high-fit items, research each concurrently, attach the fields in place."""
    targets = [r for r in results if r.get("verdict") in COMPETITION_VERDICTS]
    if not targets:
        return
    print(f"Checking competition for {len(targets)} high-fit ideas (web search={'on' if use_web else 'off'})...")
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=COMPETITION_WORKERS) as pool:
        fut_map = {pool.submit(assess_competition, client, r, use_web): r for r in targets}
        for fut in concurrent.futures.as_completed(fut_map):
            done += 1
            comp = fut.result()
            if comp:
                fut_map[fut].update(comp)
            print(f"\r  competition {done}/{len(targets)}", end="", flush=True)
    print()


# =============================================================================
# Persistence  -  cross-run memory so the dashboard ACCUMULATES + tracks frequency
# =============================================================================


def load_store(path):
    """Returns {'_meta': {...}, 'findings': {url: record}}. Tolerates a missing/corrupt file."""
    p = Path(path)
    if not p.exists():
        return {"_meta": {}, "findings": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        data.setdefault("_meta", {})
        data.setdefault("findings", {})
        return data
    except Exception as e:
        print(f"  [store] {path} unreadable ({e}); starting fresh", file=sys.stderr)
        return {"_meta": {}, "findings": {}}


def merge_new(store, records, now):
    """Insert freshly-classified records with first_seen/last_seen/times_seen=1."""
    for r in records:
        r["first_seen"] = now
        r["last_seen"] = now
        r["times_seen"] = 1
        store["findings"][r["source_url"]] = r


def bump_seen(store, urls, now):
    """A URL surfaced again this run -> stronger frequency signal."""
    for u in urls:
        rec = store["findings"].get(u)
        if rec:
            rec["last_seen"] = now
            rec["times_seen"] = rec.get("times_seen", 1) + 1


def save_store(path, store):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(store, indent=2, sort_keys=True), encoding="utf-8")


# =============================================================================
# Report  -  one self-contained dashboard: summary cards + live filters + search
# =============================================================================

VERDICT_COLOR = {"worth_a_call": "#1a7f37", "watch": "#b7791f", "skip": "#6b7280"}
# green = open field (good), red = saturated (bad) - the market's verdict alongside the pain's
COMPETITION_COLOR = {"open_field": "#1a7f37", "some_players": "#0969da", "crowded": "#b7791f",
                     "saturated": "#cf222e", "not_checked": "#6b7280"}


def _safe_href(url):
    """Allow ONLY http(s). Blocks javascript:/data: scheme injection from scraped URLs."""
    url = (url or "").strip()
    return url if url.lower().startswith(("http://", "https://")) else ""


def render_html(store, run_stats):
    findings = sorted(store["findings"].values(), key=lambda r: r.get("fit_score", 0), reverse=True)
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    counts = {"worth_a_call": 0, "watch": 0, "skip": 0}
    industries = {}
    # "open lane" = a worth-a-call idea that is NOT already a crowded/saturated market
    open_lane = 0
    for r in findings:
        counts[r.get("verdict", "skip")] = counts.get(r.get("verdict", "skip"), 0) + 1
        industries[r.get("industry", "other")] = industries.get(r.get("industry", "other"), 0) + 1
        if r.get("verdict") == "worth_a_call" and r.get("competition_level") in ("open_field", "some_players"):
            open_lane += 1
    new_urls = run_stats.get("new_urls", set())

    ind_options = "".join(f'<option value="{html.escape(k)}">{html.escape(k)} ({v})</option>'
                          for k, v in sorted(industries.items(), key=lambda kv: -kv[1]))

    rows = []
    for r in findings:
        v = r.get("verdict", "skip")
        color = VERDICT_COLOR.get(v, "#6b7280")
        href = _safe_href(r.get("source_url", ""))
        src = html.escape(r.get("source_type", ""))
        src_cell = (f'<a href="{html.escape(href)}" target="_blank" rel="noopener">{src} &#8599;</a>'
                    if href else src)
        is_new = r.get("source_url") in new_urls
        newbadge = '<span class="new">NEW</span>' if is_new else ""
        seen = r.get("times_seen", 1)
        # competition cell: colored level badge + confidence letter, tooltip = rationale + competitors
        comp = r.get("competition_level", "not_checked")
        competitors = r.get("competitors", []) or []
        conf = r.get("comp_confidence", "unknown")
        if comp == "not_checked":
            comp_cell = '<span class="dim">-</span>'
        else:
            tip = r.get("comp_rationale", "")
            if competitors:
                tip = (tip + " | " if tip else "") + "; ".join(competitors)
            if r.get("comp_sanity"):
                tip += f"  (sanity: {r['comp_sanity']})"
            conf_sup = f'<span class="conf" title="confidence">{html.escape(conf[0])}</span>' if conf != "unknown" else ""
            comp_cell = (f'<span class="badge" style="background:{COMPETITION_COLOR.get(comp, "#6b7280")}" '
                         f'title="{html.escape(tip[:400])}">{html.escape(comp.replace("_", " "))}</span>{conf_sup}')
        search_blob = html.escape(f"{r.get('pain','')} {r.get('quote','')} {r.get('where_they_gather','')} "
                                  f"{r.get('industry','')} {' '.join(competitors)}".lower())
        rows.append(f"""
      <tr data-verdict="{v}" data-industry="{html.escape(r.get('industry','other'))}" data-wtp="{html.escape(r.get('wtp_tier',''))}"
          data-source="{src}" data-competition="{comp}" data-new="{'1' if is_new else '0'}" data-search="{search_blob}">
        <td class="num">{r.get('fit_score',0)}{newbadge}</td>
        <td><span class="badge" style="background:{color}">{html.escape(v)}</span></td>
        <td>{comp_cell}</td>
        <td>{html.escape(r.get('industry','other'))}</td>
        <td>{html.escape(r.get('wtp_tier',''))}</td>
        <td>{html.escape(r.get('pain',''))}</td>
        <td class="quote">{html.escape(r.get('quote',''))}</td>
        <td>{html.escape(r.get('where_they_gather',''))}</td>
        <td class="num" title="times this pain resurfaced across runs">{seen}</td>
        <td>{src_cell}</td>
      </tr>""")

    def card(n, label, color=None):
        style = f' style="color:{color}"' if color else ""
        return f'<div class="card"><div class="n"{style}>{n}</div><div class="l">{label}</div></div>'

    cards = "".join([
        card(len(findings), "total findings"),
        card(counts["worth_a_call"], "worth a call", VERDICT_COLOR["worth_a_call"]),
        card(open_lane, "worth a call + open lane", COMPETITION_COLOR["open_field"]),
        card(counts["watch"], "watch", VERDICT_COLOR["watch"]),
        card(run_stats.get("new_count", 0), "new this run", "#8957e5"),
        card(len(industries), "industries covered"),
    ])

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ask My Market</title>
<style>
  :root {{ color-scheme: light dark;
    --bg:#0d1117; --fg:#e6edf3; --dim:#9198a1; --line:#21262d; --panel:#161b22; --accent:#58a6ff; }}
  @media (prefers-color-scheme: light) {{
    :root {{ --bg:#fff; --fg:#1f2328; --dim:#59636e; --line:#d1d9e0; --panel:#f6f8fa; --accent:#0969da; }} }}
  * {{ box-sizing: border-box; }}
  body {{ font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    margin: 0; padding: 24px; background: var(--bg); color: var(--fg); }}
  h1 {{ font-size: 22px; margin: 0 0 2px; letter-spacing: -.02em; }}
  .sub {{ color: var(--dim); font-size: 13px; margin-bottom: 18px; }}
  .cards {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 18px; }}
  .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
    padding: 12px 18px; min-width: 120px; }}
  .card .n {{ font-size: 26px; font-weight: 700; font-variant-numeric: tabular-nums; }}
  .card .l {{ color: var(--dim); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }}
  .controls {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 14px; }}
  select, input[type=search] {{ background: var(--panel); color: var(--fg); border: 1px solid var(--line);
    border-radius: 8px; padding: 7px 10px; font-size: 14px; }}
  input[type=search] {{ min-width: 240px; flex: 1; }}
  label.chk {{ color: var(--dim); font-size: 14px; }}
  .count {{ color: var(--dim); font-size: 13px; margin-left: auto; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }}
  th {{ position: sticky; top: 0; background: var(--panel); font-size: 11px; text-transform: uppercase;
    letter-spacing: .04em; color: var(--dim); cursor: default; z-index: 1; }}
  td.num {{ font-weight: 700; font-variant-numeric: tabular-nums; white-space: nowrap; }}
  td.quote {{ font-style: italic; color: var(--dim); max-width: 260px; }}
  .badge {{ color: #fff; padding: 2px 9px; border-radius: 999px; font-size: 12px; white-space: nowrap; }}
  .new {{ background: #8957e5; color: #fff; font-size: 9px; font-weight: 700; padding: 1px 5px;
    border-radius: 4px; margin-left: 6px; vertical-align: middle; }}
  .conf {{ color: var(--dim); font-size: 10px; margin-left: 4px; text-transform: uppercase; cursor: help; }}
  .dim {{ color: var(--dim); }}
  .badge[title] {{ cursor: help; }}
  a {{ color: var(--accent); text-decoration: none; }}
  tr:hover td {{ background: var(--panel); }}
</style></head>
<body>
  <h1>Ask My Market</h1>
  <div class="sub">Run {ts} &middot; {run_stats.get('fetched',0)} fetched &middot;
    {run_stats.get('new_count',0)} newly classified &middot; {len(findings)} total in memory</div>

  <div class="cards">{cards}</div>

  <div class="controls">
    <select id="fVerdict"><option value="">all verdicts</option>
      <option value="worth_a_call">worth a call</option><option value="watch">watch</option><option value="skip">skip</option></select>
    <select id="fCompetition"><option value="">all competition</option>
      <option value="open_field">open field</option><option value="some_players">some players</option>
      <option value="crowded">crowded</option><option value="saturated">saturated</option>
      <option value="not_checked">not checked</option></select>
    <select id="fIndustry"><option value="">all industries</option>{ind_options}</select>
    <select id="fWtp"><option value="">all wtp tiers</option>
      <option value="paying_a_human">paying a human</option><option value="paying_for_bad_tool">paying for bad tool</option>
      <option value="just_complaining">just complaining</option></select>
    <select id="fSource"><option value="">all sources</option>
      <option value="reddit">reddit</option><option value="hackernews">hackernews</option></select>
    <input type="search" id="fSearch" placeholder="search pain / quote / where / industry...">
    <label class="chk"><input type="checkbox" id="fNew"> new only</label>
    <span class="count" id="count"></span>
  </div>

  <table>
    <thead><tr>
      <th>fit</th><th>verdict</th><th title="market saturation from live web search">competition</th>
      <th>industry</th><th>wtp tier</th><th>pain</th>
      <th>sharpest quote</th><th>where they gather</th><th>seen</th><th>source</th>
    </tr></thead>
    <tbody id="tb">{''.join(rows) if rows else '<tr><td colspan="10">No findings yet. Run a scan.</td></tr>'}</tbody>
  </table>
<script>
  var rows = Array.prototype.slice.call(document.querySelectorAll('#tb tr[data-verdict]'));
  ['fVerdict','fCompetition','fIndustry','fWtp','fSource','fSearch','fNew'].forEach(function(id){{
    var el=document.getElementById(id); if(el) el.addEventListener('input', apply);
  }});
  function apply() {{
    var v=val('fVerdict'), c=val('fCompetition'), i=val('fIndustry'), w=val('fWtp'), s=val('fSource'),
        q=val('fSearch').toLowerCase(), n=document.getElementById('fNew').checked;
    var shown=0;
    rows.forEach(function(r){{
      var ok = (!v||r.dataset.verdict===v) && (!c||r.dataset.competition===c) && (!i||r.dataset.industry===i)
            && (!w||r.dataset.wtp===w) && (!s||r.dataset.source===s) && (!n||r.dataset.new==='1')
            && (!q||r.dataset.search.indexOf(q)>-1);
      r.style.display = ok ? '' : 'none';
      if(ok) shown++;
    }});
    document.getElementById('count').textContent = shown + ' / ' + rows.length + ' shown';
  }}
  function val(id){{ return document.getElementById(id).value; }}
  apply();
</script>
</body></html>"""


def write_and_open(store, run_stats, out_path, open_browser):
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(store, run_stats), encoding="utf-8")
    print(f"Dashboard: {out.resolve()}")
    if open_browser:
        webbrowser.open(out.resolve().as_uri())


# =============================================================================
# Main
# =============================================================================


def main():
    ap = argparse.ArgumentParser(description="Personal economy-wide pain-discovery scanner.")
    ap.add_argument("--limit", type=int, default=MAX_ITEMS, help="max NEW items to classify this run")
    ap.add_argument("--sample", action="store_true", help="tiny source subset for a fast smoke test")
    ap.add_argument("--dry-run", action="store_true", help="fetch + prefilter only, no API cost")
    ap.add_argument("--no-open", action="store_true", help="do not open the dashboard in a browser")
    ap.add_argument("--no-web-search", action="store_true", help="competition check uses model knowledge only (no live web search)")
    ap.add_argument("--out", default="report.html", help="dashboard output path")
    ap.add_argument("--data", default=DATA_PATH, help="cross-run memory JSON path")
    args = ap.parse_args()

    store = load_store(args.data)

    print("Fetching...")
    raw = []
    for name, fn in [("hackernews", lambda: fetch_hn(HN_QUERIES[:1] if args.sample else HN_QUERIES)),
                     ("reddit", lambda: fetch_reddit(INDUSTRIES, sample=args.sample))]:
        try:
            got = fn()
            print(f"  fetched {len(got)} from {name}")
            raw.extend(got)
        except Exception as e:
            print(f"  [{name}] source failed entirely: {e}", file=sys.stderr)

    kept = prefilter(raw, args.limit + 500)  # prefilter generously; split new/seen next
    # split against memory: classify only URLs we've never scored; bump the rest
    new_items = [it for it in kept if it["url"] not in store["findings"]][:args.limit]
    seen_again = [it["url"] for it in kept if it["url"] in store["findings"]]
    print(f"Prefiltered {len(raw)} -> {len(kept)} candidates; {len(new_items)} new, {len(seen_again)} already known.")

    if args.dry_run:
        for it in new_items[:15]:
            print(f"  [{it['source_type']}] {it['created']} {it['where']}: {it['title'][:70]}")
        print("\n(dry run: no classification, no dashboard)")
        return

    now = _now()
    bump_seen(store, seen_again, now)

    results = []
    if new_items:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            sys.exit("ANTHROPIC_API_KEY not set. export it and retry (or use --dry-run).")
        import anthropic
        client = anthropic.Anthropic()   # one client, shared across classify + competition threads
        print(f"Classifying {len(new_items)} new items with {MODEL}...")
        results = classify_all(client, new_items)
        attach_competition(client, results, use_web=not args.no_web_search)
        merge_new(store, results, now)
    else:
        print("No new items to classify.")

    store["_meta"]["last_run"] = now
    save_store(args.data, store)
    run_stats = {"fetched": len(raw), "new_count": len(results),
                 "new_urls": {r["source_url"] for r in results}}
    write_and_open(store, run_stats, args.out, not args.no_open)
    print(f"Memory: {Path(args.data).resolve()} ({len(store['findings'])} findings)")


if __name__ == "__main__":
    main()
