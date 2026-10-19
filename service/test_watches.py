"""Brief preferences -> "Things to watch" (service/watches.py and
orchestrator.step5w_watches). A watch is named by Michael, checked at most
once a day, and a confirmed hit becomes a row of the Things watching section.

Run (from the repo root):
    venv/bin/python -m pytest service/test_watches.py -q
"""

import datetime as _dt

import pytest

import db as dbmod
import llm_cli
import orchestrator as o
import store
import watches

TODAY = _dt.date(2026, 10, 10)


def entry(wid="w1", what="Nikon Z50 on sale", urls=(), active=True):
    return {"id": wid, "what": what, "urls": list(urls), "active": active}


@pytest.fixture
def env(tmp_path):
    path = str(tmp_path / "b.db")
    dbmod.init_db(path)
    return {"db": path, "ctx": {"today": TODAY, "state": {}}}


def save_watches(path, entries):
    store.kv_set(path, watches.STORE_KEY, {"version": 1, "entries": entries})


# --- pure rules -------------------------------------------------------------

def test_clean_drops_blank_rows_and_trims():
    out = watches.clean_entries([entry(what="  Nikon   Z50 "), entry("w2", "  ")])
    assert [e["what"] for e in out] == ["Nikon Z50"]


def test_clean_refuses_bad_id_repeat_and_limits():
    with pytest.raises(ValueError):
        watches.clean_entries([entry("Bad ID!")])
    with pytest.raises(ValueError):
        watches.clean_entries([entry("w1"), entry("w1", "again")])
    with pytest.raises(ValueError):
        watches.clean_entries([entry("w%d" % i) for i in range(13)])
    with pytest.raises(ValueError):
        watches.clean_entries([entry(what="x" * 141)])


def test_clean_requires_https_links():
    with pytest.raises(ValueError):
        watches.clean_entries([entry(urls=["http://bhphoto.com/z50"])])
    out = watches.clean_entries([entry(urls=[" https://bhphoto.com/z50 ", ""])])
    assert out[0]["urls"] == ["https://bhphoto.com/z50"]


def test_clean_keeps_several_pages_and_drops_repeats():
    pages = ["https://a.example/1", "https://b.example/2", "https://a.example/1"]
    out = watches.clean_entries([entry(urls=pages)])
    assert out[0]["urls"] == ["https://a.example/1", "https://b.example/2"]


def test_clean_refuses_more_than_three_pages():
    pages = ["https://x.example/%d" % i for i in range(4)]
    with pytest.raises(ValueError):
        watches.clean_entries([entry(urls=pages)])


def test_clean_ignores_old_single_url_field():
    out = watches.clean_entries([{"id": "w1", "what": "Z50", "url": "https://x.example/"}])
    assert out[0]["urls"] == []


def test_prompt_lists_every_page():
    text = watches.prompt([entry(urls=["https://a.example/1", "https://b.example/2"])])
    assert "(sites: https://a.example/1, https://b.example/2)" in text


def test_due_skips_paused_and_checked_today():
    entries = [entry("a"), entry("b", active=False), entry("c")]
    checked = {"c": TODAY.isoformat()}
    assert [w["id"] for w in watches.due(entries, checked, TODAY)] == ["a"]
    assert [w["id"] for w in watches.due(entries, {}, TODAY)] == ["a", "c"]


def test_hit_is_a_watch_row_not_an_opportunity():
    it = watches.hit(entry("w1"), "On sale for $499 at B&H.",
                     "https://bhphoto.com/z50", TODAY)
    assert it == {"item_id": "watch-w1", "watch_id": "w1",
                  "title": "Nikon Z50 on sale", "finding": "On sale for $499 at B&H.",
                  "url": "https://bhphoto.com/z50", "seen_on": "page",
                  "found_on": "2026-10-10", "checked_on": "2026-10-10"}


def test_hit_drops_non_https_link():
    it = watches.hit(entry("w1"), "Seen.", "javascript:alert(1)", TODAY)
    assert it["url"] == ""


