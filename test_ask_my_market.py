"""Fast self-checks for the pure logic (scoring, parsing, prefilter balance, synthesis, render).

Run either way:
    python test_ask_my_market.py
    pytest -q
No network, no API, no framework required.
"""

import os

import ask_my_market as m
import report
import sources
from industries import INDUSTRIES


# =============================================================================
# Scoring + verdicts
# =============================================================================


def test_score_caps_and_floors():
    full = {"wtp_tier": "paying_a_human", "judgment_on_stream": True, "solo_shippable": True,
            "time_to_pay": "weeks", "funded_team_ignores": True, "firsthand_domain": True}
    assert m.compute_score(full) == 100  # 35+25+15+10+10+5 = 100
    assert m.compute_score({"wtp_tier": "just_complaining"}) == 0
    mid = {"wtp_tier": "paying_for_bad_tool", "judgment_on_stream": True}  # 20+25
    assert m.compute_score(mid) == 45


def test_verdict_thresholds():
    assert m.verdict_for(65) == "worth_a_call"
    assert m.verdict_for(64) == "watch"
    assert m.verdict_for(40) == "watch"
    assert m.verdict_for(39) == "skip"


def test_pay_signal_needs_the_edge_to_clear_the_bar():
    # By the spec's own math, paying_a_human ALONE = 35 -> skip. Pay-signal is
    # necessary, not sufficient: it must also fit the operator's edge.
    assert m.compute_score({"wtp_tier": "paying_a_human"}) == 35
    assert m.verdict_for(35) == "skip"
    # add "judgment on a stream" (the edge) and it clears into watch: 35+25 = 60
    with_edge = {"wtp_tier": "paying_a_human", "judgment_on_stream": True}
    assert m.verdict_for(m.compute_score(with_edge)) == "watch"


# =============================================================================
# Model-output parsing + boundary validation
# =============================================================================


def test_extract_json_variants():
    assert m._extract_json('{"a": 1}')["a"] == 1
    assert m._extract_json('```json\n{"a": 2}\n```')["a"] == 2
    assert m._extract_json('here you go: {"a": 3} done')["a"] == 3


def test_coerce_validates_boundary():
    c = m._coerce({"wtp_tier": "bogus", "time_to_pay": "someday", "judgment_on_stream": "yes"})
    assert c["wtp_tier"] == "just_complaining"
    assert c["time_to_pay"] == "unknown"
    assert c["judgment_on_stream"] is True  # truthy string coerced to bool
    assert c["industry"] == "other"          # missing industry -> safe default


def test_coerce_normalizes_industry():
    assert m._coerce({"industry": "  Real Estate "})["industry"] == "real estate"


def test_coerce_competition_validates_and_trims():
    c = m._coerce_competition({"competition_level": "bogus", "competitors": ["A - x"] * 10,
                              "confidence": "sky-high", "rationale": "r", "sanity_check": "s",
                              "junk_key": "should be dropped"})
    assert c["competition_level"] == "not_checked"           # invalid enum -> safe default
    assert len(c["competitors"]) == 6                          # capped at 6
    assert c["comp_confidence"] == "unknown"                   # invalid confidence -> unknown
    assert set(c) == {"competition_level", "competitors", "comp_rationale", "comp_confidence", "comp_sanity"}
    good = m._coerce_competition({"competition_level": "saturated", "competitors": [], "confidence": "high"})
    assert good["competition_level"] == "saturated" and good["comp_confidence"] == "high"


def test_default_competition_shape():
    d = m._default_competition()
    assert d["competition_level"] == "not_checked" and d["competitors"] == []


def test_all_text_joins_blocks_around_tool_use():
    # web search splits the answer across text blocks with tool blocks interleaved;
    # _all_text must join ALL text blocks so the JSON (and cited competitor names) survives
    class Blk:
        def __init__(self, t, text=None):
            self.type = t
            if text is not None:
                self.text = text

    class Msg:
        content = [Blk("text", '{"competition_level":"crow'),
                   Blk("server_tool_use"),
                   Blk("web_search_tool_result"),
                   Blk("text", 'ded","competitors":["Clio"]}')]

    assert m._all_text(Msg()) == '{"competition_level":"crowded","competitors":["Clio"]}'
    assert m._extract_json(m._all_text(Msg()))["competition_level"] == "crowded"


def test_coerce_outreach():
    assert m._coerce_outreach({"comment": "  Hi there, how do you handle X today?  "}) == \
        "Hi there, how do you handle X today?"
    assert m._coerce_outreach({"comment": ""}) is None
    assert m._coerce_outreach({}) is None
    assert m._coerce_outreach(None) is None
    assert len(m._coerce_outreach({"comment": "x" * 2000})) == 700   # capped


