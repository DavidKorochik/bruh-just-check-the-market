"""Fast self-checks for the pure logic (scoring, parsing, prefilter balance).

Run either way:
    python test_ask_my_market.py
    pytest -q
No network, no API, no framework required.
"""

import ask_my_market as m


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


def test_load_store_tolerates_missing_and_corrupt(tmp_path=None):
    import tempfile, os as _os
    d = tempfile.mkdtemp()
    missing = _os.path.join(d, "nope.json")
    s = m.load_store(missing)
    assert s == {"_meta": {}, "findings": {}}
    bad = _os.path.join(d, "bad.json")
    open(bad, "w").write("{not json")
    s = m.load_store(bad)   # corrupt -> fresh, never crashes
    assert s["findings"] == {}


def test_safe_href_blocks_dangerous_schemes():
    assert m._safe_href("https://reddit.com/x") == "https://reddit.com/x"
    assert m._safe_href("http://news.ycombinator.com/item?id=1") == "http://news.ycombinator.com/item?id=1"
    assert m._safe_href("javascript:alert(1)") == ""      # XSS scheme -> dropped
    assert m._safe_href("data:text/html,<script>") == ""  # data scheme -> dropped
    assert m._safe_href("  JavaScript:alert(1)") == ""     # trimmed + case-insensitive
    assert m._safe_href("") == "" and m._safe_href(None) == ""


def _store(*recs):
    return {"_meta": {}, "findings": {r["source_url"]: r for r in recs}}


def test_render_drops_unsafe_link_but_keeps_row():
    rec = {"verdict": "worth_a_call", "fit_score": 90, "wtp_tier": "paying_a_human",
           "industry": "accounting", "pain": "p", "quote": "q", "where_they_gather": "r/x",
           "source_url": "javascript:alert(document.cookie)", "source_type": "reddit", "times_seen": 1}
    out = m.render_html(_store(rec), {"fetched": 1, "new_count": 1, "new_urls": set()})
    assert "javascript:alert" not in out   # never reaches the HTML
    assert "worth_a_call" in out           # row still rendered


def test_render_escapes_script_in_scraped_text():
    rec = {"verdict": "skip", "fit_score": 10, "wtp_tier": "just_complaining", "industry": "x",
           "pain": "</td></tr><script>alert(1)</script>", "quote": "q", "where_they_gather": "w",
           "source_url": "https://reddit.com/x", "source_type": "reddit", "times_seen": 1}
    out = m.render_html(_store(rec), {"fetched": 1, "new_count": 0, "new_urls": set()})
    assert "<script>alert(1)</script>" not in out   # escaped, not injected


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


def test_render_shows_competition_and_filters():
    rec = {"verdict": "worth_a_call", "fit_score": 90, "wtp_tier": "paying_a_human", "industry": "legal",
           "pain": "p", "quote": "q", "where_they_gather": "r/law", "times_seen": 1,
           "source_url": "https://reddit.com/x", "source_type": "reddit",
           "competition_level": "saturated", "competitors": ["Clio - practice mgmt", "MyCase"],
           "comp_rationale": "many strong incumbents", "comp_confidence": "high", "comp_sanity": "search agrees"}
    out = m.render_html(_store(rec), {"fetched": 1, "new_count": 0, "new_urls": set()})
    assert 'data-competition="saturated"' in out       # filterable
    assert "Clio - practice mgmt" in out               # competitor visible (in tooltip)
    assert 'id="fCompetition"' in out                  # filter control present


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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} checks passed")
