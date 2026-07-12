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


def test_safe_href_blocks_dangerous_schemes():
    assert m._safe_href("https://reddit.com/x") == "https://reddit.com/x"
    assert m._safe_href("http://news.ycombinator.com/item?id=1") == "http://news.ycombinator.com/item?id=1"
    assert m._safe_href("javascript:alert(1)") == ""      # XSS scheme -> dropped
    assert m._safe_href("data:text/html,<script>") == ""  # data scheme -> dropped
    assert m._safe_href("  JavaScript:alert(1)") == ""     # trimmed + case-insensitive
    assert m._safe_href("") == "" and m._safe_href(None) == ""


def test_render_drops_unsafe_link_but_keeps_row():
    rec = {"verdict": "worth_a_call", "fit_score": 90, "wtp_tier": "paying_a_human",
           "pain": "p", "quote": "q", "where_they_gather": "r/x",
           "source_url": "javascript:alert(document.cookie)", "source_type": "reddit"}
    out = m.render_html([rec], 1, 1)
    assert "javascript:alert" not in out   # never reaches the HTML
    assert "worth_a_call" in out           # row still rendered


def test_prefilter_dedups_and_balances():
    body = "a body with plenty of characters to clear the minimum length filter"
    items = []
    for i in range(5):
        items.append(m.make_item("reddit", f"rurl{i}", f"reddit title {i}", body, f"rurl{i}", "r/x", "2025-01-01"))
    for i in range(5):
        items.append(m.make_item("hackernews", f"hurl{i}", f"hn title {i}", body, f"hurl{i}", "Hacker News", "2026-01-01"))
    items.append(items[0])          # duplicate -> dropped
    items.append(m.make_item("reddit", "tiny", "x", "y", "tiny", "r/x", "2025"))  # too short -> dropped

    kept = m.prefilter(items, limit=6)
    assert len(kept) == 6
    urls = [k["url"] for k in kept]
    assert len(set(urls)) == 6                       # no dupes
    srcs = [k["source_type"] for k in kept]
    assert srcs.count("reddit") == 3 and srcs.count("hackernews") == 3  # balanced


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} checks passed")