# =============================================================================
# Pain-pattern synthesis
# =============================================================================


def _finding(url, industry="accounting", source="reddit", verdict="worth_a_call",
             fit=90, where="r/x", seen=1, pain="p"):
    return {"source_url": url, "industry": industry, "source_type": source, "verdict": verdict,
            "fit_score": fit, "where_they_gather": where, "times_seen": seen, "pain": pain,
            "wtp_tier": "paying_a_human", "quote": "q"}


def test_coerce_patterns_maps_indices_and_validates():
    inputs = [_finding("https://a"), _finding("https://b", source="github", where="gh/repo"),
              _finding("https://c")]
    findings = {f["source_url"]: f for f in inputs}
    raw = {"patterns": [
        {"name": "Manual reconciliation grind", "pain_summary": "hours lost weekly",
         "who_hurts": "bookkeepers", "evidence": [1, 2, 99, "x", 2],   # 99/str/dup -> dropped
         "product_direction": "agent that reconciles", "discovery_next_step": "ask 3 bookkeepers"},
        {"name": "", "pain_summary": "nameless -> dropped", "evidence": [1]},
        {"pain_summary": "no name key -> dropped"},
    ]}
    out = m._coerce_patterns(raw, inputs, findings)
    assert len(out) == 1
    p = out[0]
    assert p["evidence"] == ["https://a", "https://b"]           # 1-based, deduped, validated
    assert p["spread"] == "2 findings / 2 communities / 2 sources"  # computed in Python, not trusted
    assert p["product_direction"] == "agent that reconciles"


def test_coerce_patterns_caps_count():
    inputs = [_finding(f"https://{i}") for i in range(3)]
    findings = {f["source_url"]: f for f in inputs}
    raw = {"patterns": [{"name": f"p{i}", "pain_summary": "s", "evidence": [1]} for i in range(20)]}
    assert len(m._coerce_patterns(raw, inputs, findings)) == m.PATTERNS_MAX


def test_coerce_patterns_rejects_bools_and_coerces_numeric_strings():
    # bool IS an int in Python: a model emitting `true` must not alias finding #1. But "2" and 3.0
    # are honest indices models do emit - accept them.
    inputs = [_finding("https://a"), _finding("https://b"), _finding("https://c")]
    findings = {f["source_url"]: f for f in inputs}
    raw = {"patterns": [{"name": "n", "pain_summary": "s", "evidence": [True, False, "2", 3.0]}]}
    out = m._coerce_patterns(raw, inputs, findings)
    assert out[0]["evidence"] == ["https://b", "https://c"]   # bools dropped, "2" and 3.0 accepted


def test_synthesize_patterns_nonfatal_keeps_previous():
    # synthesis failure must never kill the run - the table alone is still valuable
    class _msgs:
        @staticmethod
        def create(**kw):
            raise RuntimeError("model down")

    client = type("C", (), {"messages": _msgs()})()
    prev = {"generated_at": "2026-01-01", "items": [{"name": "old", "pain_summary": "s"}]}
    store = {"_meta": {}, "patterns": prev,
             "findings": {f"https://{i}": _finding(f"https://{i}") for i in range(4)}}
    m.synthesize_patterns(client, store, "2026-07-15T00:00:00+00:00")
    assert store["patterns"] is prev          # previous conclusion kept, no crash


def test_pattern_inputs_high_fit_only_and_capped():
    store = {"findings": {}}
    for i in range(60):
        store["findings"][f"https://{i}"] = _finding(f"https://{i}", fit=100 - i,
                                                     verdict="worth_a_call" if i < 50 else "skip")
    inputs = m._pattern_inputs(store)
    assert len(inputs) == m.PATTERNS_INPUT_MAX
    assert all(r["verdict"] in m.HIGH_FIT_VERDICTS for r in inputs)
    assert inputs[0]["fit_score"] == 100      # strongest first (stable index mapping)


# =============================================================================
# Persistence
# =============================================================================


def test_persistence_merge_and_bump():
    store = {"_meta": {}, "findings": {}}
    rec = {"source_url": "https://reddit.com/a", "fit_score": 70, "verdict": "worth_a_call"}
    m.merge_new(store, [rec], "2026-01-01T00:00:00+00:00")
    got = store["findings"]["https://reddit.com/a"]
    assert got["times_seen"] == 1 and got["first_seen"] == got["last_seen"]
    # same url surfaces again a later run -> frequency signal grows, first_seen preserved
    m.bump_seen(store, ["https://reddit.com/a"], "2026-01-03T00:00:00+00:00")
    got = store["findings"]["https://reddit.com/a"]
    assert got["times_seen"] == 2
    assert got["first_seen"] == "2026-01-01T00:00:00+00:00"
    assert got["last_seen"] == "2026-01-03T00:00:00+00:00"


