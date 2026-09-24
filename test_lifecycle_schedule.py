"""Tests for lifecycle.py and schedule.py.

Weighted toward the properties the prose spends paragraphs insisting on — the
substep order, the attention-item expiry exemption, grouping not reordering a
section, the Coming-up drop order — because those are the ones a hand-run got
wrong and the ones a future refactor is most likely to break quietly.
"""

import unittest
from datetime import date, datetime, timedelta

import lifecycle as L
import opportunity as O
import schedule as S

TODAY = date(2026, 9, 10)


def item(iid, kind="assignment", status="new", days=1, **kw):
    d = None if days is None else (TODAY + timedelta(days=days)).isoformat()
    base = {"id": iid, "kind": kind, "status": status, "date": d,
            "times_surfaced": 0, "title": iid,
            # Added 2026-09-16: section_for() now routes assignment/assessment
            # items by course_class (Items 1/2 -- Assignments/Assessments are
            # class-only). "cmsc" is an arbitrary pick from the five tracked
            # classes so every existing test keeps meaning "an ordinary class
            # item" unless it overrides course_class itself.
            "course_class": "cmsc"}
    base.update(kw)
    return base


class SubstepOrderTest(unittest.TestCase):
    """§5.9's ordering is a correctness property, not a preference."""

    def test_sync_runs_before_overdue_so_done_work_is_not_reported_missed(self):
        it = item("a1", days=-2)
        items, rep = L.run([it], TODAY, synced_handled=["a1"])
        self.assertEqual(it["status"], "handled")
        self.assertNotIn("a1", rep["overdue"])
        self.assertFalse(it.get("overdue_flagged"))

    def test_overdue_runs_before_expire_so_a_long_weekend_still_flags(self):
        """4 days past would expire first if the order were reversed."""
        it = item("a2", days=-4)
        items, rep = L.run([it], TODAY)
        self.assertIn("a2", rep["overdue"])
        self.assertEqual(it["status"], "unresolved")
        self.assertNotIn("a2", rep["expired"])

    def test_expire_still_fires_for_non_attention_items(self):
        it = item("e1", kind="event", days=-5)
        items, rep = L.run([it], TODAY)
        self.assertIn("e1", rep["expired"])


class ExpiryExemptionTest(unittest.TestCase):

    def test_attention_items_never_date_expire(self):
        """§3.4: expiring on date deletes the row when it matters most."""
        it = item("x1", days=-30, status="unresolved", overdue_flagged=True)
        L.expire([it], TODAY)
        self.assertEqual(it["status"], "unresolved")

    def test_attention_items_age_out_on_the_surface_counter_instead(self):
        it = item("x2", days=-30, status="new", needs_attention=True,
                  times_surfaced=5)
        out = L.auto_dismiss([it], TODAY)
        self.assertIn("x2", out["dismissed"])


class LeadLifecycleTest(unittest.TestCase):
    """§14.3: a lead is never `in_attention()`, so neither `expire()`'s
    date rule nor `auto_dismiss()`'s counter (both scoped to attention rows)
    ever reach it — `age_leads()` is the only thing that retires one, on
    ITS OWN two rules. Added 2026-09-19 alongside wiring
    `opportunity.age_leads()` in for the first time; before this, an open
    lead with no act_by sat in `items[]` forever."""

    def lead_item(self, iid, **kw):
        base = {"id": iid, "kind": "lead", "regime": "lead", "status": "new",
                "date": None, "act_by": None, "times_surfaced": 0,
                "title": iid, "course_class": "none"}
        base.update(kw)
        return base

    def test_dateless_lead_does_not_date_expire(self):
        it = self.lead_item("lead-1", times_surfaced=0)
        L.expire([it], TODAY)
        self.assertEqual(it["status"], "new")

    def test_lead_past_its_own_act_by_expires(self):
        it = self.lead_item(
            "lead-2", act_by=(TODAY - timedelta(days=1)).isoformat())
        expired = L.age_leads([it], TODAY)
        self.assertEqual(it["status"], "expired")
        self.assertIn("lead-2", expired)
        self.assertIn("act_by", it["expired_reason"])

    def test_lead_surfaced_past_the_tighter_cap_expires(self):
        """§14.3: 3, not §5.7's 5."""
        it = self.lead_item("lead-3", times_surfaced=O.LEAD_MAX_SURFACES)
        L.age_leads([it], TODAY)
        self.assertEqual(it["status"], "expired")

    def test_lead_under_the_cap_with_no_act_by_survives(self):
        it = self.lead_item("lead-4", times_surfaced=O.LEAD_MAX_SURFACES - 1)
        L.age_leads([it], TODAY)
        self.assertEqual(it["status"], "new")

    def test_confirmed_lead_is_left_alone(self):
        it = self.lead_item(
            "lead-5", status="confirmed",
            act_by=(TODAY - timedelta(days=30)).isoformat(),
            times_surfaced=99)
        L.age_leads([it], TODAY)
        self.assertEqual(it["status"], "confirmed")

    def test_run_reports_lead_expired_and_expire_never_touches_it(self):
        it = self.lead_item(
            "lead-6", act_by=(TODAY - timedelta(days=1)).isoformat())
        items, rep = L.run([it], TODAY)
        self.assertIn("lead-6", rep["lead_expired"])
        self.assertNotIn("lead-6", rep["expired"])


