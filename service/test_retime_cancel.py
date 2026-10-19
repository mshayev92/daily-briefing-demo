"""Relative-date rewording (rule 2) and cancellation marking (rule 1)."""
import sys
import unittest
from datetime import date
from zoneinfo import ZoneInfo

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import cancellations
import relative_dates as rd

MON, TUE, WED = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)
TZ = ZoneInfo("America/New_York")


class Retime(unittest.TestCase):
    def test_tomorrow_becomes_today(self):
        self.assertEqual(rd.retime("Review is tomorrow.", MON, TUE),
                         "Review is today.")
        self.assertEqual(rd.retime("Tomorrow's review", MON, TUE),
                         "Today's review")

    def test_two_days_on(self):
        self.assertEqual(rd.retime("review is tomorrow", MON, WED),
                         "review is yesterday")
        self.assertEqual(rd.retime("due by tomorrow", MON, WED),
                         "due by yesterday")

    def test_today_and_tonight(self):
        self.assertEqual(rd.retime("Quiz today. Tonight: study", MON, TUE),
                         "Quiz yesterday. Last night: study")

    def test_weekday_phrases(self):
        self.assertEqual(rd.retime("due this Friday", MON, WED),
                         "due this Friday")
        self.assertEqual(rd.retime("due this Friday", MON, date(2026, 10, 12)),
                         "due last Friday")
        self.assertEqual(rd.retime("meet next Tuesday", MON,
                                   date(2026, 10, 12)), "meet tomorrow")
        self.assertEqual(rd.retime("in 3 days", MON, TUE), "this Thursday")

    def test_parts_of_day(self):
        self.assertEqual(rd.retime("see you tomorrow morning", MON, TUE),
                         "see you this morning")

    def test_explicit_date_left_alone_and_same_day_noop(self):
        s = "GSS Today - Wednesday 9/16"
        self.assertEqual(rd.retime(s, MON, TUE), s)
        self.assertEqual(rd.retime("due tomorrow", MON, MON), "due tomorrow")

    def test_items_keep_original_and_never_compound(self):
        it = {"detail": "Review is tomorrow", "text_anchor": "2026-10-05"}
        rd.retime_items([it], TUE)
        self.assertEqual(it["detail"], "Review is today")
        rd.retime_items([it], TUE)          # same day again
        self.assertEqual(it["detail"], "Review is today")
        rd.retime_items([it], WED)          # next day: from the original
        self.assertEqual(it["detail"], "Review is yesterday")
        self.assertEqual(it["retime"]["orig"]["detail"], "Review is tomorrow")

    def test_edited_text_is_not_clobbered(self):
        it = {"detail": "Quiz tomorrow", "text_anchor": "2026-10-05"}
        rd.retime_items([it], TUE)
        it["detail"] = "Exam tomorrow"      # a later email, sent Wednesday
        it["text_anchor"] = "2026-10-07"
        rd.retime_items([it], WED)
        self.assertEqual(it["detail"], "Exam tomorrow")

    def test_title_untouched(self):
        it = {"title": "Review tomorrow", "first_seen": "2026-10-05"}
        rd.retime_items([it], TUE)
        self.assertEqual(it["title"], "Review tomorrow")


class FakeApi:
    def __init__(self, ok=True, events=()):
        self.ok, self.events, self.marked = ok, list(events), []

    def write_ok(self):
        return self.ok

    def event_refs(self, cal, tmin, tmax):
        return [e for e in self.events if e["cal"] == cal]

    def mark(self, cal, event_id):
        self.marked.append((cal, event_id))


class Cancel(unittest.TestCase):
    def setUp(self):
        self.cals = {"class": "C", "personal": "P"}
        self.label = lambda e: e.get("label", "UMD")
        self.logs = []

    def item(self, **kw):
        return {"id": "x", "title": "MATH001 discussion", "course_label":
                "MATH001", "date": "2026-10-06", "kind": "event",
                "cancellation": True, **kw}

    def go(self, items, api):
        return cancellations.apply(items, MON, TZ, self.cals, self.label,
                                   api, self.logs.append)

    def test_marks_matching_class_event_never_deletes(self):
        api = FakeApi(events=[
            {"cal": "C", "id": "e1", "title": "MATH001 Discussion",
             "label": "MATH001"},
            {"cal": "C", "id": "e2", "title": "PHIL001 Lecture",
             "label": "PHIL001"}])
        it = self.item()
        out = self.go([it], api)
        self.assertEqual(api.marked, [("C", "e1")])
        self.assertEqual(out, {"marked": 1, "pending": 0})
        self.assertTrue(it["calendar_marked"])

    def test_already_marked_is_not_remarked(self):
        api = FakeApi(events=[{"cal": "C", "id": "e1", "canceled": True,
                               "title": "CANCELED: MATH001 Discussion",
                               "label": "MATH001"}])
        it = self.item()
        self.go([it], api)
        self.assertEqual(api.marked, [])
        self.assertTrue(it["calendar_marked"])

    def test_no_token_stays_pending_and_marks_db_rows(self):
        other = {"id": "y", "title": "MATH001 discussion section",
                 "course_label": "MATH001", "date": "2026-10-06",
                 "kind": "event"}
        it = self.item()
        out = self.go([it, other], FakeApi(ok=False))
        self.assertEqual(out, {"marked": 0, "pending": 1})
        self.assertIn("cancel_pending", it)
        self.assertTrue(other["cancelled"])
        self.assertTrue(other["title"].startswith("CANCELED: "))

    def test_no_event_yet_is_retried(self):
        out = self.go([self.item()], FakeApi())
        self.assertEqual(out["pending"], 1)

    def test_old_cancellation_ignored(self):
        it = self.item(date="2026-08-01")
        self.assertEqual(self.go([it], FakeApi()),
                         {"marked": 0, "pending": 0})


if __name__ == "__main__":
    unittest.main()


class Timeline(unittest.TestCase):
    def test_canceled_row_kept_in_order_and_flagged(self):
        import orchestrator as o
        rows = [{"time": "1:00 PM", "title": "CMSC001", "course_class": "x",
                 "course_label": "CMSC001"},
                {"gap": "30 min free", "tight": False},
                {"time": "4:00 PM", "title": "Other", "course_class": "x",
                 "course_label": "X"}]
        ev = {"start": "2026-10-05T15:00:00-04:00",
              "end": "2026-10-05T15:50:00-04:00", "title":
              "CANCELED: MATH001 Lecture", "course_class": "m",
              "course_label": "MATH001", "location": ""}
        out = o._with_canceled(rows, [ev])
        self.assertEqual([r.get("title") for r in out if "gap" not in r],
                         ["CMSC001", "CANCELED: MATH001 Lecture", "Other"])
        self.assertTrue(out[2]["canceled"])

    def test_render_shows_canceled_badge_and_not_counted(self):
        import render_briefing as rb
        html = rb._build_schedule([{
            "time": "3:00 PM", "end_time": "3:50 PM", "title":
            "CANCELED: MATH001 Lecture", "course_label": "MATH001",
            "course_class": "m", "location": "", "canceled": True}])
        self.assertIn("is-canceled", html)
        self.assertIn("Canceled</span>", html)
        self.assertNotIn("CANCELED:", html)
