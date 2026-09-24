"""Tests for feedback.py (§15) and the rating control's rendered wiring.

Weighted toward the two limits the module promises, because those are the ones
a later "make it more responsive to ratings" edit would quietly remove: a
single thumbs-down does not suppress, and nothing is ever suppressed outright.
"""

import os
import re
import unittest

import feedback as F
import render_briefing as R

HERE = os.path.dirname(os.path.abspath(__file__))
TPL = os.path.join(HERE, "briefing_artifact_template.html")


def campus(iid, slug="resume-lab-drop-in", org="University Career Center"):
    return {"id": iid, "organizer": org,
            "source_refs": [{"kind": "umd_calendar",
                             "url": "https://calendar.umd.edu/event/%s" % slug}]}


def docs(*triples):
    """(doc_id, rating, family) -> the shape the db collection returns."""
    return {d: {"rating": r, "family": f, "organizer": o}
            for d, r, f, o in triples}


class FamilyTest(unittest.TestCase):

    def test_campus_events_key_on_the_calendar_slug_family(self):
        fam = F.family_of(campus("campus-resume-lab-2026-09-15"))
        self.assertTrue(fam.startswith("campus:"))

    def test_two_instances_of_one_series_share_a_family(self):
        """The whole point: rating one instance affects the next."""
        a = F.family_of(campus("campus-resume-lab-2026-09-15"))
        b = F.family_of(campus("campus-resume-lab-2026-10-20"))
        self.assertEqual(a, b)

    def test_non_campus_items_fall_back_to_the_ledger_family(self):
        fam = F.family_of({"id": "math001-quiz1-20260910"})
        self.assertEqual(fam, "item:math001-quiz1")

    def test_no_id_and_no_source_yields_no_key(self):
        self.assertIsNone(F.family_of({}))

    def test_organizer_is_normalised(self):
        self.assertEqual(F.organizer_of({"organizer": "  Career Center "}),
                         "org:career center")
        self.assertIsNone(F.organizer_of({"organizer": ""}))


class AggregateTest(unittest.TestCase):

    def test_counts_per_key(self):
        t = F.aggregate(docs(("d1", "up", "campus:x", "org:a"),
                             ("d2", "up", "campus:x", "org:b")))
        self.assertEqual(t["campus:x"], {"up": 2, "down": 0})
        self.assertEqual(t["org:a"], {"up": 1, "down": 0})

    def test_a_malformed_rating_is_ignored_not_guessed(self):
        """The store is user-writable; a bad value is not an opinion."""
        t = F.aggregate(docs(("d1", "maybe", "campus:x", ""),
                             ("d2", None, "campus:x", "")))
        self.assertEqual(t, {})

    def test_a_cleared_rating_disappears(self):
        """Clicking the lit thumb writes rating: null — it must not linger."""
        t = F.aggregate({"d1": {"rating": None, "family": "campus:x"}})
        self.assertEqual(t, {})


class WeightTest(unittest.TestCase):

    def test_nothing_known_is_neutral(self):
        self.assertEqual(F.weight(campus("a"), {}), 0.0)
        self.assertEqual(F.weight(campus("a"), None), 0.0)

    def test_an_up_promotes(self):
        fam = F.family_of(campus("a"))
        self.assertGreater(F.weight(campus("a"), {fam: {"up": 1, "down": 0}}), 0)

    def test_one_down_does_NOT_demote(self):
        """MIN_DOWN. One bad instance of a recurring series is ordinary, and
        one click is not a request to delete the series."""
        fam = F.family_of(campus("a"))
        self.assertEqual(F.weight(campus("a"), {fam: {"up": 0, "down": 1}}), 0.0)
        self.assertFalse(F.demoted(campus("a"), {fam: {"up": 0, "down": 1}}))

    def test_two_downs_do_demote(self):
        fam = F.family_of(campus("a"))
        t = {fam: {"up": 0, "down": 2}}
        self.assertLess(F.weight(campus("a"), t), 0)
        self.assertTrue(F.demoted(campus("a"), t))

    def test_weight_is_bounded_in_both_directions(self):
        fam = F.family_of(campus("a"))
        self.assertLessEqual(F.weight(campus("a"), {fam: {"up": 99, "down": 0}}),
                             F.MAX_WEIGHT)
        self.assertGreaterEqual(F.weight(campus("a"), {fam: {"up": 0, "down": 99}}),
                                -F.MAX_WEIGHT)

    def test_demotion_is_not_suppression(self):
        """`demoted` routes to §5.3's `secondary` — the disclosure list — never
        off the page. Silently withholding a plausible thing is the defect the
        2026-09-10 Coming-up change removed; rebuilding it here through
        feedback would be worse, because the reader caused it unknowingly."""
        item = campus("a")
        fam, org = F.family_of(item), F.organizer_of(item)
        worst = {fam: {"up": 0, "down": 99}, org: {"up": 0, "down": 99}}
        # Even at the floor, the verdict is a score and a routing hint. There
        # is no value either function can return that removes the item.
        self.assertEqual(F.weight(item, worst), -F.MAX_WEIGHT)
        self.assertIs(F.demoted(item, worst), True)
        self.assertIsInstance(F.weight(item, worst), float)

    def test_organizer_and_family_both_contribute(self):
        item = campus("a")
        fam, org = F.family_of(item), F.organizer_of(item)
        one = F.weight(item, {fam: {"up": 1, "down": 0}})
        both = F.weight(item, {fam: {"up": 1, "down": 0},
                               org: {"up": 1, "down": 0}})
        self.assertGreater(both, one)