class AutoDismissTest(unittest.TestCase):
    """Every fixture here is an ATTENTION row, deliberately.

    The first version of this class was not, and that is how the scope bug
    survived: `item("c1", times_surfaced=6)` is an ordinary assignment, so
    three green tests asserted the behaviour §5.7 does not ask for and nothing
    asserted the behaviour it does.
    """

    def test_changed_item_gets_a_fresh_cycle(self):
        it = item("c1", times_surfaced=6, needs_attention=True)
        out = L.auto_dismiss([it], TODAY, changed_ids=["c1"])
        self.assertEqual(it["times_surfaced"], 1)
        self.assertEqual(it["status"], "new")
        self.assertIn("c1", out["reset"])

    def test_unfixed_config_row_snoozes_instead_of_dying(self):
        """§5.7 exception 2 — a config row is only finished when the config is."""
        it = item("s1", times_surfaced=7, course_label="System",
                  needs_attention=True, config_unfixed=True)
        out = L.auto_dismiss([it], TODAY)
        self.assertEqual(it["status"], "snoozed")
        self.assertEqual(it["times_surfaced"], 0)
        self.assertEqual(it["snooze_until"],
                         (TODAY + timedelta(days=14)).isoformat())
        self.assertIn("s1", out["snoozed"])

    def test_a_fixed_config_row_is_allowed_to_dismiss(self):
        it = item("s2", times_surfaced=7, course_label="System",
                  needs_attention=True)
        out = L.auto_dismiss([it], TODAY)
        self.assertIn("s2", out["dismissed"])

    def test_a_newly_overdue_row_is_not_dismissed_in_the_same_run(self):
        """§5.9 promises one surface; STEP 6 must not spend it on nothing.

        A quiz shown five mornings and then missed arrives at substep 6.3 with
        a full counter. Without the reset in `flag_overdue()` it is flagged at
        6.3 and dismissed at 6.5, and the row Michael was owed never renders.
        """
        # An online quiz Canvas reports as missing -- the case that is still
        # flagged since the 2026-09-19 audit (an in-class assessment with no
        # Canvas verdict is not; see CanvasOverdueTest below).
        it = item("phil001-quiz1", kind="assessment", days=-1,
                  times_surfaced=6,
                  canvas_submission={"settled": False, "missing": True})
        items, rep = L.run([it], TODAY)
        self.assertIn("phil001-quiz1", rep["overdue"])
        self.assertEqual(it["status"], "unresolved")
        self.assertEqual(it["times_surfaced"], 0)
        self.assertEqual(rep["dismissed"], [])
        self.assertTrue(L.full_detail(it, TODAY))

    def test_an_unresolved_row_is_in_scope_without_needs_attention(self):
        """§5.9's overdue rows are the main population this counter retires."""
        it = item("u1", status="unresolved", overdue_flagged=True,
                  times_surfaced=5)
        out = L.auto_dismiss([it], TODAY)
        self.assertIn("u1", out["dismissed"])

    def test_live_coursework_is_never_dismissed_for_being_read(self):
        """§5.7 is scoped to attention rows; §3.4 is why that matters.

        A midterm two weeks out that has been shown five mornings running is
        not finished with — it has a date, it expires on that date, and the
        surface counter must not touch it. Applied unscoped on 2026-09-11 this
        would have dismissed 15 still-live rows.
        """
        mid = item("math001-midterm1", kind="assessment", days=14,
                   times_surfaced=6)
        hw = item("phil001-participation-4", days=7, times_surfaced=9)
        out = L.auto_dismiss([mid, hw], TODAY)
        self.assertEqual(out["dismissed"], [])
        self.assertEqual(mid["status"], "new")
        self.assertEqual(hw["status"], "new")
        self.assertEqual(hw["times_surfaced"], 9)

    def test_the_counter_and_the_date_partition_the_item_set(self):
        """Nothing may be retired by both rules, and nothing by neither.

        `expire()` skips exactly what `auto_dismiss()` covers. If one of the
        two ever stops calling `in_attention()`, this fails.
        """
        attn = item("p1", days=-30, status="new", needs_attention=True,
                    times_surfaced=5)
        plain = item("p2", kind="event", days=-30, times_surfaced=5)
        items = [attn, plain]
        self.assertEqual(L.expire(items, TODAY), ["p2"])
        out = L.auto_dismiss(items, TODAY)
        self.assertEqual(out["dismissed"], ["p1"])


class SnoozeTest(unittest.TestCase):

    def test_expired_snooze_returns_as_new_at_full_detail(self):
        it = item("z1", status="snoozed", times_surfaced=4,
                  snooze_until=(TODAY - timedelta(days=1)).isoformat())
        L.release_snoozes([it], TODAY)
        self.assertEqual(it["status"], "new")
        self.assertEqual(it["times_surfaced"], 0)
        self.assertNotIn("snooze_until", it)

    def test_future_snooze_is_left_alone(self):
        it = item("z2", status="snoozed",
                  snooze_until=(TODAY + timedelta(days=3)).isoformat())
        L.release_snoozes([it], TODAY)
        self.assertEqual(it["status"], "snoozed")


class RoutingTest(unittest.TestCase):

    def test_windows(self):
        self.assertEqual(L.section_for(item("a", days=3), TODAY), "assignments")
        self.assertEqual(
            L.section_for(item("c", kind="assessment", days=15), TODAY),
            "assessments")
        self.assertIsNone(L.section_for(item("e", days=40), TODAY))

    def test_class_item_past_its_own_window_does_not_render(self):
        """Items 1/2, 2026-09-16: Coming up is the "not my classes" view now
        -- a tracked-class item that misses its own window no longer falls
        through to Coming up as a preview, it simply waits until it is
        within window."""
        self.assertIsNone(L.section_for(item("b", days=10), TODAY))
        self.assertIsNone(
            L.section_for(item("d", kind="assessment", days=25), TODAY))

    def test_non_class_item_within_horizon_goes_to_coming_up(self):
        """The other half of the same rule: an item with no course tie
        (course_class outside MY_CLASS_CLASSES) never lands in Assignments/
        Assessments regardless of how soon it is due, and instead surfaces in
        Coming up any time within the 30-day horizon."""
        self.assertEqual(
            L.section_for(item("b2", days=2, course_class="umd"), TODAY),
            "coming_up")
        self.assertEqual(
            L.section_for(item("d2", kind="assessment", days=25,
                               course_class="umd"), TODAY),
            "coming_up")
        self.assertIsNone(
            L.section_for(item("e2", days=40, course_class="umd"), TODAY))

    def test_first_match_wins_terminal_beats_everything(self):
        self.assertIsNone(
            L.section_for(item("f", status="handled", days=1), TODAY))

    def test_evidence_insufficient_routes_coursework_to_attention_only(self):
        self.assertEqual(
            L.section_for(item("g", evidence="insufficient"), TODAY),
            "attention")
        self.assertIsNone(
            L.section_for(item("h", kind="event", evidence="insufficient"),
                          TODAY))

    def test_exclude_is_never_rendered(self):
        self.assertIsNone(L.section_for(item("i", evidence="exclude"), TODAY))

    def test_personal_only_renders_as_a_proposed_override(self):
        self.assertIsNone(L.section_for(item("j", kind="personal"), TODAY))
        self.assertEqual(
            L.section_for(item("k", kind="personal", needs_attention=True),
                          TODAY), "attention")

    def test_no_item_lands_in_two_sections(self):
        items = [item("m%d" % n, days=n) for n in range(0, 25)]
        buckets = L.assign_sections(items, TODAY)
        seen = [i["id"] for v in buckets.values() for i in v]
        self.assertEqual(len(seen), len(set(seen)))