def test_load_store_tolerates_missing_and_corrupt():
    import tempfile
    d = tempfile.mkdtemp()
    missing = os.path.join(d, "nope.json")
    s = m.load_store(missing)
    assert s == {"_meta": {}, "findings": {}}
    bad = os.path.join(d, "bad.json")
    open(bad, "w").write("{not json")
    s = m.load_store(bad)   # corrupt -> fresh, never crashes
    assert s["findings"] == {}


# =============================================================================
# Render (report.py)
# =============================================================================


def _store(*recs, patterns=None):
    s = {"_meta": {}, "findings": {r["source_url"]: r for r in recs}}
    if patterns:
        s["patterns"] = patterns
    return s


def test_safe_href_blocks_dangerous_schemes():
    assert report._safe_href("https://reddit.com/x") == "https://reddit.com/x"
    assert report._safe_href("http://news.ycombinator.com/item?id=1") == "http://news.ycombinator.com/item?id=1"
    assert report._safe_href("javascript:alert(1)") == ""      # XSS scheme -> dropped
    assert report._safe_href("data:text/html,<script>") == ""  # data scheme -> dropped
    assert report._safe_href("  JavaScript:alert(1)") == ""     # trimmed + case-insensitive
    assert report._safe_href("") == "" and report._safe_href(None) == ""


def test_render_drops_unsafe_link_but_keeps_row():
    rec = {"verdict": "worth_a_call", "fit_score": 90, "wtp_tier": "paying_a_human",
           "industry": "accounting", "pain": "p", "quote": "q", "where_they_gather": "r/x",
           "source_url": "javascript:alert(document.cookie)", "source_type": "reddit", "times_seen": 1}
    out = report.render_html(_store(rec), {"fetched": 1, "new_count": 1, "new_urls": set()})
    assert "javascript:alert" not in out   # never reaches the HTML
    assert "worth_a_call" in out           # row still rendered


def test_render_escapes_script_in_scraped_text():
    rec = {"verdict": "skip", "fit_score": 10, "wtp_tier": "just_complaining", "industry": "x",
           "pain": "</td></tr><script>alert(1)</script>", "quote": "q", "where_they_gather": "w",
           "source_url": "https://reddit.com/x", "source_type": "reddit", "times_seen": 1}
    out = report.render_html(_store(rec), {"fetched": 1, "new_count": 0, "new_urls": set()})
    assert "<script>alert(1)</script>" not in out   # escaped, not injected


def test_render_shows_competition_and_filters():
    rec = {"verdict": "worth_a_call", "fit_score": 90, "wtp_tier": "paying_a_human", "industry": "legal",
           "pain": "p", "quote": "q", "where_they_gather": "r/law", "times_seen": 1,
           "source_url": "https://reddit.com/x", "source_type": "reddit",
           "competition_level": "saturated", "competitors": ["Clio - practice mgmt", "MyCase"],
           "comp_rationale": "many strong incumbents", "comp_confidence": "high", "comp_sanity": "search agrees"}
    out = report.render_html(_store(rec), {"fetched": 1, "new_count": 0, "new_urls": set()})
    assert 'data-competition="saturated"' in out       # filterable
    assert "Clio - practice mgmt" in out               # competitor visible (in tooltip)
    assert 'id="fCompetition"' in out                  # filter control present


def test_render_source_filter_is_dynamic():
    recs = [dict(_finding("https://a"), source_type="bluesky"),
            dict(_finding("https://b"), source_type="github")]
    out = report.render_html(_store(*recs), {"fetched": 2, "new_count": 0, "new_urls": set()})
    assert '<option value="bluesky">' in out           # new sources appear without touching report.py
    assert '<option value="github">' in out


def test_render_patterns_section_and_escaping():
    pat = {"generated_at": "2026-07-15T06:00:00", "items": [
        {"name": "<script>alert(1)</script>", "pain_summary": "hours lost to manual reconciliation",
         "who_hurts": "bookkeepers", "evidence": ["https://a"], "spread": "2 findings / 2 communities / 1 sources",
         "product_direction": "reconciliation agent", "discovery_next_step": "interview 3 bookkeepers"}]}
    out = report.render_html(_store(_finding("https://a"), patterns=pat),
                             {"fetched": 1, "new_count": 0, "new_urls": set()})
    assert "Pain patterns" in out
    assert "<script>alert(1)</script>" not in out      # pattern fields escaped
    assert "reconciliation agent" in out
    assert "interview 3 bookkeepers" in out