# --- the daily check (orchestrator step) ------------------------------------

class Run(o.Run):
    def __init__(self, db_path, dry_run=False):
        super().__init__(db_path=db_path, dry_run=dry_run)


def fake_call(answer, calls):
    def call(prompt, **kwargs):
        calls.append(prompt)
        if isinstance(answer, Exception):
            raise answer
        return answer, {}
    return call


def test_dry_run_makes_no_call(env, monkeypatch):
    save_watches(env["db"], [entry()])
    calls = []
    monkeypatch.setattr(llm_cli, "call_with_tools", fake_call({}, calls))
    o.step5w_watches(Run(env["db"], dry_run=True), env["ctx"])
    assert calls == [] and store.watch_hits(env["db"]) == []


def test_live_hit_becomes_item_and_is_checked_once(env, monkeypatch):
    save_watches(env["db"], [entry("w1"), entry("w2", "Quiz grade posted")])
    answer = {"results": [
        {"id": "w1", "met": True, "finding": "On sale $499.",
         "url": "https://bhphoto.com/z50"},
        {"id": "w2", "met": False, "finding": "Not posted.", "url": ""}]}
    calls = []
    monkeypatch.setattr(llm_cli, "call_with_tools", fake_call(answer, calls))
    o.step5w_watches(Run(env["db"]), env["ctx"])
    assert [h["item_id"] for h in store.watch_hits(env["db"])] == ["watch-w1"]
    assert len(calls) == 1
    # Same day again: nothing due, no second search, the hit stays.
    o.step5w_watches(Run(env["db"]), env["ctx"])
    assert len(calls) == 1
    assert [h["item_id"] for h in store.watch_hits(env["db"])] == ["watch-w1"]


def test_omitted_watch_counts_as_checked(env, monkeypatch):
    save_watches(env["db"], [entry("w1")])
    run = Run(env["db"])
    monkeypatch.setattr(llm_cli, "call_with_tools",
                        fake_call({"results": [], "failures": ["site down"]}, []))
    o.step5w_watches(run, env["ctx"])
    assert store.watch_hits(env["db"]) == []
    assert any("site down" in e["message"] for e in run.errors)
    assert store.kv_get(env["db"], o.WATCH_CHECK_KEY) == {"w1": TODAY.isoformat()}


def test_failed_search_is_retried_next_build(env, monkeypatch):
    save_watches(env["db"], [entry("w1")])
    run = Run(env["db"])
    monkeypatch.setattr(llm_cli, "call_with_tools", fake_call(
        llm_cli.ClaudeCliError("timeout"), []))
    o.step5w_watches(run, env["ctx"])
    assert store.kv_get(env["db"], o.WATCH_CHECK_KEY) is None
    assert any(e["severity"] == "MAJOR" for e in run.errors)


def test_no_watches_makes_no_call(env, monkeypatch):
    calls = []
    monkeypatch.setattr(llm_cli, "call_with_tools", fake_call({}, calls))
    o.step5w_watches(Run(env["db"]), env["ctx"])
    assert calls == []


# --- several items per watch, and the product pages the check keeps ---------

def test_named_item_gets_its_own_row_id_and_title():
    it = watches.hit(entry("w1", "GT's Kombucha on sale"), "$2.70 for Prime.",
                      "https://www.wholefoodsmarket.com/p", TODAY,
                      name="Peach Paradise")
    assert it["item_id"] == "watch-w1-peach-paradise"
    assert it["title"] == "Peach Paradise: GT's Kombucha on sale"


def test_keep_checked_preserves_found_pages_and_status_on_save():
    stored = [{"id": "w1", "found": [{"name": "Peach", "url": "https://a.example/p"}],
               "status": {"date": "2026-10-10", "notes": ["zip mismatch"]}}]
    out = watches.keep_checked(watches.clean_entries([entry("w1")]), stored)
    assert out[0]["found"] == [{"name": "Peach", "url": "https://a.example/p"}]
    assert out[0]["status"] == {"date": "2026-10-10", "notes": ["zip mismatch"]}