class AiActionsTest(unittest.TestCase):
    """§6.5's earned/never tables. Empty is the common case and is correct."""

    def _assign(self, items):
        b = L.assign_sections(items, TODAY)
        return L.assign_ai_actions(b, TODAY)

    def test_substantial_assessment_past_seven_days_earns_study(self):
        it = item("m1", kind="assessment", days=12, title="Midterm Exam 1")
        self._assign([it])
        self.assertEqual(it["ai_actions"], ["study"])

    def test_assessment_inside_a_week_earns_nothing(self):
        """§6.5: anything inside a week has no plan to make."""
        it = item("m2", kind="assessment", days=5, title="Midterm Exam 2")
        self._assign([it])
        self.assertEqual(it["ai_actions"], [])

    def test_weekly_quiz_earns_nothing_even_far_out(self):
        it = item("q1", kind="assessment", days=15, title="Weekly Quiz 4")
        self._assign([it])
        self.assertEqual(it["ai_actions"], [])

    def test_model_can_force_substantial_for_an_unrevealing_title(self):
        """§6.5's 'or otherwise substantial' stays the model's call."""
        it = item("p1", kind="assessment", days=14, title="Portfolio review",
                  substantial=True)
        self._assign([it])
        self.assertEqual(it["ai_actions"], ["study"])

    def test_attention_row_earns_email(self):
        it = item("a1", days=-2, status="unresolved", overdue_flagged=True,
                  course_label="CMSC001")
        self._assign([it])
        self.assertEqual(it["ai_actions"], ["email"])

    def test_system_row_earns_nothing(self):
        """There is nobody to email about the pipeline's own plumbing."""
        it = item("sys1", days=1, needs_attention=True, course_label="System")
        self._assign([it])
        self.assertEqual(it["ai_actions"], [])

    def test_steps_requires_the_model_flag(self):
        plain = item("s1", days=2, title="Discussion post 3")
        multi = item("s2", days=2, title="Research paper draft",
                     multi_sitting=True)
        self._assign([plain, multi])
        self.assertEqual(plain["ai_actions"], [])
        self.assertEqual(multi["ai_actions"], ["steps"])

    def test_forgetting_the_flag_under_offers_rather_than_offering_filler(self):
        """The failure direction matters: filler looks like a working feature."""
        it = item("s3", days=2, title="Term project — three deliverables")
        self._assign([it])
        self.assertEqual(it["ai_actions"], [])

    def test_coming_up_earns_nothing_whatever_the_tables_say(self):
        """Section overrides the tables (§6.5) — no place for the button."""
        # course_class="umd": since 2026-09-16 (Items 1/2) a tracked-class
        # assessment past its window is simply not rendered, not demoted to
        # Coming up -- this test needs a row that actually LANDS in
        # coming_up, which is now only a non-class item.
        it = item("c1", kind="assessment", days=25, title="Final Exam",
                  substantial=True, course_class="umd")
        b = self._assign([it])
        self.assertEqual(L.section_for(it, TODAY), "coming_up")
        self.assertEqual(it["ai_actions"], [])

    def test_grouped_rows_earn_nothing(self):
        members = [item("g%d" % n, days=n, title="Homework %d" % n,
                        course_label="MATH001", multi_sitting=True)
                   for n in (1, 3, 5)]
        b = L.assign_sections(members, TODAY)
        rows, groups = L.build_groups(b["assignments"], TODAY)
        b["assignments"] = rows
        L.assign_ai_actions(b, TODAY)
        for r in rows:
            self.assertEqual(r.get("ai_actions"), [])

    def test_reproduces_the_live_state_result(self):
        """2026-09-10: 1 of 50 rendered rows earned anything, and it was
        ['study'] on Midterm Exam 1. If a refactor changes that count, this
        test says so before the briefing does."""
        rows = [
            item("mid", kind="assessment", days=12, title="Midterm Exam 1"),
            item("part1", days=1, title="Participation 1"),
            item("read", days=2, title="Reading response"),
            item("cu", kind="event", days=20, title="Career fair"),
            item("sys", days=1, needs_attention=True, course_label="System"),
        ]
        self._assign(rows)
        earned = [r["id"] for r in rows if r["ai_actions"]]
        self.assertEqual(earned, ["mid"])


class ActionTagTest(unittest.TestCase):
    """lifecycle.action_tag() (§16a, 2026-09-18) -- the coursework-labeling
    fix. Reproduces the exact real-brief cases that motivated it: MATH001's
    "Peer Instruction" (Canvas submission_types == ["external_tool"], no
    take-home component) and PHIL001's "Participation N" rows (some with
    submission_types == ["none"]) were rendering with the identical weight
    as a real problem set."""

    def test_canvas_none_submission_type_wins_over_everything_else(self):
        it = item("a1", title="Participation 3", submission_types=["none"])
        self.assertEqual(L.action_tag(it), L.ACTION_TAGS["no_submission"])

    def test_peer_instruction_title_is_in_class_even_without_canvas_data(self):
        """A Gmail-sourced candidate never carries submission_types at all --
        the title keyword is what has to catch this case."""
        it = item("a2", title="Peer Instruction 2 - 3P")
        self.assertEqual(L.action_tag(it), L.ACTION_TAGS["in_class"])

    def test_participation_keyword_is_in_class(self):
        it = item("a3", title="Participation 3", detail="Participation 3")
        self.assertEqual(L.action_tag(it), L.ACTION_TAGS["in_class"])

    def test_ordinary_assignment_defaults_to_submit(self):
        it = item("a4", title="Problem Set 4")
        self.assertEqual(L.action_tag(it), L.ACTION_TAGS["submit"])

    def test_ordinary_assessment_defaults_to_take_it(self):
        it = item("a5", kind="assessment", title="Quiz 3")
        self.assertEqual(L.action_tag(it), L.ACTION_TAGS["take"])

    def test_coming_up_event_earns_no_tag_at_all(self):
        it = item("a6", kind="event", title="Career fair")
        self.assertEqual(L.action_tag(it), "")

    def test_attention_row_earns_no_tag(self):
        it = item("a7", kind="assignment", needs_attention=True,
                  course_label="System", title="Gradescope needs a password")
        # needs_attention doesn't change kind, so this still tags -- the
        # function only ever looks at kind/submission_types/title, matching
        # its own contract (§16a): section membership is not its business.
        self.assertEqual(L.action_tag(it), L.ACTION_TAGS["submit"])


