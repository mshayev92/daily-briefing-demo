"""2026-10-08: the What changed eyebrow shows when Canvas was last checked.

Run (from service/):
    ../venv/bin/python -m pytest test_changes_range.py -q
"""

import datetime as _dt
import sqlite3

import digest
import orchestrator as o

TODAY = _dt.date(2026, 10, 8)
WED_632 = _dt.datetime(2026, 10, 7, 6, 32, tzinfo=o.TZ).timestamp()
THU_2147 = _dt.datetime(2026, 10, 8, 21, 47, tzinfo=o.TZ).timestamp()


class Run:
    def __init__(self, since):
        self.digest_baseline = {"since": since}


def make_cache(path, runs):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE run_log (run_at REAL)")
    conn.executemany("INSERT INTO run_log VALUES (?)", [(r,) for r in runs])
    conn.commit()
    conn.close()


def test_last_checked_is_latest_scrape_run(tmp_path):
    cache = str(tmp_path / "cache.db")
    make_cache(cache, [WED_632, THU_2147])
    assert digest.last_checked(cache) == THU_2147


def test_last_checked_none_without_cache(tmp_path):
    assert digest.last_checked(str(tmp_path / "missing.db")) is None


def test_range_shows_start_and_latest_check(tmp_path, monkeypatch):
    cache = str(tmp_path / "cache.db")
    make_cache(cache, [WED_632, THU_2147])
    monkeypatch.setattr(o.canvas_shadow, "CANVAS_CACHE_DB", cache)
    text = o._changes_range(Run(WED_632), TODAY)
    assert text == "since Wed 6:32 AM · Canvas checked 9:47 PM"


def test_range_same_day_start_has_no_weekday(tmp_path, monkeypatch):
    cache = str(tmp_path / "cache.db")
    make_cache(cache, [THU_2147])
    monkeypatch.setattr(o.canvas_shadow, "CANVAS_CACHE_DB", cache)
    text = o._changes_range(Run(THU_2147), TODAY)
    assert text == "since 9:47 PM · Canvas checked 9:47 PM"


def test_range_without_cache_keeps_start_only(tmp_path, monkeypatch):
    monkeypatch.setattr(o.canvas_shadow, "CANVAS_CACHE_DB",
                        str(tmp_path / "missing.db"))
    assert o._changes_range(Run(WED_632), TODAY) == "since Wed 6:32 AM"