def test_status_notes_are_capped_and_bad_values_read_as_empty():
    assert watches._status("junk") == {}
    assert watches._status({"date": "2026-10-10", "notes": "x"}) == {
        "date": "2026-10-10", "notes": []}
    many = {"date": "2026-10-10", "notes": ["n%d" % i for i in range(9)]}
    assert len(watches._status(many)["notes"]) == watches.NOTES_MAX


def test_page_notes_flags_blocked_site_and_failed_read():
    blocked = watches.page_notes("https://a.example/p", {"error": "net::ERR 403 Forbidden"})
    assert blocked[0].startswith("site blocked the check: https://a.example/p")
    other = watches.page_notes("https://a.example/p", {"error": "Timeout 30000ms"})
    assert other[0].startswith("page not read: https://a.example/p")
    walled = watches.page_notes("https://a.example/p",
                                {"title": "Access Denied", "text": "Sorry"})
    assert walled[0].startswith("site blocked the check")


def test_page_notes_flags_zip_mismatch_and_out_of_stock():
    wf = {"title": "Peach", "text": "Currently unavailable for delivery to 20740. Out of Stock"}
    notes = watches.page_notes("https://www.wholefoodsmarket.com/p", wf)
    assert any("zip 20740, your zip is 20737" in n for n in notes)
    assert any(n.startswith("out of stock or unavailable") for n in notes)
    amazon = {"title": "Peach", "text": "Deliver to Riverdale Park Store, 20737"}
    assert watches.page_notes("https://www.amazon.com/dp/X", amazon) == []


def test_status_record_has_date_and_notes():
    s = watches.status(TODAY, ["no product page yet: add a page to check"])
    assert s == {"date": "2026-10-10", "notes": ["no product page yet: add a page to check"]}


def test_found_drops_bad_rows_and_caps():
    raw = [{"name": "Peach", "url": "http://a.example/p"},
           {"name": "", "url": "https://a.example/q"},
           {"name": "Honeycrisp", "url": "https://a.example/h"}]
    assert watches._found(raw) == [{"name": "Honeycrisp", "url": "https://a.example/h"}]


def test_prompt_shows_known_product_pages_first():
    w = entry("w1")
    w["found"] = [{"name": "Peach Paradise", "url": "https://a.example/p"}]
    assert '"Peach Paradise" https://a.example/p' in watches.prompt([w])


def test_hit_per_item_and_pages_are_stored(env, monkeypatch):
    save_watches(env["db"], [entry("w1", "GT's Kombucha on sale")])
    answer = {"results": [
        {"id": "w1", "item": "Peach Paradise", "met": True,
         "finding": "$2.70 for Prime members.", "url": "https://a.example/p",
         "pages": [{"name": "Peach Paradise", "url": "https://a.example/p"}]},
        {"id": "w1", "item": "Honeycrisp Apple", "met": False,
         "finding": "$4.59 regular.", "url": "",
         "pages": [{"name": "Honeycrisp Apple", "url": "https://a.example/h"}]},
    ], "failures": []}
    monkeypatch.setattr(o.llm_cli, "call_with_tools", fake_call(answer, []))
    o.step5w_watches(Run(env["db"]), env["ctx"])
    assert [h["item_id"] for h in store.watch_hits(env["db"])] == ["watch-w1-peach-paradise"]
    stored = store.kv_get(env["db"], watches.STORE_KEY)["entries"][0]["found"]
    assert {f["name"] for f in stored} == {"Peach Paradise", "Honeycrisp Apple"}


def test_judge_prompt_quotes_page_text_as_data():
    w = entry("w1", "GT's Kombucha")
    text = watches.judge_prompt([(w, [("https://a.example/p", "Was $3.99. $2.70 Prime")])])
    assert "Page https://a.example/p" in text
    assert '"Was $3.99. $2.70 Prime"' in text