class IsFyiTest(unittest.TestCase):
    """lifecycle.is_fyi() (§16c, 2026-09-18) -- reproduces the real Further
    out review: an application/registration deadline wearing an `event`
    kind must stay visible; a bare announcement should demote."""

    def test_assignment_is_never_fyi(self):
        self.assertFalse(L.is_fyi(item("b1", kind="assignment",
                                       title="Homecoming registration")))

    def test_assessment_is_never_fyi(self):
        self.assertFalse(L.is_fyi(item("b2", kind="assessment", title="Quiz")))

    def test_application_deadline_wearing_event_kind_is_not_fyi(self):
        it = item("b3", kind="event", title="Apex Fund: Quantitative Analyst applications",
                  detail="Rolling review; applications DUE 9/25.")
        self.assertFalse(L.is_fyi(it))

    def test_registration_deadline_is_not_fyi(self):
        it = item("b4", kind="event", title="SGA election registration closes")
        self.assertFalse(L.is_fyi(it))

    def test_bare_announcement_is_fyi(self):
        it = item("b5", kind="event", title="Elevate Town Hall: Elevate Student Update",
                  detail="Town Hall on Workday Student implementation.")
        self.assertTrue(L.is_fyi(it))

    def test_bare_career_fair_listing_is_fyi(self):
        it = item("b6", kind="advising", title="Fall Career & Internship Fair",
                  detail="247 employers attending, per the University Career Center.")
        self.assertTrue(L.is_fyi(it))


class ParkedUmdDatesTest(unittest.TestCase):
    """§7c is off. Both halves of the switch are pinned here."""

    def test_umd_deadline_is_not_rendered(self):
        it = item("umd-1", kind="umd_deadline", days=4)
        self.assertIsNone(L.section_for(it, TODAY))

    def test_the_three_items_already_in_state_stop_rendering(self):
        """Real ids and dates from the 2026-09-10 state file. Turning off only
        the refresh would have left these publishing for four more days."""
        live = [
            item("umd-last-day-to-apply-for-december-2026-grad-2026-09-14",
                 kind="umd_deadline", days=4),
            item("umd-last-day-to-drop-a-course-with-80-refund-2026-09-14",
                 kind="umd_deadline", days=4),
            item("umd-last-day-to-withdraw-from-all-courses-wi-2026-09-14",
                 kind="umd_deadline", days=4),
        ]
        buckets = L.assign_sections(live, TODAY)
        self.assertEqual(sum(len(v) for v in buckets.values()), 0)

    def test_parked_items_are_not_mutated(self):
        """They stay in state untouched so a re-enable needs no re-fetch."""
        it = item("umd-2", kind="umd_deadline", days=4)
        before = dict(it)
        L.assign_sections([it], TODAY)
        self.assertEqual(it, before)

    def test_other_coming_up_kinds_are_unaffected(self):
        self.assertEqual(
            L.section_for(item("ev", kind="event", days=4), TODAY),
            "coming_up")
        self.assertEqual(
            L.section_for(item("ad", kind="advising", days=4), TODAY),
            "coming_up")

    def test_flipping_the_flag_back_on_restores_rendering(self):
        """The re-enable path is one line; this proves it is still wired."""
        L.UMD_DEADLINES_ENABLED = True
        try:
            self.assertEqual(
                L.section_for(item("umd-3", kind="umd_deadline", days=4),
                              TODAY), "coming_up")
        finally:
            L.UMD_DEADLINES_ENABLED = False

    def test_projection_omits_umd_dates_entirely(self):
        import state_io
        state = {"schema_version": 10, "items": [],
                 "umd_dates": [{"title": "x", "date": "2026-09-14"}],
                 "umd_dates_source": {"url": "https://example.invalid"}}
        proj = state_io.for_context(state, TODAY)
        self.assertNotIn("umd_dates", proj)
        # ...but the source block is an ordinary unknown key and is preserved,
        # because nothing may delete it from state.
        self.assertIn("umd_dates_source", proj)


class GroupingTest(unittest.TestCase):

    def test_three_recurring_items_collapse(self):
        items = [item("p%d" % n, days=n, title="Participation %d" % n,
                      course_label="PHIL001") for n in (1, 8, 15)]
        rows, groups = L.build_groups(items, TODAY)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["count"], 3)
        self.assertEqual(len(rows), 1)

    def test_two_do_not_collapse(self):
        items = [item("q%d" % n, days=n, title="Homework %d" % n,
                      course_label="MATH001") for n in (2, 9)]
        rows, groups = L.build_groups(items, TODAY)
        self.assertEqual(groups, [])
        self.assertEqual(len(rows), 2)

    def test_a_group_sorts_by_earliest_date_and_does_not_reorder(self):
        """§5.8: collapsing is a density decision, never a reordering."""
        early = item("early", days=0, title="Essay draft")
        late = item("late", days=20, title="Final paper")
        recurring = [item("r%d" % n, days=n, title="Week %d" % n,
                          course_label="ENGL001") for n in (5, 12, 19)]
        rows, groups = L.build_groups([early] + recurring + [late], TODAY)
        ids = [r.get("group_id") if r.get("is_group") else r["id"]
               for r in rows]
        self.assertEqual(ids[0], "early")
        self.assertEqual(ids[-1], "late")
        self.assertTrue(rows[1].get("is_group"))

    def test_members_keep_their_identity_and_share_a_group_id(self):
        items = [item("t%d" % n, days=n, title="HW %d" % n,
                      course_label="CMSC001") for n in (1, 2, 3)]
        L.build_groups(items, TODAY)
        self.assertEqual(len({i["group_id"] for i in items}), 1)
        self.assertEqual(len({i["id"] for i in items}), 3)

    def test_titles_with_no_number_are_not_grouped(self):
        """Otherwise three unrelated one-offs from one course would collapse."""
        items = [item("u%d" % n, days=n, title="Reading response",
                      course_label="PHIL001") for n in (1, 2, 3)]
        rows, groups = L.build_groups(items, TODAY)
        self.assertEqual(groups, [])

    def test_group_dates_label_matches_the_spec_example(self):
        g = {"dates": [date(2026, 9, 11), date(2026, 9, 18),
                       date(2026, 9, 25)], "count": 3}
        self.assertEqual(L.group_dates_label(g),
                         "Next: Sep 11, 18, 25 · 3 in the window")