def test_render_comment_expando():
    rec = dict(_finding("https://a"), outreach_comment="How do you handle this today? <script>x</script>")
    out = report.render_html(_store(rec), {"fetched": 1, "new_count": 0, "new_urls": set()})
    assert "draft discovery reply" in out
    assert "<script>x</script>" not in out             # comment escaped
    assert "copyComment" in out                        # copy button wired
    no_comment = report.render_html(_store(_finding("https://b")),
                                    {"fetched": 1, "new_count": 0, "new_urls": set()})
    assert "toggleComment" not in no_comment.split("<script>")[0]   # no dangling buttons in the table


def test_build_digest_summarizes_run():
    pat = {"generated_at": "2026-07-15", "items": [
        {"name": "Reconciliation grind", "pain_summary": "hours lost", "evidence": ["https://a"],
         "spread": "2 findings / 2 communities / 1 sources", "product_direction": "an agent"}]}
    rec = dict(_finding("https://a"), pain="manual invoice reconciliation")
    digest = report.build_digest(_store(rec, patterns=pat),
                                 {"fetched": 10, "new_count": 1, "new_urls": {"https://a"}})
    assert "Reconciliation grind" in digest
    assert "Possible product: an agent" in digest
    assert "manual invoice reconciliation" in digest   # new worth-a-call listed


# =============================================================================
# Sources (sources.py)
# =============================================================================


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.headers = {}
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code}")


def test_parse_reddit_rss():
    xml = '''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Paying someone to reconcile invoices - worth it?</title>
    <link href="https://www.reddit.com/r/Bookkeeping/comments/abc/x/"/>
    <content type="html">&lt;p&gt;I spend hours every week on this.&lt;/p&gt;</content>
    <id>t3_abc</id>
    <updated>2026-07-12T10:00:00+00:00</updated>
  </entry>
</feed>'''
    items = sources._parse_reddit_rss(xml, "Bookkeeping")
    assert len(items) == 1
    it = items[0]
    assert it["source_type"] == "reddit" and it["where"] == "r/Bookkeeping"
    assert it["url"].endswith("/x/")
    assert "reconcile invoices" in it["title"]
    assert "spend hours" in it["body"].lower()   # HTML stripped + entities unescaped
    assert it["created"] == "2026-07-12"