def test_judge_verdict_replaces_search_verdict(env, monkeypatch):
    import shared_browser
    save_watches(env["db"], [entry("w1", "GT's Kombucha on sale")])
    store.kv_set(env["db"], o.watches.STORE_KEY, {"version": 1, "entries": [
        dict(entry("w1", "GT's Kombucha on sale"),
             found=[{"name": "Peach Paradise", "url": "https://www.amazon.com/dp/X"}])]})
    search = {"results": [{"id": "w1", "item": "Peach Paradise", "met": False,
                           "finding": "Not seen.", "url": "", "seen_on": "page"}],
              "failures": []}
    judged = {"results": [{"id": "w1", "item": "Peach Paradise", "met": True,
                           "finding": "$2.70 with Prime, today.",
                           "url": "https://www.amazon.com/dp/X", "seen_on": "page"}]}
    monkeypatch.setattr(o.llm_cli, "call_with_tools", fake_call(search, []))
    monkeypatch.setattr(o.llm_cli, "call", fake_call(judged, []))
    monkeypatch.setattr(shared_browser, "read_pages", lambda urls, **kw: {
        "https://www.amazon.com/dp/X": {"text": "Peach Paradise $2.70 with Prime"}})
    o.step5w_watches(Run(env["db"]), env["ctx"])
    rows = store.watch_hits(env["db"])
    assert [h["item_id"] for h in rows] == ["watch-w1-peach-paradise"]
    assert rows[0]["finding"] == "$2.70 with Prime, today."


def test_browser_unavailable_keeps_search_verdict_and_says_so(env, monkeypatch):
    import shared_browser
    run = Run(env["db"])
    monkeypatch.setattr(shared_browser, "read_pages", lambda urls, **kw: (_ for _ in ()).throw(
        shared_browser.BrowserUnavailable("no CDP")))
    results, notes = o._judge_in_shared_browser(run, [dict(entry("w1"), found=[
        {"name": "P", "url": "https://www.amazon.com/dp/X"}])])
    assert results == []
    assert notes["w1"][0].startswith("shared browser not reachable")


def test_page_problems_reach_the_watch_record_and_log(env, monkeypatch):
    import shared_browser
    save_watches(env["db"], [entry("w1", "Peach Paradise"), entry("w2", "Honeycrisp")])
    store.kv_set(env["db"], watches.STORE_KEY, {"version": 1, "entries": [
        dict(entry("w1", "Peach Paradise"),
             found=[{"name": "Peach", "url": "https://www.wholefoodsmarket.com/p"}]),
        entry("w2", "Honeycrisp")]})
    search = {"results": [{"id": "w1", "item": "Peach", "met": False,
                           "finding": "Not seen.", "url": "", "seen_on": "page"}],
              "failures": []}
    monkeypatch.setattr(o.llm_cli, "call_with_tools", fake_call(search, []))
    monkeypatch.setattr(shared_browser, "read_pages", lambda urls, **kw: {
        "https://www.wholefoodsmarket.com/p": {
            "title": "Peach", "text": "Currently unavailable for delivery to 20740"}})
    run = Run(env["db"])
    o.step5w_watches(run, env["ctx"])
    by_id = {e["id"]: e for e in store.kv_get(env["db"], watches.STORE_KEY)["entries"]}
    notes = by_id["w1"]["status"]["notes"]
    assert any("zip 20740, your zip is 20737" in n for n in notes)
    assert any(n.startswith("out of stock") for n in notes)
    assert "no product page yet: add a page to check" in by_id["w2"]["status"]["notes"]
    assert any(n.startswith("no answer from the search") for n in by_id["w2"]["status"]["notes"])
    assert any("[w1] store or zip mismatch" in str(e) for e in run.errors)