class ItemWeightTest(unittest.TestCase):
    """§5.5's three tiers. The whole point is that they are not all 1.0."""

    def test_an_assessment_outweighs_an_assignment(self):
        self.assertGreater(L.item_weight(item("x", kind="assessment")),
                           L.item_weight(item("y", kind="assignment")))

    def test_a_discussion_reply_is_the_cheapest_tier(self):
        self.assertEqual(L.item_weight(item("d", title="Discussion 4")),
                         L.RECURRING_WEIGHT)

    def test_a_problem_set_is_NOT_the_cheapest_tier(self):
        """It recurs, but recurring is not the same as cheap: the first
        version of this used §5.8's pattern matcher and a midterm plus a
        problem set came to 3.5, which raised nothing."""
        self.assertEqual(L.item_weight(item("p", title="Problem Set 5")),
                         L.ITEM_WEIGHT["assignment"])

    def test_a_quiz_does_not_weigh_as_much_as_a_midterm(self):
        """§6.5 already calls a quiz low-stakes."""
        self.assertLess(
            L.item_weight(item("q", kind="assessment", title="Quiz 3")),
            L.item_weight(item("m", kind="assessment", title="Midterm")))

    def test_a_flagged_assignment_is_pulled_up_to_assessment_weight(self):
        """multi_sitting IS the statement "more than one sitting" (§6.5)."""
        self.assertEqual(
            L.item_weight(item("p", title="Problem Set 3", multi_sitting=True)),
            L.ITEM_WEIGHT["assessment"])


class HeavyDayTest(unittest.TestCase):
    """Load, not row count — the defect the count rule had was measurable."""

    def test_three_recurring_replies_are_not_a_heavy_day(self):
        """1.5 total. The old 3-or-more count rule raised the banner here."""
        items = [item("h%d" % n, days=4, title="Discussion %d" % n)
                 for n in range(3)]
        self.assertIsNone(L.heavy_day(items, []))

    def test_a_midterm_plus_a_problem_set_is(self):
        """4.0 total, and two rows — so the old rule stayed silent here."""
        hd = L.heavy_day([item("ps", days=4, title="Problem Set 5")],
                         [item("mt", kind="assessment", days=4,
                               title="MATH001 Midterm")])
        self.assertIsNotNone(hd)
        self.assertEqual(hd["date"], TODAY + timedelta(days=4))
        self.assertEqual(hd["load"], 4.0)

    def test_the_banner_names_the_heaviest_item_first(self):
        hd = L.heavy_day([item("ps", days=4, title="Problem Set 5")],
                         [item("mt", kind="assessment", days=4,
                               title="Midterm")])
        self.assertEqual(hd["titles"][0], "Midterm")

    def test_four_ordinary_assignments_still_qualify(self):
        items = [item("h%d" % n, days=4, title="Essay %s" % "abcd"[n])
                 for n in range(4)]
        self.assertEqual(L.heavy_day(items, [])["load"], 4.0)

    def test_two_ordinary_assignments_do_not(self):
        self.assertIsNone(L.heavy_day([item("h1", days=4, title="Essay one"),
                                       item("h2", days=4, title="Essay two")],
                                      []))

    def test_soonest_wins_when_several_qualify(self):
        near = [item("n%d" % n, days=2, kind="assessment") for n in range(2)]
        far = [item("f%d" % n, days=9, kind="assessment") for n in range(4)]
        self.assertEqual(L.heavy_day(near + far, [])["date"],
                         TODAY + timedelta(days=2))


class CampusSectionTest(unittest.TestCase):
    """§5.3b — campus events out of Coming up and into their own capped
    section, because as block 4 they were structurally unreachable."""

    def _campus(self, iid, days, score=0):
        """Builds the shape `umd_calendar.to_item()` really produces.

        It used to pass `relevance=<int>`, which is not what any producer
        writes and not what SCHEMA_AND_STATE §3.3 specifies. Every campus test
        here passed against a fixture the pipeline never creates, while the
        real thing raised `TypeError` inside `sorted()` on any run with two or
        more campus events.
        """
        return item(iid, kind="event", days=days,
                    relevance={"score": score, "evidence": "include",
                               "why": "Why you're seeing this: test."},
                    source_refs=[{"kind": "umd_calendar"}])

    def test_a_campus_event_routes_to_campus_not_coming_up(self):
        self.assertEqual(L.section_for(self._campus("c1", 5), TODAY), "campus")

    def test_a_non_campus_event_still_routes_to_coming_up(self):
        self.assertEqual(
            L.section_for(item("e1", kind="event", days=5), TODAY),
            "coming_up")

    def test_assign_sections_has_a_campus_bucket(self):
        buckets = L.assign_sections([self._campus("c1", 5)], TODAY)
        self.assertEqual([i["id"] for i in buckets["campus"]], ["c1"])

    def test_campus_never_crowds_out_coming_up_again(self):
        """Eleven owed rows plus a campus event: the event is unaffected."""
        owed = [item("o%d" % n, kind="event", days=n + 1) for n in range(11)]
        buckets = L.assign_sections(owed + [self._campus("c1", 3)], TODAY)
        self.assertEqual(len(buckets["coming_up"]), 11)
        self.assertEqual(len(buckets["campus"]), 1)

    def test_order_campus_caps_and_returns_the_surplus(self):
        evs = [self._campus("c%d" % n, n + 1) for n in range(5)]
        rows, held = L.order_campus(evs, TODAY)
        self.assertEqual(len(rows), L.CAMPUS_MAX)
        self.assertEqual(len(held), 2)

    def test_order_campus_ranks_by_relevance_then_date(self):
        low = self._campus("low", 1, score=1)
        high = self._campus("high", 8, score=9)
        rows, _ = L.order_campus([low, high], TODAY)
        self.assertEqual(rows[0]["id"], "high")

    def test_order_campus_survives_every_relevance_shape(self):
        """A v7 item carries `null`; a hand-written row may carry a bare int."""
        legacy = item("legacy", kind="event", days=2, relevance=None,
                      source_refs=[{"kind": "umd_calendar"}])
        bare = item("bare", kind="event", days=2, relevance=4,
                    source_refs=[{"kind": "umd_calendar"}])
        rows, _ = L.order_campus([legacy, bare, self._campus("d", 2, score=9)],
                                 TODAY)
        self.assertEqual([r["id"] for r in rows], ["d", "bare", "legacy"])

    def test_relevance_score_reads_the_dict(self):
        self.assertEqual(L.relevance_score(self._campus("c", 1, score=7)), 7.0)
        self.assertEqual(L.relevance_score({"relevance": None}), 0.0)
        self.assertEqual(L.relevance_score({}), 0.0)

    def test_demoted_rows_sort_last_whatever_they_score(self):
        """§15's demotion must still beat the score, with the dict in place."""
        star = self._campus("star", 5, score=9)
        plain = self._campus("plain", 5, score=1)
        rows, _ = L.order_campus([star, plain], TODAY, demoted_ids=["star"])
        self.assertEqual([r["id"] for r in rows], ["plain", "star"])

    def test_a_campus_row_earns_no_ai_action(self):
        buckets = L.assign_ai_actions(
            {"campus": [self._campus("c1", 5)]}, TODAY)
        self.assertEqual(buckets["campus"][0]["ai_actions"], [])