def test_reddit_token_none_without_creds():
    saved = {k: os.environ.pop(k, None) for k in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET")}
    try:
        assert sources._reddit_token() is None   # no creds -> None, no network call
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_fetch_one_sub_oauth_isolates_queries_and_skips_fallback():
    calls = {"oauth": 0, "rss": 0}

    def fake_oauth(sub, q, token):
        calls["oauth"] += 1
        if q == "boom":
            raise RuntimeError("429 Too Many Requests")
        return [sources.make_item("reddit", f"{sub}-{q}", "t", "b", "u", "r/" + sub, "")]

    def fake_rss(sub):
        calls["rss"] += 1
        return []

    orig = (sources._reddit_oauth_search, sources._reddit_rss, sources.REDDIT_SLEEP)
    sources._reddit_oauth_search, sources._reddit_rss, sources.REDDIT_SLEEP = fake_oauth, fake_rss, 0
    try:
        out = sources._fetch_one_sub("msp", "tok", ["good", "boom", "good2"])
    finally:
        sources._reddit_oauth_search, sources._reddit_rss, sources.REDDIT_SLEEP = orig

    assert calls["oauth"] == 3   # every query attempted (one bad query doesn't abort the loop)
    assert calls["rss"] == 0     # token present -> NO anonymous fallback (would 429 in CI + burn timeout)
    assert len(out) == 2         # partial results from the 2 good queries are kept


def test_fetch_reddit_anon_circuit_breaker():
    # anonymous path from a blocked IP (residential run degrading mid-scan, NOT github CI): every
    # sub returns nothing -> must bail after ANON_GIVEUP, not grind all 166 subs. GITHUB_ACTIONS is
    # cleared so we exercise the breaker, not the CI short-circuit.
    seen = []

    def fake_empty(name, token, queries):
        seen.append(name)
        return []

    ci_saved = os.environ.pop("GITHUB_ACTIONS", None)
    orig_token, orig_one = sources._reddit_token, sources._fetch_one_sub
    sources._reddit_token = lambda: None            # anonymous
    sources._fetch_one_sub = fake_empty
    try:
        out = sources.fetch_reddit(INDUSTRIES)       # 166 subs available
    finally:
        sources._reddit_token, sources._fetch_one_sub = orig_token, orig_one
        if ci_saved is not None:
            os.environ["GITHUB_ACTIONS"] = ci_saved

    assert out == []
    assert len(seen) == sources.ANON_GIVEUP          # stopped early, did NOT touch all 166 subs
    assert len(seen) < 20


def test_fetch_reddit_skips_anonymous_on_github_ci():
    # on GitHub Actions with NO OAuth creds, reddit is skipped ENTIRELY (its IPs are blocked on every
    # anonymous endpoint) - no subs touched, no 30-min backoff grind, other sources carry the run.
    saved = {k: os.environ.pop(k, None) for k in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET")}
    ci_saved = os.environ.get("GITHUB_ACTIONS")
    os.environ["GITHUB_ACTIONS"] = "true"
    called = []
    orig = sources._fetch_one_sub
    sources._fetch_one_sub = lambda *a, **k: called.append(a) or []
    try:
        out = sources.fetch_reddit(INDUSTRIES)       # no creds + CI -> skip before any network/sub
        assert out == []
        assert called == []                          # never touched a single sub
    finally:
        sources._fetch_one_sub = orig
        if ci_saved is None:
            os.environ.pop("GITHUB_ACTIONS", None)
        else:
            os.environ["GITHUB_ACTIONS"] = ci_saved
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_fetch_reddit_oauth_not_circuit_broken():
    # with a token, empty subs must NOT trigger the breaker (OAuth fails fast per-sub already);
    # it should visit every sub in the (sampled) set
    calls = []

    def fake_one(name, token, queries):
        calls.append(name)
        return []

    orig_token, orig_one = sources._reddit_token, sources._fetch_one_sub
    sources._reddit_token = lambda: "tok"
    sources._fetch_one_sub = fake_one
    try:
        sources.fetch_reddit(INDUSTRIES, sample=True)   # 2 sectors x 1 sub = 2 subs
    finally:
        sources._reddit_token, sources._fetch_one_sub = orig_token, orig_one

    assert len(calls) == 2   # visited both sampled subs; no early bail despite all-empty


def test_reddit_row_parses_child_data():
    d = {"title": "t", "selftext": "body", "permalink": "/r/x/comments/1/t/",
         "subreddit": "x", "created_utc": 1700000000}
    it = sources._reddit_row(d)
    assert it["source_type"] == "reddit" and it["where"] == "r/x"
    assert it["url"] == "https://www.reddit.com/r/x/comments/1/t/"
    assert it["title"] == "t" and it["body"] == "body" and it["created"] == "2023-11-14"


def test_has_pay_signal():
    yes = sources.make_item("reddit", "u", "I hired a VA to do this all day", "", "u", "r/x", "")
    also = sources.make_item("reddit", "u", "cute cat", "spend hours categorizing receipts by hand", "u", "r/x", "")
    no = sources.make_item("reddit", "u", "Look at my cat photo", "just a cat, nothing else", "u", "r/x", "")
    assert sources._has_pay_signal(yes) and sources._has_pay_signal(also) and not sources._has_pay_signal(no)


def test_bsky_url_conversion():
    assert sources._bsky_url("at://did:plc:xyz/app.bsky.feed.post/3kabc", "dave.bsky.social") == \
        "https://bsky.app/profile/dave.bsky.social/post/3kabc"


def test_fetch_bluesky_parses_posts():
    payload = {"posts": [
        {"uri": "at://did:plc:xyz/app.bsky.feed.post/3kabc",
         "author": {"handle": "dave.bsky.social"},
         "record": {"text": "paying someone to sort my invoices\nsend help", "createdAt": "2026-07-14T12:00:00Z"}},
        {"uri": "", "author": {"handle": "x"}, "record": {"text": "no uri -> dropped"}},
    ]}
    orig = sources._SESSION.get
    sources._SESSION.get = lambda *a, **k: _FakeResp(payload)
    try:
        items = sources.fetch_bluesky(["q"])
    finally:
        sources._SESSION.get = orig
    assert len(items) == 1
    it = items[0]
    assert it["source_type"] == "bluesky" and it["where"] == "Bluesky"
    assert it["url"] == "https://bsky.app/profile/dave.bsky.social/post/3kabc"
    assert it["title"] == "paying someone to sort my invoices"   # first line stands in for a title
    assert it["created"] == "2026-07-14"


def test_fetch_github_parses_issues():
    payload = {"items": [
        {"html_url": "https://github.com/acme/tool/issues/42",
         "title": "We currently do this manually every week",
         "body": "wish the export handled this", "created_at": "2026-07-10T00:00:00Z"},
    ]}
    saved_tok = os.environ.get("GITHUB_TOKEN")
    os.environ["GITHUB_TOKEN"] = "t"    # token path -> no inter-query sleep in the test
    orig = sources._SESSION.get
    sources._SESSION.get = lambda *a, **k: _FakeResp(payload)
    try:
        items = sources.fetch_github(["q"])
    finally:
        sources._SESSION.get = orig
        if saved_tok is None:
            os.environ.pop("GITHUB_TOKEN", None)
        else:
            os.environ["GITHUB_TOKEN"] = saved_tok
    assert len(items) == 1
    it = items[0]
    assert it["source_type"] == "github" and it["where"] == "acme/tool"   # repo extracted from url
    assert it["url"].endswith("/issues/42") and it["created"] == "2026-07-10"


def test_fetch_github_stops_after_repeated_rate_limits():
    calls = []
    orig_get, orig_sleep = sources._SESSION.get, sources.time.sleep
    saved_tok = os.environ.pop("GITHUB_TOKEN", None)
    sources._SESSION.get = lambda *a, **k: calls.append(1) or _FakeResp({}, status=403)
    sources.time.sleep = lambda *_a, **_k: None
    try:
        items = sources.fetch_github(["q1", "q2", "q3", "q4"])
    finally:
        sources._SESSION.get, sources.time.sleep = orig_get, orig_sleep
        if saved_tok is not None:
            os.environ["GITHUB_TOKEN"] = saved_tok
    assert items == []
    assert len(calls) == 2   # two refusals in a row -> source stopped, not 4 more doomed calls


# =============================================================================
# Classification failure handling (the no-silent-success guarantees)
# =============================================================================


def test_is_auth_error_only_for_401_403():
    class E(Exception):
        def __init__(self, code):
            self.status_code = code
    assert m._is_auth_error(E(401)) and m._is_auth_error(E(403))
    assert not m._is_auth_error(E(429))                    # rate limit is transient, not fatal
    assert not m._is_auth_error(E(500))
    assert not m._is_auth_error(RuntimeError("no status_code attr"))


def test_classify_all_aborts_on_auth_error():
    # a dead API key (401 on every call) must ABORT the whole pass with FatalClassifyError, not
    # silently return [] and let the run deploy stale data as if it just found nothing new.
    class AuthErr(Exception):
        status_code = 401

    class _msgs:
        @staticmethod
        def create(**kw):
            raise AuthErr("invalid x-api-key")

    client = type("C", (), {"messages": _msgs()})()
    items = [m.make_item("reddit", f"u{i}", "t", "some body text here", f"u{i}", "r/x", "")
             for i in range(5)]
    raised = False
    try:
        m.classify_all(client, items)
    except m.FatalClassifyError:
        raised = True
    assert raised, "a 401 on every classify call must raise FatalClassifyError, not return []"


def test_classify_all_isolates_transient_failures():
    # a non-auth error is per-item: retried once, dropped + logged, and the pass returns survivors
    # (here []). NOT fatal - main turns 0-survivors into a nonzero exit, this layer just isolates.
    class _msgs:
        @staticmethod
        def create(**kw):
            raise RuntimeError("transient blip")   # no status_code -> not auth -> not fatal

    client = type("C", (), {"messages": _msgs()})()
    sleep_saved = m.time.sleep
    m.time.sleep = lambda *_a, **_k: None           # skip the 1s inter-attempt backoff in tests
    items = [m.make_item("reddit", f"u{i}", "t", "some body text here", f"u{i}", "r/x", "")
             for i in range(3)]
    try:
        got = m.classify_all(client, items)
    finally:
        m.time.sleep = sleep_saved
    assert got == []   # every item exhausted its retry and was dropped; no exception raised


# =============================================================================
# Prefilter
# =============================================================================


def test_prefilter_drops_items_without_url():
    # url is the identity key downstream (memory dedup, outreach join, dashboard link) - two
    # url-less items would cross-wire outreach drafts and overwrite each other in the store.
    body = "a body with plenty of characters to clear the minimum length filter"
    items = [m.make_item("reddit", "id-only-1", "t1", body, "", "r/a", ""),
             m.make_item("reddit", "id-only-2", "t2", body, "", "r/a", ""),
             m.make_item("reddit", "https://ok", "t3", body, "https://ok", "r/a", "")]
    kept = m.prefilter(items, limit=10)
    assert [k["url"] for k in kept] == ["https://ok"]


def test_attach_outreach_joins_records_to_their_own_items():
    # the draft must be generated from the record's OWN post, not another one's (join by url)
    body = "long enough body text for this test to be meaningful"
    items = [m.make_item("reddit", "https://a", "alpha post", body, "https://a", "r/x", ""),
             m.make_item("reddit", "https://b", "beta post", body, "https://b", "r/x", "")]
    records = [{"source_url": "https://a", "verdict": "worth_a_call", "pain": "p1"},
               {"source_url": "https://b", "verdict": "watch", "pain": "p2"},
               {"source_url": "https://c", "verdict": "worth_a_call", "pain": "no item -> skipped"}]

    def create(**kw):
        content = kw["messages"][0]["content"]
        title = [ln for ln in content.splitlines() if ln.strip().startswith("Title:")][0].split(":", 1)[1].strip()
        blk = type("B", (), {"type": "text", "text": f'{{"comment": "re {title}"}}'})()
        return type("R", (), {"content": [blk], "stop_reason": "end_turn"})()

    client = type("C", (), {"messages": type("M", (), {"create": staticmethod(create)})()})()
    m.attach_outreach(client, records, items)
    assert records[0]["outreach_comment"] == "re alpha post"   # each record got ITS OWN post
    assert records[1]["outreach_comment"] == "re beta post"
    assert "outreach_comment" not in records[2]                # no matching item -> skipped, no crash


def test_ask_competition_joins_text_across_pause_turns():
    # web-search turns can pause mid-answer; text emitted BEFORE the pause is part of the JSON
    responses = [("pause_turn", '{"competition_level":"crow'),
                 ("end_turn", 'ded","competitors":["Clio"],"rationale":"r","confidence":"high","sanity_check":"s"}')]

    def create(**kw):
        stop, text = responses.pop(0)
        blk = type("B", (), {"type": "text", "text": text})()
        return type("R", (), {"content": [blk], "stop_reason": stop})()

    client = type("C", (), {"messages": type("M", (), {"create": staticmethod(create)})()})()
    out = m._ask_competition(client, {"pain": "p", "industry": "i", "source_url": "u"}, use_web=True)
    assert out["competition_level"] == "crowded"               # both halves of the JSON survived


def test_md_neutralizes_markdown_injection():
    # scraped text can try to fabricate links/images/headings inside the GitHub job summary
    assert "[" not in report._md("[click me](https://evil)")
    assert "`" not in report._md("`code`") and "<" not in report._md("<img src=x>")
    assert "\n" not in report._md("line1\n## fake heading")


def test_prefilter_dedups_and_balances_by_community():
    body = "a body with plenty of characters to clear the minimum length filter"
    items = []
    for where, st in [("r/a", "reddit"), ("r/b", "reddit"), ("Hacker News", "hackernews")]:
        for i in range(4):
            u = f"{where}-{i}"
            items.append(m.make_item(st, u, f"{where} title {i}", body, u, where, "2025-01-01"))
    items.append(items[0])          # duplicate -> dropped
    items.append(m.make_item("reddit", "tiny", "x", "y", "tiny", "r/a", "2025"))  # too short -> dropped

    kept = m.prefilter(items, limit=6)
    assert len(kept) == 6
    assert len(set(k["url"] for k in kept)) == 6     # no dupes
    wheres = [k["where"] for k in kept]              # round-robin across 3 communities -> 2 each
    assert wheres.count("r/a") == 2 and wheres.count("r/b") == 2 and wheres.count("Hacker News") == 2


# =============================================================================
# main() end-to-end guards - the no-stale-deploy property, tested on the REAL main()
# =============================================================================


def _drive_main(client_factory, n_items):
    """Run the REAL main() with fake sources + a fake anthropic client, no network. Returns
    (exit_code_or_None, dashboard_written_bool) and restores every global/env/argv patch. This is
    the end-to-end guard: it proves main() aborts BEFORE save_store/write_and_open on a broken run,
    so a dead key or wholesale classify failure can never re-deploy stale data with a green check."""
    import sys, tempfile, types
    d = tempfile.mkdtemp()
    dash = os.path.join(d, "out.html")
    saved = {"fetch_hn": m.fetch_hn, "fetch_reddit": m.fetch_reddit,
             "fetch_github": m.fetch_github, "fetch_bluesky": m.fetch_bluesky,
             "sleep": m.time.sleep, "argv": sys.argv,
             "anthropic": sys.modules.get("anthropic"), "key": os.environ.get("ANTHROPIC_API_KEY")}

    def fake_items():
        return [m.make_item("hackernews", u, "paying a VA to reconcile invoices by hand",
                            "we spend hours on this manually every week", u, "Hacker News", "2026-07-13")
                for u in [f"https://news.ycombinator.com/item?id={i}" for i in range(n_items)]]

    m.fetch_hn = lambda q: fake_items()
    m.fetch_reddit = lambda ind, sample=False: []
    m.fetch_github = lambda q: []
    m.fetch_bluesky = lambda q: []
    m.time.sleep = lambda *a, **k: None                      # skip inter-attempt backoff
    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.Anthropic = client_factory
    sys.modules["anthropic"] = fake_anthropic
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-bogus"
    sys.argv = ["ask_my_market.py", "--no-open", "--no-web-search",
                "--out", dash, "--data", os.path.join(d, "f.json")]
    try:
        code = None
        try:
            m.main()
        except SystemExit as e:
            code = e.code if e.code is not None else 0
        import json as _json
        data_path = os.path.join(d, "f.json")
        store = _json.load(open(data_path)) if os.path.exists(data_path) else {}
        return code, os.path.exists(dash), {"store": store,
                                            "digest": os.path.join(d, "digest.md")}
    finally:
        m.fetch_hn, m.fetch_reddit = saved["fetch_hn"], saved["fetch_reddit"]
        m.fetch_github, m.fetch_bluesky = saved["fetch_github"], saved["fetch_bluesky"]
        m.time.sleep = saved["sleep"]
        sys.argv = saved["argv"]
        if saved["anthropic"] is None:
            sys.modules.pop("anthropic", None)
        else:
            sys.modules["anthropic"] = saved["anthropic"]
        if saved["key"] is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = saved["key"]


class _AuthErr(Exception):
    status_code = 401


def _client(create_fn):
    """Build a fake anthropic client whose messages.create runs create_fn(**kw)."""
    msgs = type("M", (), {"create": staticmethod(create_fn)})()
    return type("C", (), {"messages": msgs})


def test_main_aborts_and_skips_deploy_on_dead_key():
    # THE incident: a dead API key 401s every call -> main must exit nonzero and NOT write/deploy.
    code, written, _ = _drive_main(lambda: _client(lambda **kw: (_ for _ in ()).throw(_AuthErr("invalid x-api-key"))), 6)
    assert isinstance(code, str) and "aborted" in code.lower(), f"expected abort message, got {code!r}"
    assert not written, "dashboard must NOT be written on a dead-key run"


def test_main_aborts_when_whole_batch_fails_nonauth():
    # a meaningful batch (>= MIN_FATAL_BATCH) that classifies to zero is a broken pipeline -> abort.
    code, written, _ = _drive_main(lambda: _client(lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))), 6)
    assert isinstance(code, str) and "0 succeeded" in code, f"expected 0-survivors abort, got {code!r}"
    assert not written, "dashboard must NOT be written when a full batch fails to classify"