class SummaryTest(unittest.TestCase):

    def test_empty_when_nothing_rated(self):
        self.assertEqual(F.summary({}), "")

    def test_names_what_was_learned(self):
        s = F.summary({"campus:x": {"up": 2, "down": 0},
                       "campus:y": {"up": 0, "down": 3}})
        self.assertIn("2 rated useful", s)
        self.assertIn("3 rated not useful", s)


class RenderedWiringTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tpl = R.read_template(TPL)

    def _row(self, section, item):
        return R.build_row(self.tpl, section, item)

    def test_a_rated_row_carries_both_keys(self):
        item = dict(campus("campus-resume-lab-2026-09-15"),
                    course_class="none", course_label="Campus",
                    title="Résumé Lab", detail="d", expand_context="x",
                    days_out_meta="in 5 days · Mon, Sep 15", times_surfaced=1)
        row = self._row("campus", item)
        self.assertIn('data-family="campus:', row)
        self.assertIn("data-organizer=", row)
        self.assertIn('data-action="rate"', row)

    def test_an_obligation_is_not_rateable(self):
        """§15's control is for SUGGESTIONS. An email-sourced deadline in
        Coming up carried "Worth showing?" until 2026-09-11, which had no
        consumer: family_of() generalises over recurring suggestions."""
        row = self._row("coming_up", {
            "id": "cu-1", "course_class": "umd", "course_label": "UMD",
            "title": "Advising appointment", "detail": "d",
            "days_out_meta": "in 5 days · Mon, Sep 15", "times_surfaced": 1})
        self.assertNotIn('data-action="rate"', row)
        self.assertNotIn("menu-rate", row)
        # The block came out whole: no orphaned label, no stray buttons.
        self.assertNotIn("Worth showing?", row)
        self.assertEqual(row.count("<div"), row.count("</div>"))

    def test_a_demoted_row_keeps_its_thumbs_wherever_it_renders(self):
        row = self._row("coming_up_more", dict(
            campus("campus-x-2026-09-15"), course_class="none",
            course_label="Campus", title="T", detail="d",
            days_out_meta="in 5 days", times_surfaced=1, rateable=True))
        self.assertIn('data-action="rate"', row)

    def test_an_item_with_no_family_gets_no_empty_attribute(self):
        """Injection, not a placeholder: an absent key is an absent attribute
        rather than data-family="" for the client to interpret."""
        item = {"course_class": "none", "course_label": "Campus",
                "title": "Thing", "detail": "d", "expand_context": "x",
                "days_out_meta": "today", "times_surfaced": 1}
        row = self._row("coming_up", item)
        self.assertNotIn('data-family=""', row)

    def test_coursework_rows_carry_no_rating_control(self):
        """Rating a midterm 'not useful' is a judgement about the course, and
        nothing in the pipeline could act on it (§15)."""
        item = {"id": "x", "course_class": "cmsc", "course_label": "CMSC001",
                "title": "Project 2", "detail": "d", "expand_context": "x",
                "days_out_meta": "today", "times_surfaced": 1, "ai_actions": []}
        for section in ("assignments", "assessments", "attention"):
            self.assertNotIn('data-action="rate"',
                             self._row(section, item),
                             "%s should not be rateable" % section)

    def test_leads_use_the_stronger_signal_instead(self):
        """Leads already carry Confirmed / Not real, which says more than a
        thumb. Two overlapping controls would make both ambiguous."""
        seg = self.tpl[self.tpl.index("BEGIN LEAD ROW TEMPLATE"):
                       self.tpl.index("END LEAD ROW TEMPLATE")]
        self.assertNotIn('data-action="rate"', seg)
        self.assertIn('data-action="lead-confirmed"', seg)

    def test_the_writer_lives_with_the_other_db_writes(self):
        """A second writer with its own queue could clobber a concurrent
        read-modify-set; the queue only serialises what shares it."""
        blocks = re.findall(r"<script>.*?</script>", self.tpl, re.S)
        self.assertIn('data-action="rate"', blocks[0])
        self.assertIn("writeQueue", blocks[0])

    def test_clicking_a_lit_thumb_can_clear_it(self):
        """A mis-tap must be retractable, or the ranker learns something the
        reader never meant. Updated 2026-09-16: the client now only predicts
        this optimistically (POST /feedback/<id> makes the real set-vs-clear
        decision server-side, toggling on a repeated rating), so the literal
        line changed from a `var rating = ...` assignment to painting the
        prediction directly -- same behavior, checked via the still-present
        `already ? null : btn.dataset.rating` expression."""
        self.assertIn("already ? null : btn.dataset.rating", self.tpl)

    def test_ratings_are_read_only_for_the_pipeline(self):
        src = open(os.path.join(HERE, "feedback.py"), encoding="utf-8").read()
        self.assertIn("never writes it", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