class FullDetailTest(unittest.TestCase):
    """§5.6 — collapse by repetition, re-expand by proximity."""

    def test_first_surface_is_full_detail(self):
        self.assertTrue(L.full_detail(item("a", days=20), TODAY))

    def test_a_seen_item_far_out_collapses(self):
        self.assertFalse(
            L.full_detail(item("a", days=20, times_surfaced=4), TODAY))

    def test_an_assignment_re_expands_the_day_before(self):
        self.assertTrue(
            L.full_detail(item("a", days=1, times_surfaced=4), TODAY))

    def test_an_assessment_re_expands_three_days_out(self):
        self.assertTrue(L.full_detail(
            item("a", kind="assessment", days=3, times_surfaced=9), TODAY))

    def test_an_assessment_four_days_out_does_not(self):
        self.assertFalse(L.full_detail(
            item("a", kind="assessment", days=4, times_surfaced=9), TODAY))

    def test_re_expansion_is_not_the_New_chip(self):
        """Gate check 20: the chip means times_surfaced == 0 and nothing
        else, so a re-expanded row must not look new."""
        it = item("a", days=1, times_surfaced=4)
        self.assertTrue(L.full_detail(it, TODAY))
        self.assertNotEqual(it["times_surfaced"], 0)


class ChangeSummaryTest(unittest.TestCase):
    """§5.0 — the counts behind the change strip."""

    def test_counts_new_moved_and_resolved(self):
        rendered = [item("a", times_surfaced=0), item("b", times_surfaced=3),
                    item("c", times_surfaced=1)]
        counts = L.summarize_changes({"synced": ["z", "y"], "overdue": ["b"]},
                                     rendered, TODAY, moved_ids=["c", "gone"])
        self.assertEqual(counts["new"], 1)
        self.assertEqual(counts["moved"], 1)      # "gone" did not render
        self.assertEqual(counts["resolved"], 2)
        self.assertEqual(counts["overdue"], 1)

    def test_an_expiry_is_not_reported_as_an_accomplishment(self):
        counts = L.summarize_changes({"expired": ["a"], "dismissed": ["b"]},
                                     [], TODAY)
        self.assertEqual(counts["resolved"], 0)


class SkipRowTest(unittest.TestCase):
    """§9.6 — a step that truncates itself says so on the page."""

    def test_it_routes_to_attention(self):
        row = L.skip_row("step5b", "2 of 3 campus category pages", TODAY)
        self.assertEqual(L.section_for(row, TODAY), "attention")

    def test_it_is_a_System_row(self):
        row = L.skip_row("step6.7", "the link backfill", TODAY)
        self.assertEqual(row["course_label"], "System")
        self.assertEqual(row["course_class"], "none")

    def test_it_is_not_treated_as_an_unfixed_config_gap(self):
        """§5.7 exception 2 snoozes those 14 days. A per-run skip must not
        be silenced for a fortnight."""
        row = L.skip_row("step5b", "two pages", TODAY)
        self.assertFalse(row.get("config_unfixed"))
        row["times_surfaced"] = L.AUTO_DISMISS_AT
        out = L.auto_dismiss([row], TODAY)
        self.assertEqual(out["snoozed"], [])

    def test_the_id_is_dated_so_a_new_run_raises_a_new_row(self):
        a = L.skip_row("step5b", "x", TODAY)
        b = L.skip_row("step5b", "x", TODAY + timedelta(days=1))
        self.assertNotEqual(a["id"], b["id"])


class ComingUpOrderTest(unittest.TestCase):

    def _email(self, iid, days):
        return item(iid, days=days, source_refs=[{"kind": "gmail"}])

    def _cal(self, iid, days):
        return item(iid, days=days, source_refs=[{"kind": "gcal_class"}])

    def _campus(self, iid, days):
        return item(iid, kind="event", days=days,
                    source_refs=[{"kind": "umd_calendar"}])

    def test_blocks_order_email_calendar_umd_campus(self):
        rows, _ = L.order_coming_up([
            self._campus("camp", 2), item("umd", kind="umd_deadline", days=3),
            self._cal("cal", 1), self._email("mail", 9)], TODAY)
        self.assertEqual([r["id"] for r in rows],
                         ["mail", "cal", "umd", "camp"])

    def test_campus_demoted_first_over_the_cap(self):
        items = [self._email("m%d" % n, n) for n in range(9)]
        items += [self._campus("c%d" % n, n) for n in range(4)]
        primary, secondary = L.order_coming_up(items, TODAY)
        self.assertTrue(all(r["id"].startswith("m") for r in primary))
        self.assertEqual(len(secondary), 4)

    def test_demoted_rows_are_kept_not_dropped(self):
        """The whole point of the 2026-09-10 change: over the cap the section
        used to DISCARD these, so a plausible thing was never shown and the
        reader could not know it existed."""
        items = [self._email("m%d" % n, n) for n in range(9)]
        items += [self._campus("c%d" % n, n) for n in range(4)]
        primary, secondary = L.order_coming_up(items, TODAY)
        self.assertEqual(len(primary) + len(secondary), len(items))

    def test_actionable_and_registrar_blocks_are_never_demoted(self):
        items = [self._email("m%d" % n, n) for n in range(12)]
        items += [item("umd", kind="umd_deadline", days=5)]
        primary, secondary = L.order_coming_up(items, TODAY)
        self.assertEqual(secondary, [])
        self.assertEqual(len(primary), 13)

    def test_demotion_stops_as_soon_as_it_is_under_the_cap(self):
        """Block 2 is only demoted if losing block 4 was not enough. With 9
        actionable rows, dropping 3 campus events already fits, so the
        calendar row stays in primary — demoting it too would hide something
        for no reason."""
        items = [self._email("m%d" % n, n) for n in range(9)]
        items += [self._cal("cal", 4), self._campus("a", 2),
                  self._campus("b", 9), self._campus("c", 20)]
        primary, secondary = L.order_coming_up(items, TODAY)
        self.assertIn("cal", [r["id"] for r in primary])
        self.assertEqual([r["id"] for r in secondary], ["a", "b", "c"])

    def test_secondary_keeps_section_ordering(self):
        """When both blocks demote, the expanded list is still §5.3-ordered —
        calendar block before campus block, soonest first inside each — not a
        differently-sorted second section."""
        items = [self._email("m%d" % n, n) for n in range(11)]
        items += [self._campus("late", 20), self._campus("soon", 2),
                  self._cal("cal", 4), self._campus("mid", 9)]
        primary, secondary = L.order_coming_up(items, TODAY)
        self.assertEqual([r["id"] for r in secondary],
                         ["cal", "soon", "mid", "late"])

    def test_more_label_names_what_is_hidden_not_just_how_many(self):
        """"Show 4 more" tells the reader nothing about whether to open it."""
        secondary = [self._cal("c1", 3), self._cal("c2", 4),
                     self._campus("x1", 5)]
        label = L.more_label(secondary)
        self.assertIn("3 more", label)
        self.assertIn("already on your calendar", label)
        self.assertIn("1 campus event", label)
        self.assertNotIn("campus events", label)   # singular for one

    def test_more_label_is_empty_when_nothing_is_demoted(self):
        """An empty string here means render() removes the whole <details>."""
        self.assertEqual(L.more_label([]), "")
        self.assertEqual(L.more_label(None), "")