def test_main_tolerates_tiny_all_fail_batch():
    # below MIN_FATAL_BATCH, a total failure is bad luck, not a broken pipeline: the run completes
    # and the dashboard is still (re-)rendered from memory (persisting any seen-again bumps).
    code, written, _ = _drive_main(lambda: _client(lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))), 2)
    assert code in (None, 0), f"tiny all-fail batch should not abort, got {code!r}"
    assert written, "a sub-threshold all-fail run should still (re-)write the dashboard"


_CLASSIFY_OK = ('{"pain":"p","quote":"q","industry":"accounting","wtp_tier":"paying_a_human",'
                '"judgment_on_stream":true,"solo_shippable":true,"time_to_pay":"weeks",'
                '"funded_team_ignores":true,"firsthand_domain":true,"where_they_gather":"r/x"}')


def _smart_create(**kw):
    """Route by call site, like the real API would: Haiku -> classification JSON; Sonnet ->
    patterns / outreach / competition JSON by prompt shape."""
    content = kw["messages"][0]["content"]
    if kw.get("model") == m.MODEL:
        text = _CLASSIFY_OK
    elif "PAIN PATTERNS" in content:
        text = ('{"patterns": [{"name": "Manual reconciliation grind", "pain_summary": "hours lost",'
                '"who_hurts": "ops", "evidence": [1, 2], "product_direction": "an agent",'
                '"discovery_next_step": "interview 3 operators"}]}')
    elif "Draft ONE short reply" in content:
        text = '{"comment": "How do you handle this today?"}'
    else:
        text = ('{"competition_level": "open_field", "competitors": [], "rationale": "r",'
                '"confidence": "high", "sanity_check": "s"}')
    blk = type("B", (), {"type": "text", "text": text})()
    return type("R", (), {"content": [blk], "stop_reason": "end_turn"})()


def test_main_deploys_on_healthy_classify():
    code, written, extra = _drive_main(lambda: _client(_smart_create), 6)
    assert code in (None, 0), f"healthy run should exit clean, got {code!r}"
    assert written, "healthy run must write the dashboard"


def test_main_runs_synthesis_outreach_and_digest():
    # the run's CONCLUSION actually happens: patterns persisted, discovery replies attached to the
    # high-fit findings, and the digest written next to --out (the scan.yml job-summary contract).
    code, written, extra = _drive_main(lambda: _client(_smart_create), 6)
    assert code in (None, 0) and written
    store = extra["store"]
    pats = store.get("patterns", {}).get("items", [])
    assert pats and pats[0]["name"] == "Manual reconciliation grind"
    assert len(pats[0]["evidence"]) == 2                    # index-mapped to real finding urls
    assert all(r.get("outreach_comment") == "How do you handle this today?"
               for r in store["findings"].values())         # all 6 are high-fit in this fake
    assert os.path.exists(extra["digest"]), "digest.md must land next to --out for the CI job summary"
    digest = open(extra["digest"]).read()
    assert "Manual reconciliation grind" in digest


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} checks passed")