def test_out_of_stock_note_dropped_when_same_watch_hits(env, monkeypatch):
    import shared_browser
    save_watches(env["db"], [entry("w1", "Peach Paradise")])
    store.kv_set(env["db"], watches.STORE_KEY, {"version": 1, "entries": [
        dict(entry("w1", "Peach Paradise"),
             found=[{"name": "Peach", "url": "https://www.amazon.com/dp/X"}])]})
    judged = {"results": [{"id": "w1", "item": "Peach", "met": True,
                           "finding": "$2.70 with Prime.",
                           "url": "https://www.amazon.com/dp/X", "seen_on": "page"}]}
    monkeypatch.setattr(o.llm_cli, "call_with_tools", fake_call(
        {"results": [], "failures": []}, []))
    monkeypatch.setattr(o.llm_cli, "call", fake_call(judged, []))
    monkeypatch.setattr(shared_browser, "read_pages", lambda urls, **kw: {
        "https://www.amazon.com/dp/X": {"title": "P", "text": "Only 1 left. Currently unavailable on one size"}})
    o.step5w_watches(Run(env["db"]), env["ctx"])
    assert [h["item_id"] for h in store.watch_hits(env["db"])] == ["watch-w1-peach"]
    notes = store.kv_get(env["db"], watches.STORE_KEY)["entries"][0]["status"]["notes"]
    assert not any(n.startswith("out of stock") for n in notes)


def test_failed_check_is_recorded_on_the_watch(env, monkeypatch):
    save_watches(env["db"], [entry("w1")])
    monkeypatch.setattr(llm_cli, "call_with_tools", fake_call(
        llm_cli.ClaudeCliError("timeout"), []))
    o.step5w_watches(Run(env["db"]), env["ctx"])
    status = store.kv_get(env["db"], watches.STORE_KEY)["entries"][0]["status"]
    assert status["notes"][0].startswith("watch check failed today")
    assert store.kv_get(env["db"], o.WATCH_CHECK_KEY) is None


def test_item_from_search_result_is_marked_open_to_confirm():
    it = watches.hit(entry("w1"), "$2.70 for Prime members.", "https://a.example/p",
                      TODAY, name="Peach Paradise", seen_on="search")
    assert it["finding"] == watches.SEEN_PREFIX + "$2.70 for Prime members."
    assert it["item_id"] == "watch-w1-peach-paradise"


def test_item_default_detail_has_no_prefix():
    it = watches.hit(entry("w1"), "On sale.", "", TODAY)
    assert it["finding"] == "On sale."


def test_hit_keeps_first_sighting_and_clears_when_no_longer_met(env, monkeypatch):
    save_watches(env["db"], [entry("w1")])
    day2 = TODAY + _dt.timedelta(days=1)
    met = {"results": [{"id": "w1", "met": True, "finding": "On sale.", "url": ""}],
           "failures": []}
    monkeypatch.setattr(llm_cli, "call_with_tools", fake_call(met, []))
    o.step5w_watches(Run(env["db"]), env["ctx"])
    o.step5w_watches(Run(env["db"]), {"today": day2, "state": {}})
    row = store.watch_hits(env["db"])[0]
    assert row["found_on"] == TODAY.isoformat()
    assert row["checked_on"] == day2.isoformat()
    gone = {"results": [{"id": "w1", "met": False, "finding": "Full price.", "url": ""}],
            "failures": []}
    monkeypatch.setattr(llm_cli, "call_with_tools", fake_call(gone, []))
    o.step5w_watches(Run(env["db"]), {"today": day2 + _dt.timedelta(days=1), "state": {}})
    assert store.watch_hits(env["db"]) == []


def test_paused_watch_clears_its_hit(env, monkeypatch):
    save_watches(env["db"], [entry("w1")])
    met = {"results": [{"id": "w1", "met": True, "finding": "On sale.", "url": ""}],
           "failures": []}
    monkeypatch.setattr(llm_cli, "call_with_tools", fake_call(met, []))
    o.step5w_watches(Run(env["db"]), env["ctx"])
    save_watches(env["db"], [entry("w1", active=False)])
    o.step5w_watches(Run(env["db"]), env["ctx"])
    assert store.watch_hits(env["db"]) == []