class DaysOutTest(unittest.TestCase):

    def test_formats(self):
        self.assertEqual(L.days_out_meta(TODAY, TODAY), "today")
        self.assertTrue(
            L.days_out_meta(TODAY + timedelta(days=1), TODAY)
            .startswith("tomorrow · "))
        self.assertTrue(
            L.days_out_meta(TODAY + timedelta(days=5), TODAY)
            .startswith("in 5 days · "))


class GapTest(unittest.TestCase):

    def ev(self, h1, h2, loc="", title=""):
        return {"start": datetime(2026, 9, 10, h1, 0),
                "end": datetime(2026, 9, 10, h2, 0),
                "location": loc, "title": title}

    def test_gap_minutes_and_tightness(self):
        g = S.gaps([self.ev(9, 10), self.ev(10, 11)])
        self.assertEqual(g[0]["minutes"], 0)
        self.assertTrue(g[0]["tight"])

    def test_twenty_minutes_is_not_tight(self):
        evs = [self.ev(9, 10), {"start": datetime(2026, 9, 10, 10, 20),
                                "end": datetime(2026, 9, 10, 11, 0),
                                "location": "", "title": ""}]
        self.assertFalse(S.gaps(evs)[0]["tight"])

    def test_label_names_buildings_only_when_both_sides_have_one(self):
        evs = [self.ev(9, 10, "Tydings"),
               {"start": datetime(2026, 9, 10, 10, 15),
                "end": datetime(2026, 9, 10, 11, 0),
                "location": "Key Hall", "title": ""}]
        self.assertEqual(S.gap_label(S.gaps(evs)[0]),
                         "15 minutes, Tydings to Key Hall")

    def test_a_missing_location_yields_no_invented_one(self):
        """§5.5: never invent a location for a transition that has none."""
        evs = [self.ev(9, 10, "Tydings"),
               {"start": datetime(2026, 9, 10, 10, 15),
                "end": datetime(2026, 9, 10, 11, 0),
                "location": "", "title": ""}]
        self.assertEqual(S.gap_label(S.gaps(evs)[0]), "15 minutes")

    def test_overlapping_events_are_not_reported_as_free_time(self):
        evs = [self.ev(9, 11), self.ev(10, 12)]
        self.assertEqual(S.gaps(evs), [])

    def test_tight_alert_threshold(self):
        tight = [self.ev(9, 10), self.ev(10, 11), self.ev(11, 12)]
        self.assertIsNone(S.tight_transition_alert(tight))    # 2 gaps
        tight.append(self.ev(12, 13))
        self.assertEqual(S.tight_transition_alert(tight), 3)


class PersonalCalendarTest(unittest.TestCase):
    """§2.2 — the personal calendar feeds the timeline and nothing else."""

    def cls(self, h1, h2, title="Lecture", loc="Tawes"):
        return {"start": datetime(2026, 9, 10, h1, 0),
                "end": datetime(2026, 9, 10, h2, 0), "location": loc,
                "title": title, "course_class": "phil",
                "course_label": "PHIL001"}

    def personal(self, h1, h2, title="Dentist", loc="", all_day=False):
        return {"start": datetime(2026, 9, 10, h1, 0),
                "end": datetime(2026, 9, 10, h2, 0), "location": loc,
                "title": title, "course_class": "personal",
                "course_label": "Personal", "all_day": all_day}

    def test_both_calendars_interleave_in_time_order(self):
        tl = S.timeline([self.cls(9, 10), self.personal(13, 14),
                         self.cls(15, 16)])
        titles = [e["title"] for e in tl if e["type"] == "event"]
        self.assertEqual(titles, ["Lecture", "Dentist", "Lecture"])

    def test_personal_events_keep_their_own_tag(self):
        tl = S.timeline([self.cls(9, 10), self.personal(13, 14)])
        events = [e for e in tl if e["type"] == "event"]
        self.assertEqual(events[0]["course_class"], "phil")
        self.assertEqual(events[1]["course_class"], "personal")
        self.assertEqual(events[1]["course_label"], "Personal")

    def test_a_personal_event_consumes_free_time(self):
        """The reason to read the calendar at all. Classes alone would report
        the whole 10-15 block free while he is at an appointment — a false
        claim about his day (§1.6), not a display nicety."""
        classes_only = S.gaps([self.cls(9, 10), self.cls(15, 16)])
        self.assertEqual(classes_only[0]["minutes"], 300)
        merged = S.gaps([self.cls(9, 10), self.personal(10, 15),
                         self.cls(15, 16)])
        self.assertEqual([g["minutes"] for g in merged], [0, 0])

    def test_a_personal_event_can_create_a_tight_transition(self):
        """Two back-to-back commitments are pressure whether or not both are
        lectures."""
        evs = [self.cls(9, 10), self.personal(10, 11),
               self.cls(11, 12), self.personal(12, 13)]
        self.assertEqual(S.tight_transition_alert(evs), 3)

    def test_all_day_personal_events_are_dropped(self):
        """No position on a single day's timeline; placing one at midnight
        would invent a time and manufacture a nine-hour opening gap."""
        tl = S.timeline([self.personal(0, 0, "Birthday", all_day=True),
                         self.cls(9, 10)])
        self.assertEqual([e["title"] for e in tl if e["type"] == "event"],
                         ["Lecture"])

    def test_a_personal_event_with_no_location_names_no_building(self):
        evs = [self.cls(9, 10, loc="Tawes"), self.personal(10, 11, loc="")]
        self.assertEqual(S.gap_label(S.gaps(evs)[0]), "0 minutes")

    def test_the_umbrella_window_respects_personal_events(self):
        """Rain during an appointment is not rain during a free gap."""
        evs = [self.cls(9, 12), self.personal(12, 18), self.cls(18, 19)]
        self.assertIsNone(S.umbrella([{"hour": 14, "pop": 80}], evs))


class UmbrellaTest(unittest.TestCase):

    def ev(self, h1, h2, title=""):
        return {"start": datetime(2026, 9, 10, h1, 0),
                "end": datetime(2026, 9, 10, h2, 0),
                "location": "", "title": title}

    def test_below_threshold_returns_none(self):
        self.assertIsNone(S.umbrella([{"hour": 15, "pop": 30}], []))

    def test_no_events_uses_the_daylight_span(self):
        note = S.umbrella([{"hour": 14, "pop": 70}], [])
        self.assertEqual(note["pop"], 70)
        self.assertIn("70%", note["text"])

    def test_rain_outside_daylight_is_not_reported(self):
        self.assertIsNone(S.umbrella([{"hour": 23, "pop": 90}], []))

    def test_gap_between_classes_is_named(self):
        events = [self.ev(9, 12, "ENGL001"), self.ev(18, 19, "PHIL001")]
        note = S.umbrella([{"hour": 16, "pop": 60}], events)
        self.assertIn("gap after ENGL001", note["text"])

    def test_rain_during_class_only_is_not_reported(self):
        """The note exists to change a decision; rain while seated does not."""
        events = [self.ev(9, 12, "ENGL001"), self.ev(12, 13, "MATH001")]
        self.assertIsNone(S.umbrella([{"hour": 10, "pop": 80}], events))

    def test_no_readings_returns_none_rather_than_a_dry_day_claim(self):
        self.assertIsNone(S.umbrella([], []))
        self.assertIsNone(S.umbrella(None, []))


class CanvasOverdueTest(unittest.TestCase):
    """2026-09-19 audit: Canvas's own submission record decides completion,
    and "may have been missed" fires only when something can still be done."""

    def test_canvas_submission_closes_an_open_item(self):
        it = item("a1", days=2, canvas_submission={"settled": True,
                                                   "state": "submitted"})
        L.run([it], TODAY)
        self.assertEqual(it["status"], "handled")
        self.assertEqual(it["handled_by"], "canvas: submitted")

    def test_submitted_work_is_never_flagged_overdue(self):
        it = item("a1", days=-1, canvas_submission={"settled": True})
        _, rep = L.run([it], TODAY)
        self.assertEqual(rep["overdue"], [])
        self.assertEqual(it["status"], "handled")

    def test_in_class_and_no_submission_items_are_not_flagged(self):
        paper = item("q", kind="assignment", days=-1,
                     submission_types=["on_paper"])
        none_ = item("p", days=-1, submission_types=["none"])
        _, rep = L.run([paper, none_], TODAY)
        self.assertEqual(rep["overdue"], [])

    def test_zero_point_optional_item_is_not_flagged(self):
        it = item("proj0", days=-1, points_possible=0.0,
                  submission_types=["online_upload"])
        _, rep = L.run([it], TODAY)
        self.assertEqual(rep["overdue"], [])

    def test_assessment_without_canvas_verdict_is_not_flagged(self):
        it = item("quiz", kind="assessment", days=-1)
        _, rep = L.run([it], TODAY)
        self.assertEqual(rep["overdue"], [])

    def test_canvas_missing_is_always_flagged(self):
        it = item("hw", days=-1, submission_types=["none"],
                  canvas_submission={"settled": False, "missing": True})
        _, rep = L.run([it], TODAY)
        self.assertEqual(rep["overdue"], ["hw"])

    def test_ordinary_online_assignment_still_flagged(self):
        it = item("hw", days=-1, submission_types=["online_upload"],
                  points_possible=10)
        _, rep = L.run([it], TODAY)
        self.assertEqual(rep["overdue"], ["hw"])

    def test_stale_false_alarm_is_retracted(self):
        it = item("q", kind="assessment", days=-2, status="unresolved",
                  overdue_flagged=True, needs_attention=True)
        _, rep = L.run([it], TODAY)
        self.assertEqual(rep["overdue_retracted"], ["q"])
        self.assertEqual(it["status"], "expired")
        self.assertIsNone(L.section_for(it, TODAY))


class RecordSurfacedTest(unittest.TestCase):
    def test_counts_once_per_day(self):
        it = item("a1", times_surfaced=2, last_shown="2026-01-01")
        L.record_surfaced([it], {"a1"}, TODAY)
        L.record_surfaced([it], {"a1"}, TODAY)
        self.assertEqual(it["times_surfaced"], 3)
        self.assertEqual(it["last_shown"], TODAY.isoformat())

    def test_unrendered_items_untouched(self):
        it = item("a1", times_surfaced=2)
        L.record_surfaced([it], {"other"}, TODAY)
        self.assertEqual(it["times_surfaced"], 2)

    def test_lead_minted_today_still_counts_first_showing(self):
        it = item("lead1", times_surfaced=0, last_shown=TODAY.isoformat())
        L.record_surfaced([it], {"lead1"}, TODAY)
        self.assertEqual(it["times_surfaced"], 1)

    def test_counter_drives_auto_dismiss_end_to_end(self):
        it = item("u1", status="unresolved", overdue_flagged=True,
                  times_surfaced=4, last_shown="2026-01-01")
        L.record_surfaced([it], {"u1"}, TODAY)
        L.auto_dismiss([it], TODAY)
        self.assertEqual(it["status"], "dismissed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
