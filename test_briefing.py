"""End-to-end checks for the briefing pipeline. Run: python3 test_briefing.py"""

import html as _html
import json
import re
import sys
import os
import tempfile
from datetime import date, datetime

import umd_calendar as uc
from reminder_url import build_reminder_url
from render_briefing import (block, build, build_row, fill, render, validate,
                             change_strip, resolve_meta_slot, COURSE_LABELS,
                             COURSE_CLASSES, EMPTY_NOTES, LIMITS, CAMPUS_MAX)

# Snapshots for the docs/code consistency block at the end of this file.
COURSE_LABELS_SET = set(COURSE_LABELS)
COURSE_CLASSES_SET = set(COURSE_CLASSES)
EMPTY_NOTES_MAP = dict(EMPTY_NOTES)
src_render = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "render_briefing.py"), encoding="utf-8").read()
# TEN since 2026-09-10: "Brief preferences" stopped being a section when the
# form moved into #sheet-prefs, and "Worth your time" became one when campus
# events left Coming up (§5.3b). The list is duplicated here on purpose so a
# template edit that drops a REAL section still fails against a written-down
# expectation rather than against whatever the template happens to say.
#
# One of the ten — "Where you stand" — is OPTIONAL on a rendered page, since
# it suppresses itself with no requirements set. It is mandatory in the
# TEMPLATE, which is what this list is compared against.
# Rewritten 2026-09-17 for the redesigned page: the design's ten headings, in
# page order. The campus heading's first spelling stands in for its slot.
GATE_LABELS = ["Today — Due", "Today — on your calendar",
               "Needs your attention", "What changed", "This week",
               "Opportunities", "Today — around campus",
               "Where you stand", "Further out",
               "Leads — unconfirmed, worth one look", "What I can't see"]

# Resolve everything against THIS file's directory. The suite used to open
# "briefing_artifact_template.html" and "umd_calendar.py" as bare relative
# paths, so running it from anywhere but its own folder failed on a missing
# template rather than on a real regression.
HERE = os.path.dirname(os.path.abspath(__file__))
TPL = os.path.join(HERE, "briefing_artifact_template.html")


# ---------------------------------------------------------------------------
# Sample pages, EMBEDDED.
#
# These were separate files under fixtures/ until 2026-09-07, and the suite
# could not run without them — which is the one thing a regression suite must
# never do. Worse, the project store did not keep them: a doc written to a
# nested path disappeared within the hour, so "just commit the fixtures" was
# not a durable fix. They live here instead, in the same doc as the tests that
# read them, so the suite is self-contained by construction.
#
# An on-disk copy still wins if one is present, so a real captured page can be
# dropped in to re-check the parsers against the live calendar without editing
# this file.
# ---------------------------------------------------------------------------

FIXTURES = {}

FIXTURES["category_current_students.md"] = """# Current Students

[Afterhours: Art After Dark](https://calendar.umd.edu/afterhours-art-after-dark)

2026-09-01T00:00:00

2026-10-15T00:00:00

An ongoing exhibition in the Herman Maril Gallery, open through the middle of October.

[View Event](https://calendar.umd.edu/afterhours-art-after-dark)

[NextNOW Fest](https://calendar.umd.edu/nextnow-fest-2026)

2026-09-08T18:00:00

2026-09-08T22:00:00

The Clarice opens the year with three nights of performance across the plaza.

[View Event](https://calendar.umd.edu/nextnow-fest-2026)

[NextNOW Fest](https://calendar.umd.edu/nextnow-fest-2026-2)

2026-09-09T18:00:00

2026-09-09T22:00:00

Night two of the festival, same plaza, different lineup.

[View Event](https://calendar.umd.edu/nextnow-fest-2026-2)

[Weed It Wednesdays](https://calendar.umd.edu/weed-it-wednesdays)

2026-09-09T10:00:00

2026-09-09T11:30:00

Drop in at the campus garden and help with the beds for an hour.

[View Event](https://calendar.umd.edu/weed-it-wednesdays)

[Study Abroad Fair](https://calendar.umd.edu/study-abroad-fair)

2026-09-09T14:00:00

2026-09-09T17:00:00

Meet program advisors from more than sixty partner universities in the Grand Ballroom.

[View Event](https://calendar.umd.edu/study-abroad-fair)

[Finding Your Fit: Career Workshop](https://calendar.umd.edu/finding-your-fit-career-workshop)

2026-09-10T15:30:00

2026-09-10T17:00:00

A ninety minute workshop on narrowing a career direction before applications open.

[View Event](https://calendar.umd.edu/finding-your-fit-career-workshop)

[Terp Farmers Market](https://calendar.umd.edu/terp-farmers-market)

2026-09-10T11:00:00

2026-09-10T14:00:00

Weekly market on Hornbake Plaza with regional growers and prepared foods.

[View Event](https://calendar.umd.edu/terp-farmers-market)

[Graduate Research Symposium](https://calendar.umd.edu/graduate-research-symposium)

2026-09-15T09:00:00

2026-09-15T12:00:00

Poster session and lightning talks from departments across the university.

[View Event](https://calendar.umd.edu/graduate-research-symposium)

[Chamber Music Recital](https://calendar.umd.edu/chamber-music-recital)

2026-09-16T19:30:00

2026-09-16T21:00:00

An evening recital in Gildenhorn Hall, doors at seven.

[View Event](https://calendar.umd.edu/chamber-music-recital)

[Wellness Walk](https://calendar.umd.edu/wellness-walk)

2026-09-18T08:00:00

2026-09-18T09:00:00

A guided morning walk that loops the campus perimeter.

[View Event](https://calendar.umd.edu/wellness-walk)

[Homecoming Kickoff](https://calendar.umd.edu/homecoming-kickoff)

2026-09-25T17:00:00

2026-09-25T20:00:00

The week opens on McKeldin Mall with music and a bonfire.

[View Event](https://calendar.umd.edu/homecoming-kickoff)

[Fall Career Fair](https://calendar.umd.edu/fall-career-fair)

2026-10-02T10:00:00

2026-10-02T16:00:00

Employer tables in the Xfinity Center pavilion, resume review on site.

[View Event](https://calendar.umd.edu/fall-career-fair)
"""

FIXTURES["detail_study_abroad.md"] = """# Study Abroad Fair

- 2:00 pm To - 5:00 pm

Meet program advisors from more than sixty partner universities in the Grand
Ballroom. Bring questions about coursework equivalence and scholarships.

[Register on TerpLink](https://terplink.umd.edu/event/12530799)

[Add to Calendar](https://calendar.umd.edu/study-abroad-fair/ics)

## Location

### [Adele H. Stamp Student Union](https://calendar.umd.edu/places/stamp)

3972 Campus Drive, College Park, MD 20742

## Contact

### Education Abroad

<educationabroad@umd.edu>

## Tags

### Event Topics

[Academics](https://calendar.umd.edu/category/academics)
[Student Life](https://calendar.umd.edu/category/student-life)

### Schools and Units

[Office of Undergraduate Studies](https://calendar.umd.edu/category/office-of-undergraduate-studies)

### Audience

[Current Students](https://calendar.umd.edu/category/current-students)
"""


def fixture(name):
    """Read a sample page: an on-disk override if there is one, else embedded."""
    for path in (os.path.join(HERE, "fixtures", name),
                 os.path.join(HERE, "fixture_%s" % name)):
        if os.path.exists(path):
            return open(path, encoding="utf-8").read()
    if name in FIXTURES:
        return FIXTURES[name]
    raise SystemExit("no fixture named %r" % name)


FAILURES = []
CAMPUS = {}


def check(name, cond, detail=""):
    if cond:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s  %s" % (name, detail))
        FAILURES.append(name)


# ---------------------------------------------------------------------------
# a realistic day, built the way step 8 builds it: a spec through build()
#
# Rewritten 2026-09-17 for the "Daily Briefing — reimagined" layout. Until then
# this fixture hand-assembled rows into the old type-named sections with
# render(); the page is now placed by DATE (Today cards, five week columns,
# Further out), which only build() computes, so the fixture is a spec.
# ---------------------------------------------------------------------------

import render_briefing as RB

CANVAS_OK = "https://umd.instructure.com/courses/1000009/assignments/1000011"
TODAY = date(2026, 9, 7)          # a Monday: the week runs Tue 9/8 – Sat 9/12


def ctx_of(label, kind, title, detail, dt, loc=None, src="Canvas assignment page",
           links=None):
    parts = ["%s — %s: %s" % (label, kind, title),
             "Full detail: %s" % detail,
             "Date: %s" % dt]
    if loc:
        parts.append("Location: %s" % loc)
    parts.append("Source: %s" % src)
    for l in links or []:
        parts.append("%s: %s" % (l["label"], l["url"]))
    return "\n".join(parts)


def day_spec():
    email_item = {
        "id": "advising-sprinternship-info-2026-09-15",
        "course": "CS-Advising", "course_label": "CS-Advising", "kind": "event",
        "title": "Sprinternship info session",
        "detail": "Paid spring-break externship; application walkthrough",
        "notes": "Bring a resume draft.",
        "date": "2026-09-15", "end_date": None,
        "time": "16:00", "end_time": "17:30",
        "location": "Iribe Center 1116, University of Maryland",
        "organizer": "CS Undergraduate Office",
        "links": [{"label": "Register", "url": "https://forms.umd.edu/sprinternship"},
                  {"label": "Join", "url": "https://umd.zoom.us/j/98765"}],
    }
    url, _ = build_reminder_url(email_item, datetime(2026, 9, 7),
                                source_label="CS advising email")

    # the campus event comes through the real module
    evs = uc.dedupe(uc.parse_category_page(
        fixture("category_current_students.md"), "current-students"))
    prefs = {"answers": {"topics": {}, "goals": ["career"], "units": [],
                         "keywords_boost": [], "keywords_block": [],
                         "max_events": 1, "horizon_days": 14,
                         "avoid_class_conflicts": "no", "on_campus_only": "no",
                         "free_only": "no"}}
    evs = uc.classify(evs, prefs, TODAY)
    chosen, _ = uc.select_and_enrich(
        evs, prefs, lambda u: fixture("detail_study_abroad.md"), max_fetches=2)
    # NOTE: no course_label patch here. to_item() sets it (§12.4).
    ci = uc.to_item(chosen[0], TODAY)
    curl, _ = build_reminder_url(ci, datetime(2026, 9, 7),
                                 source_label="UMD campus calendar")
    CAMPUS["item"], CAMPUS["url"] = ci, curl

    campus = [dict(ci, course_class="none", course_label="Campus",
                   detail=ci["detail"][:120], days_out_meta="in 2 days · Wed, Sep 9",
                   add_reminder_url=curl, times_surfaced=0, ai_actions=[],
                   expand_context=ctx_of("Campus", "event", ci["title"], ci["detail"],
                                         ci["date"], ci["location"],
                                         "UMD campus calendar", ci["links"])
                   + "\n" + ci["relevance"]["why"])]
    for n, (tm, loc) in enumerate((("12:00", "Iribe atrium"), ("19:30", ""))):
        campus.append({
            "id": "campus-%d" % n, "course_class": "none", "course_label": "Campus",
            "title": "Campus event %d" % n, "detail": "An open talk.",
            "date": "2026-09-07", "time": tm, "location": loc,
            "days_out_meta": "", "due_flag_text": "Due today",
            "expand_context": "Why you're seeing this: matches your interest.",
            "add_reminder_url": curl, "times_surfaced": 1, "ai_actions": []})

    return {
        "today": "2026-09-07",
        "scalars": {
            "ARTIFACT_TITLE": "UMD Daily Briefing — September 7, 2026",
            "WEEKDAY": "Monday", "MONTH_DAY": "September 7",
            "TLDR": "Problem Set 2 was due Friday with no submission recorded; "
                    "the ENGL001 draft is due tonight at 11:59.",
            "WEATHER_LINE": "Bring an umbrella (60% rain chance).",
            "CHANGE_STRIP": "2 new · 1 date moved · 3 resolved since yesterday",
            "TIME_LABEL": "Generated 7:02 AM ET · Mon, Sep 7",
            "STATUS_LABEL": "12 emails sorted · filing on",
            "WEEK_STAMP": "Rebuilt Mon, Sep 7",
            "ATTENTION_MORE_LABEL": "1 more — 1 system notice",
        },
        "timeline": [
            {"time": "9:30 AM", "course_class": "cmsc", "course_label": "CMSC001",
             "title": "Object-Oriented Programming I", "location": "IRB 0324"},
            {"gap": "15 minutes, IRB to Kirwan Hall", "tight": True},
            {"time": "11:00 AM", "course_class": "math", "course_label": "MATH001",
             "title": "Calculus II", "location": ""},
        ],
        "sections": {
            "assignments": [
                {"id": "cmsc001-project-1-2026-09-12", "course_class": "cmsc",
                 "course_label": "CMSC001", "title": "Project 1: Sequence Analyzer",
                 "detail": "Four classes plus a JUnit suite; partner work allowed",
                 "date": "2026-09-12", "days_out_meta": "in 5 days · Sat, Sep 12",
                 "canvas_url": CANVAS_OK, "ai_actions": ["steps"], "times_surfaced": 0,
                 "expand_context": ctx_of("CMSC001", "assignment", "Project 1: Sequence Analyzer",
                                          "Four classes plus a JUnit suite.", "2026-09-12")},
                {"id": "engl001-rhetorical-analysis-2026-09-07", "course_class": "engl",
                 "course_label": "ENGL001", "title": "Rhetorical Analysis draft",
                 "detail": "1200 words, upload as PDF", "date": "2026-09-07",
                 "due_flag_text": "Due today, 11:59 PM", "ai_actions": ["steps"],
                 "times_surfaced": 0,
                 "expand_context": ctx_of("ENGL001", "assignment", "Rhetorical Analysis draft",
                                          "1200 words, PDF.", "2026-09-07")},
                {"grouped": True, "course": "phil", "pattern_slug": "weekly-participation",
                 "course_class": "phil", "course_label": "PHIL001",
                 "title": "Weekly Participation posts", "date": "2026-09-11",
                 "group_dates": "Next: Sep 11, 18, 25 · 3 in the window",
                 "days_out_meta": "in 4 days · Fri, Sep 11"},
            ],
            "assessments": [
                {"id": "math001-first-exam-2026-09-09", "course_class": "math",
                 "course_label": "MATH001", "title": "First exam",
                 "detail": "Covers through §6.4.", "date": "2026-09-09",
                 "days_out_meta": "in 2 days · Wed, Sep 9", "ai_actions": ["study"],
                 "times_surfaced": 3,
                 "expand_context": ctx_of("MATH001", "assessment", "First exam",
                                          "Covers through 6.4.", "2026-09-09")},
                {"id": "math001-midterm-1-2026-09-24", "course_class": "math",
                 "course_label": "MATH001", "title": "Midterm 1",
                 "detail": "Covers integration techniques and series",
                 "date": "2026-09-24", "days_out_meta": "in 17 days · Thu, Sep 24",
                 "ai_actions": ["study"], "times_surfaced": 3,
                 "expand_context": ctx_of("MATH001", "assessment", "Midterm 1",
                                          "Integration techniques and series.", "2026-09-24")},
            ],
            "coming_up": [
                {"id": email_item["id"], "course_class": "advising",
                 "course_label": "CS-Advising", "title": email_item["title"],
                 "detail": email_item["detail"], "date": "2026-09-15",
                 "days_out_meta": "in 8 days · Tue, Sep 15", "add_reminder_url": url,
                 "ai_actions": [], "times_surfaced": 0,
                 "expand_context": ctx_of("CS-Advising", "event", email_item["title"],
                                          email_item["detail"], "2026-09-15",
                                          email_item["location"], "CS advising email",
                                          email_item["links"])},
            ],
            "campus": campus,
            "attention": [
                {"id": "econ001-problem-set-2-2026-09-04", "course_class": "econ",
                 "course_label": "ECON001", "title": "Problem Set 2",
                 "detail": "Due Sep 4; no submission confirmation was seen",
                 "ai_actions": ["email"], "times_surfaced": 0,
                 "expand_context": ctx_of("ECON001", "assignment", "Problem Set 2",
                                          "Due Sep 4, no confirmation seen.", "2026-09-04")},
                {"id": "phil001-quiz-1-2026-09-04", "course_class": "phil",
                 "course_label": "PHIL001", "title": "Quiz 1", "detail": "",
                 "days_out_meta": "3 days ago · Fri, Sep 4", "ai_actions": [],
                 "times_surfaced": 2, "expand_context": "PHIL001 — assessment: Quiz 1"},
            ],
            "attention_more": [
                {"id": "attention-umd-dates-empty", "course_class": "none",
                 "course_label": "System", "title": "No UMD academic dates configured",
                 "detail": "state.umd_dates is empty, so add/drop deadlines will not appear",
                 "ai_actions": [], "times_surfaced": 0,
                 "expand_context": "System — housekeeping: umd_dates is empty."},
            ],
            "opportunities": [
                {"id": "opp-quant-fund-2026-09-30", "course_class": "none",
                 "course_label": "Campus", "title": "Quant fund analyst applications",
                 "detail": "Rolling review; 2-8 hrs/week", "date": "2026-09-30",
                 "days_out_meta": "in 23 days · Wed, Sep 30",
                 "opp_meta": "Open to all majors · no prior experience stated",
                 "expand_context": "Campus — opportunity: Quant fund", "ai_actions": [],
                 "times_surfaced": 1},
            ],
            "leads": [
                {"id": "lead-ra-slots", "course_class": "none", "course_label": "Lead",
                 "title": "A CMSC research group may open two RA slots",
                 "confidence_tier": "inferred", "regime": "lead",
                 "basis": [{"claim": "A lab page lists spring openings", "source": "the lab site"}],
                 "confirm_action": "Email the lab manager", "kill_criteria": "No reply by Friday",
                 "source_url": "https://www.cs.umd.edu/lab", "expand_context": "Lead — RA slots",
                 "times_surfaced": 1, "ai_actions": []},
            ],
        },
        "changes": [{"course": "MATH001", "entries": [
            {"label": "Announcement", "text": "Quiz 1 solution",
             "url": "https://umd.instructure.com/courses/1/discussion_topics/2",
             "summary": "Solutions to Quiz 1 are posted under Files."},
            {"label": "Posted", "text": "141-f26-8-inverses",
             "url": "https://umd.instructure.com/courses/1/files/3"}]}],
        "coverage": ["Gradescope is not connected, so its deadlines are invisible."],
        "portfolio": ["28 of 120 credits toward the CS major."],
        "grades": {"PHIL001": "91.7% (2 of 36 graded)"},
        "caps": {"campus": 3},
    }


print("\n== render + validation gate ==")
SPEC = day_spec()
out, bprobs = build(SPEC, TPL)
open(os.path.join(tempfile.gettempdir(), "briefing.html"), "w").write(out)
check("the full day builds gate-clean", not bprobs, json.dumps(bprobs, indent=1))
check("validate() without an items map agrees", not validate(out, TPL),
      validate(out, TPL)[:3])
sec = lambda n: RB._section(out, n)
ids_in = lambda n: [rid for rid, _ in RB._rows(out, n)]

_labels = [re.sub(r"<[^>]+>", "", re.sub(r'<span class="m-only">.*?</span>', "", l))
           for l in re.findall(r'<h2 class="section-label">(.*?)</h2>', out)]
check("a full day shows all eleven headings, in the design's order",
      _labels == [names[0] for names in RB.CANONICAL_LABELS], _labels)
check("preferences form reachable (footer link opens the dialog)",
      'id="sheet-prefs"' in out and 'data-action="prefs-open"' in out
      and 'data-role="prefs-body"' in out)
check("detail dialog present", 'id="sheet-detail"' in out
      and 'data-role="detail-body"' in out)
check("four script blocks survive splicing", len(re.findall(r"<script>", out)) == 4)
check("title is dated", "<title>UMD Daily Briefing — September 7, 2026</title>" in out)

# --- the masthead ----------------------------------------------------------
check("the masthead carries the long and the phone date",
      '<span class="d-only">Monday</span><span class="m-only">Mon</span>, September 7' in out)
check("the weather line renders top right", "Bring an umbrella (60% rain chance)." in out)
check("masthead pills: due today, campus today, attention, next exam, "
      "opportunities — in order",
      re.findall(r'<span class="pill[^"]*"[^>]*>([^<]*)</span>', out)
      == ["1 due today", "2 campus events today", "2 need a look",
          "First exam (MATH001) in 2 days", "1 opportunity to consider"],
      re.findall(r'<span class="pill[^"]*"[^>]*>([^<]*)</span>', out))
check("only the attention pill is the accent pill",
      re.findall(r'<span class="pill is-accent"[^>]*>([^<]*)</span>', out) == ["2 need a look"])
check("counted pills carry their re-count formats",
      'data-count="attention" data-one="&#123;n&#125; needs a look"' in out
      and 'data-count="today"' in out)
check("the phone board drops the campus and exam pills",
      out.count('class="pill pill-wide"') == 2)
_nowx = build(dict(SPEC, scalars={k: v for k, v in SPEC["scalars"].items()
                                  if k != "WEATHER_LINE"}), TPL)[0]
check("no weather line means the eyebrow is deleted, not emptied",
      "rain chance" not in _nowx and '<span class="eyebrow"></span>' not in _nowx)

# --- §5.0 the change strip -------------------------------------------------
check("the change strip renders under the TLDR",
      re.search(r'class="tldr".*?class="changes"', out, re.S))
_no_strip = build(dict(SPEC, scalars=dict(SPEC["scalars"], CHANGE_STRIP="")), TPL)
check("no comparison means the whole div.changes is deleted, not emptied",
      'class="changes"' not in _no_strip[0] and not _no_strip[1], _no_strip[1])
_empty_strip = out.replace(
    ">2 new · 1 date moved · 3 resolved since yesterday<", "><")
check("check 26 fails an EMPTY change strip",
      any(x.startswith("26") for x in validate(_empty_strip, TPL)))
check("change_strip() says so plainly when nothing moved",
      change_strip({"new": 0, "moved": 0, "resolved": 0})
      == "Nothing changed since yesterday.")
check("change_strip() is silent when there is nothing to compare against",
      change_strip({"new": 3}, comparable=False) == "")
check("change_strip() omits the zero counts",
      change_strip({"new": 2, "moved": 0, "resolved": 1})
      == "2 new · 1 resolved since yesterday")

# --- placement by date -----------------------------------------------------
check("Today — Due holds exactly what is due today",
      ids_in("section-today") == ["engl001-rhetorical-analysis-2026-09-07"],
      ids_in("section-today"))
check("a today card shows its due flag as the status eyebrow",
      "Due today, 11:59 PM" in sec("section-today"))
check("This week holds the next five days' rows, grouped row included",
      sorted(ids_in("section-week")) == sorted([
          "cmsc001-project-1-2026-09-12", "group-phil-weekly-participation",
          "math001-first-exam-2026-09-09"]), ids_in("section-week"))
_days = re.findall(r'<div class="wk-day[^"]*">\s*<span class="eyebrow">([^<]*)</span>',
                   sec("section-week"))
check("five day columns, Tue – Sat", _days == ["Tue 9/8", "Wed 9/9", "Thu 9/10",
                                               "Fri 9/11", "Sat 9/12"], _days)
check("the week range eyebrow names the window", ">Tue – Sat<" in out)
check("an exam day is the ink card, with the accent pill",
      re.search(r'<div class="wk-day is-exam">\s*<span class="eyebrow">Wed 9/9', out)
      and '<div class="wk-group is-exam">' in out)
check("a day with nothing is the dashed empty column",
      len(re.findall(r'<div class="wk-day is-empty">', out)) == 2)
check("Further out holds everything past the window, soonest first",
      ids_in("section-further") == ["advising-sprinternship-info-2026-09-15",
                                    "math001-midterm-1-2026-09-24"],
      ids_in("section-further"))
check("Further out dates are the short form",
      ">Tue 9/15</div>" in sec("section-further") and ">Thu 9/24</div>" in sec("section-further"))
check("no item renders twice", len(RB._ROW_ID and re.findall(RB._ROW_ID, out))
      == len(set(re.findall(RB._ROW_ID, out))))

# --- §16c: FYI events demoted out of Further out ----------------------------
_fyi_item = {"id": "umd-homecoming-2026-10-14", "course_class": "umd",
            "course_label": "UMD", "title": "Homecoming 2026",
            "detail": "Oct 14-17, on campus.", "date": "2026-10-14",
            "days_out_meta": "in 37 days · Wed, Oct 14", "add_reminder_url": "",
            "ai_actions": [], "times_surfaced": 0, "fyi": True,
            "expand_context": "UMD — event: Homecoming 2026"}
_fyi_spec = dict(SPEC, sections=dict(
    SPEC["sections"],
    coming_up=SPEC["sections"]["coming_up"] + [_fyi_item]))
_fyi_out, _fyi_probs = build(_fyi_spec, TPL)
check("a page with an FYI further-out item is still gate-clean", not _fyi_probs, _fyi_probs)
check("an item flagged fyi is demoted out of Further out's primary list",
      "umd-homecoming-2026-10-14" not in RB._section(_fyi_out, "section-further")
      .split('<details class="more further-more">')[0])
check("...and reappears inside further-more's own disclosure",
      "umd-homecoming-2026-10-14" in _fyi_out
      and "Homecoming 2026" in _fyi_out.split(
          '<details class="more further-more">')[1].split("</details>")[0])
check("FURTHER_MORE_LABEL names what's hidden as informational",
      "informational" in _fyi_out)
check("a non-fyi further-out item never triggers the disclosure",
      '<details class="more further-more">' not in out)

# --- around campus ---------------------------------------------------------
_camp = sec("section-campus")
check("campus heading says Today when any event is today",
      "Today — around campus" in out)
check("today's campus events are banded Midday / Evening, other days by date",
      re.findall(r'<div class="t-group eyebrow">([^<]*)</div>', _camp)
      == ["Midday", "Evening", "Wed 9/9"],
      re.findall(r'<div class="t-group eyebrow">([^<]*)</div>', _camp))
check("a campus row's sub-line is its location, not its description",
      ">Iribe atrium</div>" in _camp and "An open talk." not in _camp)
check("a campus row keeps Add reminder and the rating control",
      'class="menu-item add-reminder"' in _camp and 'data-action="rate"' in _camp)
check("the campus time cell carries the long and the phone form",
      '<span class="t-long">7:30 PM</span><span class="t-short">7:30 PM</span>' in _camp
      and '<span class="t-long">12:00 PM</span><span class="t-short">12 PM</span>' in _camp)
check("check 27 fails campus rows over the run's cap",
      any(p.startswith("27:") for p in validate(out, TPL, caps={"campus": 2})))

# --- calendar --------------------------------------------------------------
check("today's calendar renders as a time panel with its tight gap",
      "Object-Oriented Programming I" in sec("section-schedule")
      and 't-note tight">15 min between IRB and Kirwan Hall' in sec("section-schedule"))
check("What changed renders per course with its summary",
      'class="chg-course" data-seen="2026-09-07"' in sec("section-changes")
      and "Solutions to Quiz 1 are posted" in sec("section-changes")
      and '<span class="chg-kind">Posted</span>' in sec("section-changes"))

# --- attention -------------------------------------------------------------
_attn = sec("section-attention")
check("a System row sits behind the disclosure, not inline",
      "attention-umd-dates-empty" in _attn.split('<details class="more attn-more">')[1]
      and "attention-umd-dates-empty" not in _attn.split('<details class="more attn-more">')[0])
check("an attention row with no detail gets its course and when as the sub-line",
      ">PHIL001 · 3 days ago · Fri, Sep 4</div>" in _attn)
check("a System attention row earns no Draft an email",
      'data-task="email"' not in _attn.split('<details class="more attn-more">')[1])

# --- where you stand -------------------------------------------------------
_stand = sec("section-standing")
check("Where you stand lists the next thing per course, in the design's order",
      re.findall(r'<span class="course-tag [a-z]+">([^<]*)</span>', _stand)
      == ["ENGL001", "ECON001", "MATH001", "PHIL001", "CMSC001", "CS-Advising"],
      re.findall(r'<span class="course-tag [a-z]+">([^<]*)</span>', _stand))
check("a course with nothing upcoming but something overdue says so",
      re.search(r"Problem Set 2 · Due Sep 4; no submission confirmation was seen</p>"
                r'\s*<span class="tbl-when">overdue', _stand))
check("standing lines' own .tbl-row carries data-ref, never a second data-item-id",
      not re.search(r'<div class="tbl-row"[^>]*data-item-id', _stand)
      and 'data-ref="engl001-rhetorical-analysis-2026-09-07"' in _stand)
check("a standing line's Mark done button DOES carry the real item's id "
      "(§16b — the generic click handler reads it off the button, not the row)",
      'data-action="mark-done" data-item-id="engl001-rhetorical-analysis-2026-09-07"'
      in _stand)
check("portfolio notes join the standing table", "28 of 120 credits" in _stand)
# STEP 5d (2026-09-19): a compact, honest grade readout per course.
check("a course's grade readout joins its standing line, grouped row included",
      "91.7% (2 of 36 graded)" in _stand
      and "Weekly Participation posts" in _stand)
check("a course with no grade data gets no grade suffix at all",
      "%" not in re.search(r"Rhetorical Analysis draft[^<]*", _stand).group(0))
check("check 31 fails a page with more portfolio notes than the run's cap",
      any(p.startswith("31:") for p in validate(
          build(dict(SPEC, portfolio=["a", "b", "c", "d"]), TPL)[0], TPL,
          caps={"portfolio_notes": 3})))
check("a page within the portfolio-notes cap stays gate-clean on check 31",
      not any(p.startswith("31:") for p in validate(out, TPL)))

# --- a quiet day -----------------------------------------------------------
_quiet, _qp = build({"today": "2026-09-11", "scalars": {
    "ARTIFACT_TITLE": "UMD Daily Briefing — September 11, 2026",
    "WEEKDAY": "Friday", "MONTH_DAY": "September 11", "TLDR": "A quiet day.",
    "TIME_LABEL": "t", "STATUS_LABEL": "s"}, "sections": {}}, TPL)
check("a quiet day is gate-clean", not _qp, _qp)
check("a quiet day keeps its mandatory sections, each with its note",
      all(n in _quiet for n in EMPTY_NOTES.values())
      and _quiet.count('class="section is-empty"') == 3)
check("a quiet day drops every optional section outright",
      all(('id="%s"' % s) not in _quiet for s in RB.OPTIONAL_SECTIONS.values()))
check("a quiet week is five dashed columns",
      _quiet.count('<div class="wk-day is-empty">') == 5)
check("no pill is ever a zero", '<span class="pill' not in _quiet)
check("a trailing comma on WEEKDAY cannot double the separator",
      ", , " not in build({"today": "2026-09-10", "scalars": {
          "ARTIFACT_TITLE": "UMD Daily Briefing — September 10, 2026",
          "WEEKDAY": "Thursday,", "MONTH_DAY": "September 10", "TLDR": "x",
          "TIME_LABEL": "t", "STATUS_LABEL": "s"}, "sections": {}}, TPL)[0])

print("\n== gate catches deliberate faults ==")
ITEMS = {}
for _sec, _rows_ in SPEC["sections"].items():
    for _it in _rows_:
        if not _it.get("grouped"):
            ITEMS[_it["id"]] = {"ai_actions": _it.get("ai_actions") or [],
                                "times_surfaced": _it.get("times_surfaced") or 0,
                                "regime": _it.get("regime") or "confirmed",
                                "date": _it.get("date"), "today": "2026-09-07",
                                # §16b: the same data-kind remember() stamps,
                                # so a Where-you-stand echo of this item's
                                # mark-done/mark-resolved cross-checks clean.
                                "kind": RB.SECTION_KIND.get(_sec)}
check("clean page passes with item data supplied", not validate(out, TPL, ITEMS),
      json.dumps(validate(out, TPL, ITEMS), indent=1))
_eid = "engl001-rhetorical-analysis-2026-09-07"
faults = [
    ("unfilled placeholder", out.replace("Monday", "{{WEEKDAY}}"), "1"),
    ("an HTML comment", out.replace('<div class="footer">', '<!-- x --><div class="footer">'), "2"),
    ("corrupted locked script", out.replace("var STORES = [", "var STORES = [ /*x*/"), "10"),
    ("undated title", out.replace("UMD Daily Briefing — September 7, 2026",
                                  "UMD Daily Briefing"), "21"),
    ("renamed artifact title", out.replace("UMD Daily Briefing — September 7, 2026",
                                           "Briefing 2026"), "21"),
    ("stripped preferences shell",
     out.replace('data-action="prefs-save"', 'data-action="x"'), "22"),
    ("http link", out.replace('href="https://umd.instructure.com',
                              'href="http://umd.instructure.com'), "19"),
    ("add-reminder as button",
     out.replace('<a class="menu-item add-reminder"', '<button class="menu-item add-reminder"'), "8"),
    ("bad course label", out.replace(">System<", ">Housekeeping<"), "13"),
    ("F11 empty data-item-id",
     out.replace('data-item-id="econ001-problem-set-2-2026-09-04"', 'data-item-id=""', 1), "5"),
    ("a row with no data-kind", out.replace(' data-kind="assignment"', "", 1), "5"),
    ("F12 empty .row-new span",
     out.replace('<span class="row-new">New</span>', '<span class="row-new"></span>', 1), "20"),
    ("F12 'null' where a value should be",
     out.replace(">Rhetorical Analysis draft<", ">null<"), "2"),
    ("F12 a field rendered as None",
     out.replace("Covers integration techniques and series", "Location: None"), "2"),
    ("F9 reminder that lost a captured link",
     re.sub(r'(add-reminder" role="menuitem" href="[^"]*?)details=[^"&]*',
            r'\1details=Hi', out), "19"),
    ("spoofed Canvas host",
     out.replace(CANVAS_OK, "https://evil-instructure.com/courses/1/assignments/2"), "7"),
    ("a Canvas link to a non-object path",
     out.replace("/courses/1000009/assignments/1000011", "/login"), "7"),
    ("mark-resolved on an assignment row",
     out.replace('data-action="mark-done" data-item-id="cmsc001-project-1-2026-09-12"',
                 'data-action="mark-resolved" data-item-id="cmsc001-project-1-2026-09-12"'), "16"),
    ("mark-done on an assessment row",
     re.sub(r'(data-item-id="math001-first-exam-2026-09-09"[^>]*data-kind="assessment">'
            r'.*?<div class="row-menu" role="menu" hidden>)',
            r'\1<button type="button" class="menu-item" role="menuitem" data-action="mark-done" '
            r'data-item-id="math001-first-exam-2026-09-09">Mark done</button>', out, count=1, flags=re.S), "9"),
    ("a class/label pair §2.1 does not allow",
     out.replace('<span class="course-tag engl">ENGL001</span>',
                 '<span class="course-tag engl">UMD</span>', 1), "13"),
    ("a .row-expand id that does not match its row",
     out.replace('<div class="row-expand" data-item-id="math001-midterm-1-2026-09-24"',
                 '<div class="row-expand" data-item-id="oops"'), "14"),
    ("an invented section heading",
     out.replace('<div class="footer">',
                 '<h2 class="section-label">Extra</h2>\n  <div class="footer">'), "4"),
    ("sections out of order",
     out.replace('<h2 class="section-label">This week</h2>',
                 '<h2 class="section-label">Where you stand</h2>'), "4"),
    ("a javascript: href",
     out.replace('href="%s"' % CANVAS_OK, 'href="javascript:alert(1)"'), "19"),
    ("an over-length data-context",
     out.replace('data-context="CMSC001', 'data-context="' + "q" * 3100 + ' CMSC001', 1), "23"),
    ("a System row moved inline",
     out.replace('<details class="more attn-more">', '<div class="x">', 1)
        .replace("</details>", "</div>", 1), "29"),
    ("a lead row moved out of Leads",
     out.replace('<div class="section" id="section-leads">', '<div class="section" id="section-lx">'), "24"),
]
for name, broken, code in faults:
    probs = validate(broken, TPL, ITEMS)
    check("catches %s" % name, any(p.startswith(code + ":") for p in probs),
          "got %r" % probs[:3])

unearned = dict(ITEMS)
unearned["cmsc001-project-1-2026-09-12"] = dict(ITEMS["cmsc001-project-1-2026-09-12"], ai_actions=[])
check("catches F10 an AI button the item never earned",
      any(p.startswith("18:") for p in validate(out, TPL, unearned)))
missing = dict(ITEMS)
missing["advising-sprinternship-info-2026-09-15"] = dict(
    ITEMS["advising-sprinternship-info-2026-09-15"], ai_actions=["steps"])
check("catches F10 an earned action with no button",
      any(p.startswith("18:") for p in validate(out, TPL, missing)))
stale = dict(ITEMS)
stale["cmsc001-project-1-2026-09-12"] = dict(ITEMS["cmsc001-project-1-2026-09-12"], times_surfaced=4)
check("catches a New chip on an item that is not new",
      any(p.startswith("20:") for p in validate(out, TPL, stale)))
moved = dict(ITEMS)
moved[_eid] = dict(ITEMS[_eid], date="2026-09-20")
check("check 30 catches a Today card that is not due today",
      any(p.startswith("30:") for p in validate(out, TPL, moved)))
moved2 = dict(ITEMS)
moved2["cmsc001-project-1-2026-09-12"] = dict(ITEMS["cmsc001-project-1-2026-09-12"], date="2026-10-01")
check("check 30 catches a week row outside the five-day window",
      any(p.startswith("30:") for p in validate(out, TPL, moved2)))
check("CS content saying 'null' does not block the briefing",
      not validate(out.replace("1200 words, upload as PDF",
                               "Handle a null reference and NaN input"), TPL, ITEMS))
_both = validate(out.replace("Due today, 11:59 PM</span>", "Due today, 11:59 PM</span>today", 1), TPL)
check("check 28 fails a row carrying both a due flag and a days-out",
      any(p.startswith("28:") for p in _both), _both)
plain = build(dict(SPEC, sections={"assignments": [
    dict(SPEC["sections"]["assignments"][0], ai_actions=["steps", "study"])]}), TPL)[1]
check("build() still catches an earned action the template cannot render",
      any(p.startswith("18:") for p in plain), plain)

# P0-1 — every file arrives with blank lines holding a single space; block()
# must still find every row template.
padded = "\n".join(" " if l.strip() == "" else l
                   for l in open(TPL, encoding="utf-8").read().split("\n"))
for name in ("ASSIGNMENT ROW", "ASSESSMENT ROW", "GROUPED ROW", "COMING-UP ROW",
             "ATTENTION ROW", "LEAD ROW", "OPPORTUNITY ROW"):
    try:
        got = bool(block(padded, name))
    except KeyError:
        got = False
    check("block() survives whitespace-only blank lines: %s" % name, got)

# Untrusted item text must not reach the renderer's substitution (§1.5).
_hostile = dict(SPEC["sections"]["assignments"][0],
                detail="Prof wrote: use {{WEEK_RANGE}} as the variable name")
_ho, _hp = build(dict(SPEC, sections=dict(SPEC["sections"], assignments=[_hostile])), TPL)
check("a placeholder in item text injects nothing and still validates",
      "use &#123;&#123;WEEK_RANGE&#125;&#125; as" in _ho and not _hp, _hp)

# data-context is the AI actions' grounding and Show-full-detail's verbatim
# text; the whitespace tidy used to eat a blank line inside it.
multi = "CMSC001 — assignment: P1\nFull detail: one\n\n\ntwo\nLinks: Submit: https://umd.instructure.com/a"
_mo = build(dict(SPEC, sections={"assignments": [
    dict(SPEC["sections"]["assignments"][0], expand_context=multi)]}), TPL)[0]
got_ctx = _html.unescape(re.search(r'data-context="([^"]*)"', _mo).group(1))
check("data-context round-trips blank lines byte-exact", got_ctx == multi, "%r" % got_ctx)

# The CLI is what step 8 actually calls.
import subprocess
with tempfile.TemporaryDirectory() as td:
    sp = os.path.join(td, "spec.json")
    op = os.path.join(td, "out.html")
    open(sp, "w", encoding="utf-8").write(json.dumps(SPEC))
    rc = subprocess.run([sys.executable, os.path.join(HERE, "render_briefing.py"),
                         sp, TPL, op], capture_output=True, text=True)
    check("the CLI exits 0 and prints nothing on a clean page",
          rc.returncode == 0 and rc.stdout.strip() == "", (rc.returncode, rc.stdout[:300]))
    check("the CLI writes a page byte-identical to build()",
          open(op, encoding="utf-8").read() == out)
    bad = json.loads(json.dumps(SPEC))
    bad["sections"]["assignments"][0]["id"] = ""
    open(sp, "w", encoding="utf-8").write(json.dumps(bad))
    rc = subprocess.run([sys.executable, os.path.join(HERE, "render_briefing.py"),
                         sp, TPL, op], capture_output=True, text=True)
    check("the CLI exits non-zero and reports the problem on a broken page",
          rc.returncode == 1 and "5:" in rc.stdout, (rc.returncode, rc.stdout[:200]))
check("the spec is small enough to be the only thing in context",
      len(json.dumps(SPEC)) < 12000, len(json.dumps(SPEC)))


print("\n== UMD calendar ==")
text = fixture("category_current_students.md")
raw = uc.parse_category_page(text, "current-students")
check("parses every listing", len(raw) == 12, len(raw))
check("captures real start/end times",
      any(e["title"].startswith("Study Abroad") and e["start"].hour == 14
          and e["end"].hour == 17 for e in raw))
d = uc.dedupe(raw)
check("collapses the repeated festival listing", len(d) == 11, len(d))
check("records the collapsed instance",
      any(e.get("other_instances") == 1 for e in d))
check("multi-week exhibition flagged as ongoing, not timed",
      next(e for e in d if "Afterhours" in e["title"])["multi_day"] is True)

prefs = {"answers": {"topics": {"athletics-and-recreation": "never"},
                     "goals": ["career"], "units": [], "keywords_boost": [],
                     "keywords_block": ["garden"], "max_events": 2,
                     "horizon_days": 14, "avoid_class_conflicts": "yes",
                     "on_campus_only": "no", "free_only": "no"}}
cls = [(datetime(2026, 9, 9, 14, 0), datetime(2026, 9, 9, 15, 15))]
c = uc.classify(list(d), prefs, date(2026, 9, 7), class_blocks=cls)
byname = {e["title"]: e for e in c}
check("blocked keyword excludes",
      byname["Weed It Wednesdays"]["evidence"] == "exclude")
check("class conflict excludes",
      byname["Study Abroad Fair"]["evidence"] == "exclude")
check("career interest ranks a career workshop top",
      max(c, key=lambda e: e.get("score", 0))["title"].startswith("Finding Your Fit"))
check("every surfaced event explains itself",
      all(e.get("why") for e in c))
check("horizon respected",
      all(e["evidence"] == "exclude" for e in c
          if (e["start"].date() - date(2026, 9, 7)).days > 14))

hz = dict(prefs); hz["answers"] = dict(prefs["answers"], max_events=0)
check("max_events 0 turns campus events off", uc.select(c, hz) == [])

det = uc.enrich_from_detail(fixture("detail_study_abroad.md"),
                            byname["Study Abroad Fair"])
check("detail page yields venue", det["location"] == "Adele H. Stamp Student Union")
check("detail page yields street address", det["address"].endswith("20742"))
check("detail page yields registration link",
      det["registration_url"] == "https://terplink.umd.edu/event/12530799")
check("detail page yields organizer", det["organizer"] == "Education Abroad")
check("absent field recorded, not invented",
      det["cost"] is None and "cost" in det["fields_absent"])

topics, units, audience = uc.parse_tags_from_detail(
    fixture("detail_study_abroad.md"))
check("audience tags parsed", "current-students" in audience)

print("\n== email event extraction keeps its details ==")
item = {"id": "x", "course": "CS-Advising", "course_label": "CS-Advising",
        "kind": "event", "title": "Sprinternship info session",
        "detail": "Paid spring-break externship", "notes": "Bring a resume draft.",
        "date": "2026-09-15", "end_date": None, "time": "16:00", "end_time": "17:30",
        "location": "Iribe Center 1116", "organizer": "CS Undergraduate Office",
        "links": [{"label": "Register", "url": "https://forms.umd.edu/sprinternship"},
                  {"label": "Join", "url": "https://umd.zoom.us/j/98765"}]}
u, assum = build_reminder_url(item, datetime(2026, 9, 7), "CS advising email")
# The `or` clause here used to match any timed event at all, so the exact-value
# half could never fail the check. Assert the real UTC value: 4:00 PM ET on
# 2026-09-15 is EDT, so 20:00Z, and 5:30 PM is 21:30Z.
check("stated start and end make an exact timed event",
      "dates=20260915T200000Z%2F20260915T213000Z" in u, u)
check("ET->UTC honours DST (same wall clock in winter is an hour later in UTC)",
      "dates=20261211T210000Z%2F20261211T223000Z" in
      build_reminder_url(dict(item, date="2026-12-11"), datetime(2026, 12, 1))[0])
check("registration link reaches the event", "forms.umd.edu%2Fsprinternship" in u)
check("join link reaches the event", "umd.zoom.us" in u)
check("join URL becomes the location", "location=https%3A%2F%2Fumd.zoom.us" in u)
check("details keep line breaks", "%0A" in u)
check("organizer carried through", "Organizer%3A" in u)
check("no assumption invented when both times stated", assum == [])

noend = dict(item, end_time=None)
u2, assum2 = build_reminder_url(noend, datetime(2026, 9, 7))
check("missing end time is disclosed, not silently invented",
      assum2 == ["End time not stated — 1 hour assumed."] and "1+hour+assumed" in u2.replace("%20", "+"))

deadline = {"id": "d", "course": "ENGL001", "course_label": "ENGL001",
            "kind": "assignment", "title": "Draft", "detail": "1200 words",
            "date": "2026-09-12", "end_date": None, "time": "23:59",
            "end_time": None, "links": []}
u3, _ = build_reminder_url(deadline, datetime(2026, 9, 7))
check("a deadline is all-day, not a meeting",
      re.search(r"dates=\d{8}%2F\d{8}(&|$)", u3) is not None)
check("the real due time survives in the details", "11%3A59+PM+ET" in u3.replace("%20", "+"))

print("\n== preferences ==")
tpl = open(TPL, encoding="utf-8").read()
schema_topics = re.findall(r'\["([a-z\-]+)", "[^"]+"\]', tpl.split("var TOPICS = [")[1].split("];")[0])
check("quiz topic ids are the calendar's own slugs",
      schema_topics == uc.TOPIC_SLUGS,
      "%s vs %s" % (schema_topics, uc.TOPIC_SLUGS))
check("quiz persists to an even-segment doc path",
      'db.doc("prefs/quiz")' in tpl and 'db.doc("prefs/custom")' in tpl)
# Asserts the INVARIANT (each document is read and written independently),
# not the spelling. This used to count `doc("prefs/custom")` literally, so
# routing the writes through a helper broke the check while the two documents
# stayed exactly as separate as before.
check("requirements are a third, separate document (§13.2)",
      tpl.count("prefs/requirements") >= 2 and 'q.type === "requirements"' in tpl,
      tpl.count("prefs/requirements"))
check("the requirements editor can create and remove entries",
      "Add a requirement" in tpl and 'className = "act req-del"' in tpl)
check("a requirement id is minted once, not re-derived from the statement",
      "function mintId" in tpl and "never re-derived" in tpl)
check("requirement horizons are stored as numbers, not option strings",
      'parseInt(r.horizon_days, 10)' in tpl)
check("a blank requirement is dropped rather than stored",
      'filter(function (r) { return (r.statement || "").trim(); })' in tpl)
check("custom instructions stored separately from quiz answers",
      tpl.count("prefs/custom") >= 2 and tpl.count("prefs/quiz") >= 2
      and "prefs/quiz" != "prefs/custom",
      "quiz=%d custom=%d" % (tpl.count("prefs/quiz"), tpl.count("prefs/custom")))
# The shell moved from #section-prefs to the #sheet-prefs dialog (2026-09-10).
# Same property, new container: the pipeline writes NOTHING into the form, so a
# placeholder appearing here would mean a run had started filling it in.
check("preferences shell carries no pipeline placeholder",
      "{{" not in re.search(r'<dialog[^>]*id="sheet-prefs".*?</dialog>',
                            tpl, re.S).group(0))
check("preferences persist through the FastAPI service's own REST shim, "
      "not the retired claude.ai Artifact db capability (2026-09-16)",
      # Only ONE restDb() now (2026-09-16b): the bootstrap script's copy was
      # removed once the Leads pair got real app.py endpoints and every
      # STORES entry became `endpoint`-based, leaving the prefs script block
      # (a genuinely different shape -- one whole document per call, not a
      # one-item POST) as the only caller left.
      tpl.count("function restDb()") == 1
      and 'db.doc("prefs/quiz")' in tpl and 'db.doc("prefs/custom")' in tpl
      and 'db.doc("prefs/requirements")' in tpl)
check("lifecycle actions (done/resolved/lead-confirmed/lead-killed) are all "
      "single-item REST POSTs, not a retired map-document store (2026-09-16b)",
      "restDb()" not in tpl.split("<script>")[1].split("</script>")[0]
      and '"/leads/" + encodeURIComponent(id) + "/confirmed"' in tpl
      and '"/leads/" + encodeURIComponent(id) + "/killed"' in tpl
      and "store.path" not in tpl)
check("quiz reload merges over defaults so new questions appear",
      "Merge over defaults" in tpl)

print("\n== regressions fixed 2026-09-07 (audit) ==")

# F5 — over-cap truncation must keep the summary line, the links and the
# provenance line, and must use the budget it has. The old code collapsed a
# 1200-character reminder to 116 characters and dropped the final line even
# though it was far under the cap.
from urllib.parse import parse_qs, urlparse

def _details(u):
    return parse_qs(urlparse(u).query)["details"][0]

big = dict(item, notes="y" * 1200, detail="Bring a laptop and the lab handout")
ub, _ = build_reminder_url(big, datetime(2026, 9, 7), "CS advising email")
db = _details(ub)
check("over-cap reminder keeps its summary line", db.startswith("CS-Advising · "), repr(db[:60]))
check("over-cap reminder keeps its provenance line", "Added from your Daily Briefing" in db)
check("over-cap reminder keeps every link",
      "https://forms.umd.edu/sprinternship" in db and "https://umd.zoom.us/j/98765" in db)
check("over-cap reminder keeps When/Where", "When: " in db and "Where: " in db)
check("over-cap reminder respects the 1200 cap", len(db) <= 1200, len(db))
check("over-cap reminder actually uses its budget", len(db) > 900, len(db))
small, _ = build_reminder_url(dict(item, notes="short note"), datetime(2026, 9, 7), "email")
check("under-cap reminder is left completely alone",
      "Added from your Daily Briefing" in _details(small)
      and "short note" in _details(small))
# the assumption disclosure is a required line, so truncation must not eat it
bignoend, _ = build_reminder_url(dict(big, end_time=None), datetime(2026, 9, 7), "email")
check("truncation never drops the assumption disclosure",
      "1 hour assumed" in _details(bignoend))

# F6 — a slug family is only the same event when the listings sit close
# together. Two colloquia a fortnight apart must both survive.
two = ""
for t, slug, dt in [("CMSC Colloquium: Systems", "cmsc-colloquium-3", "2026-09-09"),
                    ("CMSC Colloquium: Theory", "cmsc-colloquium-7", "2026-09-23")]:
    two += ("[%s](https://calendar.umd.edu/%s)\n\n%sT16:00:00\n\n%sT17:00:00\n\n"
            "A talk.\n\n[View Event](https://calendar.umd.edu/%s)\n\n"
            % (t, slug, dt, dt, slug))
kept = uc.dedupe(uc.parse_category_page(two, "academics"))
check("distinct events sharing a slug family both survive", len(kept) == 2,
      [e["title"] for e in kept])
check("distinct same-family events get distinct ids",
      len({e["family"] for e in kept}) == 2)
check("a real multi-day festival still collapses to one row",
      len(uc.dedupe(uc.parse_category_page(text, "current-students"))) == 11)

# F7 — to_item carries the Campus label, unpatched.
ci, curl = CAMPUS["item"], CAMPUS["url"]
check("to_item sets COURSE_LABEL Campus", ci["course_label"] == "Campus", ci.get("course_label"))
check("a campus reminder is label-prefixed",
      _details(curl).startswith("Campus · "), repr(_details(curl)[:40]))

# F8 — the dead classify-stage branch is gone.
check("no dead location_is_offcampus read",
      'get("location_is_offcampus")' not in
      open(os.path.join(HERE, "umd_calendar.py"), encoding="utf-8").read())

# F20 — a bare "apply" no longer captures "Apply for graduation" as an RSVP.
grad = ("## Location\n### [Somewhere](x)\n\n"
        "[Apply for graduation](https://umd.edu/graduation)\n"
        "[RSVP here](https://terplink.umd.edu/event/1)\n")
e2 = uc.enrich_from_detail(grad, {"all_day": False})
check("registration capture ignores 'Apply for graduation'",
      e2["registration_url"] == "https://terplink.umd.edu/event/1",
      e2.get("registration_url"))

# R1 — multi-line notes must be trimmed a line at a time, not all or nothing.
mln = dict(item, notes="\n".join("Requirement %d: %s" % (i, "word " * 11)
                                 for i in range(1, 26)))
dmln = _details(build_reminder_url(mln, datetime(2026, 9, 7), "email")[0])
check("R1 multi-line notes are trimmed, not discarded wholesale",
      0 < sum(1 for l in dmln.split("\n") if l.startswith("Requirement")) < 25,
      sum(1 for l in dmln.split("\n") if l.startswith("Requirement")))
check("R1 the trimmed reminder still uses its budget", len(dmln) > 900, len(dmln))

# R4 — a prose line that merely quotes a URL is droppable; a labelled link is not.
check("R4 a labelled link line is still essential",
      "https://forms.umd.edu/sprinternship" in dmln)

# R5 — a multi-day deadline keeps its span.
u5, _ = build_reminder_url({"id": "m", "course": "ENGL001", "course_label": "ENGL001",
                            "kind": "assignment", "title": "Portfolio", "detail": "d",
                            "date": "2026-09-12", "end_date": "2026-09-14",
                            "time": "23:59", "end_time": None, "links": []},
                           datetime(2026, 9, 7))
check("R5 a multi-day deadline keeps its span in When:",
      "Sep 12 – " in _details(u5) and "11:59 PM ET" in _details(u5),
      repr([l for l in _details(u5).split("\n") if l.startswith("When")]))

# U1 — a campus id carries its date, so a weekly series is a new item each week.
check("U1 a campus item id carries its date",
      re.match(r"^campus-[a-z0-9\-]+-\d{4}-\d{2}-\d{2}$", CAMPUS["item"]["id"])
      is not None, CAMPUS["item"]["id"])
check("U1 no punctuation survives into a campus id (§3.3)",
      "@" not in CAMPUS["item"]["id"])
week = ""
for slug, wk in (("recurring-seminar", "2026-09-09"),
                 ("recurring-seminar-2", "2026-09-17")):
    week += ("[Recurring Seminar](https://calendar.umd.edu/%s)\n\n"
             "%sT16:00:00\n\n%sT17:00:00\n\nA talk.\n\n"
             "[View Event](https://calendar.umd.edu/%s)\n\n" % (slug, wk, wk, slug))
wk_ev = uc.classify(uc.dedupe(uc.parse_category_page(week, "academics")),
                    prefs, date(2026, 9, 7))
check("U1 two occurrences of one series get distinct, date-bearing ids",
      len({e["item_id"] for e in wk_ev}) == len(wk_ev) == 2,
      [e["item_id"] for e in wk_ev])

# U3 — a stated time on an all-day-looking listing is promoted, not discarded.
allday = uc.parse_category_page(
    "[Study Abroad Fair](https://calendar.umd.edu/study-abroad-fair)\n\n"
    "2026-09-09T00:00:00\n\n2026-09-09T00:00:00\n\nA fair.\n\n"
    "[View Event](https://calendar.umd.edu/study-abroad-fair)\n", "academics")[0]
check("U3 a listing with midnight stamps starts out all-day", allday["all_day"])
prom = uc.enrich_from_detail(fixture("detail_study_abroad.md"), allday)
check("U3 the detail page's stated hours are promoted to real times",
      prom["all_day"] is False and prom["start"].hour == 14 and prom["end"].hour == 17,
      (prom["all_day"], prom["start"].isoformat(), prom["end"].isoformat()))

# U5 — every item carries a top-level evidence field (§3.3).
check("U5 to_item sets the top-level evidence field",
      CAMPUS["item"].get("evidence") in ("include", "consider"),
      CAMPUS["item"].get("evidence"))

print("\n== the 2026-09-17 design: 'Daily Briefing — reimagined' ==")
# The page was rebuilt to the design canvas (claude.ai artifact
# RigUM5cJpZovhuixWE9wNq, boards Desktop 1280 and Mobile 390). These pin the
# values taken from it, so a later "small tweak" cannot quietly drift the page
# back toward the old two-column card layout.
_tpl = open(TPL, encoding="utf-8").read()
_css = re.search(r"<style>(.*?)</style>", _tpl, re.S).group(1)

for _tok, _val in (("--ink", "#1B1F2A"), ("--cream", "#FBFAF7"), ("--paper", "#F3F1EA"),
                   ("--line", "#E4E0D3"), ("--line-2", "#D8D3C4"), ("--muted", "#6B6558"),
                   ("--text-on-ink", "#F3EFE6"), ("--text-on-ink-muted", "#A9A395"),
                   ("--accent", "#D8672E"), ("--accent-ink", "#8A3B18"),
                   ("--accent-soft", "#F5E1D2")):
    check("design token %s is %s" % (_tok, _val), "%s: %s;" % (_tok, _val) in _css)
check("the design's three typefaces load",
      "family=Newsreader" in _tpl and "family=Public+Sans" in _tpl
      and "family=IBM+Plex+Mono" in _tpl)
for _cls, _bg, _fg in (("engl", "#E3F1EE", "#1F6B5C"), ("econ", "#E9F2E3", "#3D6B24"),
                       ("math", "#F5E9F2", "#7A3B63"), ("phil", "#F5EBD2", "#7A5A1E"),
                       ("cmsc", "#E7E9F7", "#38408A"), ("advising", "#F7E7EC", "#8A3550")):
    check("course pill %s uses the design's colours" % _cls,
          re.search(r"\.%s\s*\{ background: %s; color: %s; \}" % (_cls, _bg, _fg), _css))
check("the masthead is full-bleed ink with a 1040px content-box column",
      ".mast { width: 100%; background: var(--ink-surface);" in _css
      and "--ink-surface: #1B1F2A;" in _css
      and ".mast-in { box-sizing: content-box; max-width: 1040px; margin: 0 auto; padding: 44px 40px 38px;" in _css)
check("the body column is the same 1040px content-box",
      ".wrap { box-sizing: content-box; max-width: 1040px; margin: 0 auto; padding: 0 40px; }" in _css)
check("the TLDR is the 34px serif headline", re.search(
      r"\.tldr \{ font-family: var\(--serif\); font-size: 34px; line-height: 1\.28;", _css))
check("today's coursework is a two-up card grid",
      ".cards { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }" in _css)
check("this week is five equal columns", "grid-template-columns: repeat(5, minmax(0, 1fr))" in _css)
check("the campus panel's time column is 96px",
      "grid-template-columns: 96px 1fr; gap: 14px; padding: 11px 20px;" in _css)
check("where-you-stand rows are 150px / 1fr / 110px",
      "grid-template-columns: 150px 1fr 110px;" in _css)
check("the phone board has its own breakpoint",
      "@media (max-width: 640px)" in _css and ".tldr { font-size: 23px; line-height: 1.3; }" in _css)
check("below the five-column width the week becomes the design's list",
      "@media (max-width: 860px)" in _css and ".wk-day { display: contents; }" in _css)
check("the page no longer traps scrolling inside body",
      "overflow: hidden" not in re.search(r"html, body \{[^}]*\}", _css).group(0)
      and not re.search(r"\bhtml \{[^}]*overflow: hidden", _css))
check("no tabs or panes are left in the markup",
      'class="pane-tabs"' not in _tpl and 'role="tabpanel"' not in _tpl)
check("no visible row control: the menu button shows on hover/focus only",
      ".act-menu {" in _css and "opacity: 0" in _css[_css.index(".act-menu {"):_css.index(".act-menu {") + 300]
      and ".row:focus-within .act-menu" in _css)
check("clicking a row opens that row's own menu (script block 2)",
      'row.querySelector(\'[data-action="row-menu"]\')' in _tpl and "own.click();" in _tpl)
check("Done hides the row wherever its date put it, page-wide",
      "var sel = '.row[data-item-id=\"' + esc(itemId)" in _tpl
      and "[data-ref=\"' + esc(itemId)" in _tpl)
check("every row template wraps its title so the layouts can place it",
      _tpl.count('<span class="row-name">{{TITLE}}</span>') == 7)

# --- behaviour checks carried over from the 2026-09-11 audits --------------
_boot = _tpl.split("<script>")[1].split("</script>")[0]
_rate_block = _boot[_boot.index("RATING (§15)"):]
check("the rating queue is declared at the block's own scope",
      "var rateQueue" in _boot
      and _boot.index("var rateQueue") < _boot.index("STORES.forEach(function"))
check("the rating handler uses a queue that is actually in scope",
      "rateQueue = rateQueue.then" in _rate_block
      and "writeQueue = writeQueue" not in _rate_block)
check("§15's rating control is documented as a per-row block deletion",
      "div.menu-rate" in _tpl and "def _drop_block(" in src_render)
_opts = re.search(r'id: "max_events".*?options: \[(.*?)\]\],', _tpl, re.S)
_src_orch = open(os.path.join(HERE, "service", "orchestrator.py"), encoding="utf-8").read()
check("the campus cap the quiz offers is what the gate actually enforces",
      _opts and 'caps.get("campus", CAMPUS_MAX)' in src_render
      and 'answers.get("max_events")' in _src_orch and '"campus": campus_cap' in _src_orch)
check("the conflict question names both calendars",
      "personal calendar" in _tpl and "costs you nothing to say yes to" not in _tpl)
check("no locked block still claims there are three of them",
      "compares all THREE" not in _tpl and _tpl.count("compares all FOUR") == 2)
# 2026-09-19: page-level AI grounding ("Plan my week") moved server-side --
# service/app.py's ai_week() queries `items` directly instead of the client
# re-deriving the same set from the rendered DOM (see contextFor()'s own
# comment, which used to hold this gathering).
_src_app = open(os.path.join(HERE, "service", "app.py"), encoding="utf-8").read()
_ai_week_src = _src_app.split("def ai_week")[1].split("# --- Phase 6: preferences")[0]
_ai_week_sql = _ai_week_src.split('conn.execute("""')[1].split('""")')[0]
check("page-level AI grounding reads assignments/assessments plus open attention",
      "kind IN ('assignment', 'assessment')" in _ai_week_sql
      and "needs_attention = 1 AND course_label != 'System'" in _ai_week_sql)
check("page-level AI grounding still excludes the optional campus section",
      "campus" not in _ai_week_sql.lower())
check("Done and Resolved are menu items, not direct buttons",
      'role="menuitem" data-action="mark-done"' in _tpl
      and 'role="menuitem" data-action="mark-resolved"' in _tpl and "act-do" not in _tpl)
check("the due flag shares the row-meta line",
      '<span class="due-flag">{{DUE_FLAG_TEXT}}</span>{{DAYS_OUT_META}}' in _tpl
      and '<div class="due-flag">' not in _tpl)
_due = resolve_meta_slot(block(_tpl, "ASSIGNMENT ROW"), "Due today, 11:59 PM")
_not = resolve_meta_slot(block(_tpl, "ASSIGNMENT ROW"), "")
check("due today keeps the flag and drops the days-out",
      "due-flag" in _due and "{{DAYS_OUT_META}}" not in _due)
check("any other day drops the flag and keeps the days-out",
      "due-flag" not in _not and "{{DAYS_OUT_META}}" in _not)
check("there is one visible focus ring for everything interactive",
      "button:focus-visible, a:focus-visible" in _css and "outline-offset: 2px" in _css)
check("reduced motion is respected", "prefers-reduced-motion" in _css)
check("menu items are a real tap target", "min-height: 40px" in _css)

# The dialog-never-closes bug: layout must be conditioned on [open].
check("the sheet is a flex column only while open",
      re.search(r"\.sheet\[open\] \{[^}]*display: flex; flex-direction: column", _css))
check("no rule sets .sheet's display unconditionally",
      not re.search(r"^\s*\.sheet \{[^}]*display:", _css, re.M)
      and not re.search(r"dialog\.sheet \{[^}]*display: (?!none)", _css))
check("head and foot are pinned; the body is the one scrolling region",
      re.search(r"\.sheet-head \{ flex: none;", _css) and re.search(r"\.sheet-foot \{ flex: none;", _css)
      and re.search(r"\.sheet-body \{ flex: 1 1 auto; min-height: 0;.*overflow-y: auto", _css))
check("print expands disclosures and hides every control",
      "details.more > * { display: revert; }" in _css
      and re.search(r"\.row-actions, \.page-actions, \.more > summary, dialog\.sheet \{ display: none !important; \}", _css))

# --- leads keep their evidence treatment ------------------------------------
check("To confirm / Drop it if are bold in the lead template",
      "<b>To confirm:</b>" in _tpl and "<b>Drop it if:</b>" in _tpl and ".lead-ask b {" in _css)
_basis_row = build_row(_tpl, "leads", {
    "id": "lead-x", "course_class": "none", "course_label": "Lead",
    "title": "T", "confidence_tier": "inferred",
    "basis": [{"claim": "A thing was posted", "source": "the lab site"}],
    "confirm_action": "ask", "kill_criteria": "no reply", "expand_context": "x",
    "times_surfaced": 0})
check("the claim and its source render as separate, escaped pieces",
      "A thing was posted" in _basis_row
      and '<span class="basis-src">— the lab site</span>' in _basis_row)
check("build_row() stamps a lead row with its kind",
      'data-kind="lead"' in _basis_row.split(">")[0])
check("build_row() actually strips the chip when a lead is not new",
      "row-new" not in build_row(_tpl, "leads", {
          "id": "L", "course_class": "none", "course_label": "Lead",
          "title": "T", "confidence_tier": "inferred",
          "basis": [{"claim": "c", "source": "s"}], "confirm_action": "a",
          "kill_criteria": "k", "expand_context": "x", "times_surfaced": 3}))
check("confidence renders in its own row-meta line, with the New chip",
      '<div class="row-meta"><span class="row-new">New</span>'
      '<span class="lead-conf">{{CONFIDENCE}}</span></div>' in _tpl)
check("rowTitleFor() peels every leading label",
      "querySelectorAll(\".course-tag, .lead-conf\")" in _tpl)


print("\n== docs/code consistency (the drift that keeps happening) ==")

# EVERY bug this project has had of the shape "a rule stated in three places,
# changed in one" would have been caught here. The schema version desynced
# (7/8/7) and left live state stuck with the migration firing forever. The gate
# counted six section labels while the prompt said five. The prompt said three
# script blocks after a fourth was added, and said `schema_version: 8` after
# three other places said 10. Grepping is not a strategy; this is.
#
# The docs are optional inputs — the suite stays self-contained (fixtures are
# embedded) — but when they ARE present they get checked.

_DOCS = {}
for _f in ("PROJECT_INSTRUCTIONS.md", "SCHEMA_AND_STATE.md",
           "CAMPUS_AND_PREFERENCES.md", "DISCOVERY.md",
           "DAILY_BRIEFING_PROMPT.md"):
    _p = os.path.join(HERE, _f)
    if os.path.exists(_p):
        _DOCS[_f] = open(_p, encoding="utf-8").read()
_ALL = "\n".join(_DOCS.values())

if not _DOCS:
    print("  SKIP  no Instructions files beside the suite; nothing to cross-check")
else:
    check("all five Instructions files are present to check against",
          len(_DOCS) == 5, sorted(_DOCS))

    def _words(n):
        return {1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
                6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve"}[n]

    # 1. locked script blocks: template vs every prose claim about them
    _n = len(re.findall(r"<script>", tpl))
    check("the docs name the real number of locked script blocks",
          not re.search(r"(?:all |the )(one|two|three|five|six) `?<script>`?", _ALL)
          and ("%s `<script>`" % _words(_n) in _ALL
               or "%s script blocks" % _words(_n) in _ALL),
          "template has %d; prose says %r" % (_n, re.findall(
              r"(?:all |the )(one|two|three|four|five) `?<script>`?|"
              r"(?:are \*\*)?(one|two|three|four|five)\*\*? script blocks", _ALL)[:4]))

    # 2. gate checks: highest number implemented vs the count claimed
    _impl = max(int(m) for m in re.findall(r'p\.append\("(\d+)', src_render))
    check("the docs name the real number of gate checks",
          "all %d checks" % _impl in _ALL, "implemented up to %d" % _impl)

    # 3/4. the closed sets live in §2.1, in 7f and in code — all three or none
    _7f = re.search(r"`\{\{COURSE_LABEL\}\}` \| Exactly one of `([^`]+)`", _ALL)
    check("7f's COURSE_LABEL set matches render_briefing.COURSE_LABELS",
          _7f and set(_7f.group(1).split()) == COURSE_LABELS_SET,
          (sorted(set(_7f.group(1).split())) if _7f else None))
    _7fc = re.search(r"`\{\{COURSE_CLASS\}\}` \| Exactly one of `([^`]+)`", _ALL)
    check("7f's COURSE_CLASS set matches render_briefing.COURSE_CLASSES",
          _7fc and set(_7fc.group(1).split()) == COURSE_CLASSES_SET,
          (sorted(set(_7fc.group(1).split())) if _7fc else None))
    check("§2.1's label table lists exactly the same labels",
          all(("`%s`" % l) in _DOCS.get("SCHEMA_AND_STATE.md", "")
              for l in COURSE_LABELS_SET))

    # 5. schema_version, the desync that actually happened
    _vers = set(re.findall(r"schema_version\` \| \`(\d+)\`", _ALL)) \
        | set(re.findall(r"schema_version` is below `(\d+)`", _ALL)) \
        | set(re.findall(r"leaves it at `(\d+)`", _ALL)) \
        | set(re.findall(r"schema to version (\d+) if needed", _ALL)) \
        | set(re.findall(r"Set `schema_version: (\d+)`", _ALL))
    check("every stated schema_version agrees", len(_vers) == 1, sorted(_vers))

    # 6. section labels: the gate's expected list vs the template's own markup
    _tpl_labels = [re.sub(r'<span class="m-only">.*?</span>', "", l)
                   .replace("{{CAMPUS_HEADING}}", "Today — around campus")
                   for l in re.findall(r'<h2 class="section-label">(.*?)</h2>', tpl)]
    check("the gate's expected section labels are exactly the template's",
          _tpl_labels == GATE_LABELS, (_tpl_labels, GATE_LABELS))
    check("the docs name the real number of section labels",
          "%s `.section-label`" % _words(len(GATE_LABELS)) in _ALL
          or "counts %s" % _words(len(GATE_LABELS)) in _ALL,
          len(GATE_LABELS))

    # 7-9. numeric constants named in prose AND defined in code
    for label, value, patterns in (
            ("FAMILY_WINDOW_DAYS", uc.FAMILY_WINDOW_DAYS,
             (r"slug-family window \| (\d+) days", r"FAMILY_WINDOW_DAYS`? \((\d+)\)")),
            ("reminder details cap", __import__("reminder_url").DETAILS_CAP,
             (r"Add-reminder `details` cap \| (\d+)", r"`details` at \*\*(\d+)\*\*")),
            ("EXPAND_CONTEXT cap", LIMITS["EXPAND_CONTEXT"],
             (r"max length \| (\d+) characters",)),
            ("DETAIL cap", LIMITS["DETAIL"], (r"`\{\{DETAIL\}\}` (\d+)",)),
            ("TLDR cap", LIMITS["TLDR"], (r"`\{\{TLDR\}\}` (\d+)",))):
        found = set()
        for pat in patterns:
            found |= set(int(x) for x in re.findall(pat, _ALL))
        check("%s: code says %d and the docs agree" % (label, value),
              found and found == {value}, sorted(found) or "not stated in any doc")

    # 10. the item prune window, stated in two files
    _prune = set(int(x) for x in re.findall(r"more than \*\*(\d+) days\*\* in the past", _ALL)) \
        | set(int(x) for x in re.findall(r"than (\d+) days in the past: delete", _ALL))
    check("the item prune window agrees across files", len(_prune) == 1, sorted(_prune))

    # 11. empty-notes: code, §5.2's table, and the bootstrap script
    for _k, _v in EMPTY_NOTES_MAP.items():
        check("empty-note for %s is identical in code, docs and template" % _k,
              _v in _ALL and (_v in tpl or _k in ("coverage", "portfolio",
                                                  "leads", "opportunities")),
              _v)

    # 12. the never-write set, which grew and must have grown everywhere
    for _doc in ("prefs/quiz", "prefs/custom", "prefs/requirements"):
        check("%s is on the never-write list in the Instructions" % _doc,
              _doc in _DOCS.get("PROJECT_INSTRUCTIONS.md", ""))

    # 13. the one hardcoded recipient — a literal string, stated twice
    _rcpt = set(re.findall(r"[a-z]+@terpmail\.umd\.edu", _ALL))
    check("exactly one recipient address appears anywhere", len(_rcpt) == 1, sorted(_rcpt))

    # 14. §0's file table and the files on disk must agree — BOTH directions.
    # The named->exists half alone let four of the five test suites sit
    # unlisted indefinitely: the table said "test_briefing.py" and nothing
    # noticed the other four, because nothing was looking that way.
    _pi = _DOCS.get("PROJECT_INSTRUCTIONS.md", "")
    _named = set(re.findall(r"\*\*([A-Za-z_]+\.(?:md|py|html))\*\*", _pi))
    _named |= set(re.findall(r"`([A-Za-z_]+\.(?:md|py|html))`", _pi))
    _absent = sorted(f for f in _named
                     if not os.path.exists(os.path.join(HERE, f))
                     # Exempt by §0's own text: BUTTONS_AND_DELIVERY.md is a
                     # historical reference ("do not split them out again"),
                     # and files prefixed `example_` or `fixture_` are exempt
                     # "in both directions" — they are documented as optional,
                     # their absence "is never an error". This used to list
                     # the two fixture names literally, so adding
                     # `example_rendered_briefing.html` to the table turned
                     # the suite red for a file §0 says may not exist.
                     and f != "BUTTONS_AND_DELIVERY.md"
                     and not f.startswith(("example_", "fixture_")))
    check("every file §0's table names is actually present", not _absent, _absent)

    # `fixture_` and `example_` prefixes are reference material, not pipeline
    # files: §0's optional row documents both, and neither ships nor is
    # required. Everything else on disk must be named in the Instructions.
    _ondisk = {f for f in os.listdir(HERE)
               if f.endswith((".py", ".md", ".html"))
               and not f.startswith(("fixture_", "example_"))}
    _unlisted = sorted(_ondisk - _named)
    check("every file on disk is named somewhere in the Instructions",
          not _unlisted, _unlisted)

    # 15. every module.function the docs name must actually exist. This is the
    # check that would have caught `collect.harvest()` — uncallable by design
    # yet named in three documents, with 22 passing tests behind it.
    import importlib
    _mods = {}
    for _m in ("render_briefing", "reminder_url", "umd_calendar", "opportunity",
               "collect", "ledger", "umd_dates",
               "lifecycle", "schedule", "feedback", "state_io", "run_stats"):
        try:
            _mods[_m] = importlib.import_module(_m)
        except ImportError:
            pass
    _missing = []
    for _m, _fn in set(re.findall(
            r"`(render_briefing|reminder_url|umd_calendar|opportunity|collect|"
            r"ledger|umd_dates)\.([A-Za-z_][A-Za-z0-9_]*)", _ALL)):
        if _fn in ("py",):          # `collect.py` is a filename, not an attribute
            continue
        if _m in _mods and not hasattr(_mods[_m], _fn):
            _missing.append("%s.%s" % (_m, _fn))
    check("every module function the docs name actually exists",
          not _missing, sorted(_missing))

    # 15b. Regression from the live state file, 2026-09-09: ids minted before
    # this month end in a COMPACT date (`-20260910`), not §3.3's hyphenated
    # form. _family() stripped only the hyphenated one, so every legacy item's
    # family equalled its whole id and recurrence could never match one year to
    # the next — silently, because that family looks well-formed.
    import ledger as _G
    check("ledger family strips both date shapes in live state",
          _G._family("math001-quiz1-20260910") == "math001-quiz1"
          and _G._family("campus-fair-2026-09-15") == "campus-fair",
          (_G._family("math001-quiz1-20260910"), _G._family("campus-fair-2026-09-15")))

    # 15c. Gmail label names: the live account nests them under College/Canvas/,
    # and §2.3 asserted College/ for months. The account is authoritative; this
    # only checks the docs no longer assert the flatter form as fact.
    check("§2.3 names the labels the account actually has",
          "College/Canvas/ENGL001" in _ALL
          and "| `College/ENGL001` …" not in _ALL)

    # 15d. learned_patterns keys that exist in live state must be documented,
    # or a state rewrite drops them and the sweep starts misfiling Canvas mail.
    for _k in ("sender_overrides", "canvas_domains", "canvas_nicknames"):
        check("learned_patterns.%s is documented in the schema" % _k,
              ('"%s"' % _k) in _DOCS.get("SCHEMA_AND_STATE.md", ""))

    # 15e. The 2026-09-11 defects were all "the docs said one thing and the
    # code did another, and no test looked at both." These look at both.
    _pi = _DOCS.get("PROJECT_INSTRUCTIONS.md", "")
    _pr = _DOCS.get("DAILY_BRIEFING_PROMPT.md", "")
    _sc = _DOCS.get("SCHEMA_AND_STATE.md", "")

    import inspect
    import lifecycle as _L

    # §5.7 is scoped to attention rows — the check that was missing when
    # auto_dismiss() would have dismissed a midterm for being read 5 times.
    _ad = inspect.getsource(_L.auto_dismiss)
    check("auto_dismiss() gates on in_attention()", "in_attention(it)" in _ad)
    check("auto_dismiss() covers unresolved rows, per §5.9",
          '"unresolved"' in _ad.split("for it in items")[1].split("continue")[0])
    check("§5.7 says the scope is attention rows only",
          "in_attention" in _pi and "attention rows only" in _pi)
    check("prompt step 6.5 lists the same three statuses",
          "`new`/`ongoing`/`unresolved`" in _pr)

    # Nothing may retire by both rules or by neither: expire() skips exactly
    # what auto_dismiss() covers.
    check("expire() and auto_dismiss() share one predicate",
          "in_attention(it)" in inspect.getsource(_L.expire))

    # §5.7 exception 2 is dead unless something sets the flag it reads.
    check("config_unfixed is documented in the schema", "config_unfixed" in _sc)
    check("§5.7 names who sets config_unfixed",
          "config_unfixed" in _pi and "sets `config_unfixed" in _pi)

    # relevance is an object; reading it as a number raised TypeError on any
    # run with two campus events to sort.
    check("order_campus() reads relevance through relevance_score()",
          "relevance_score(i)" in inspect.getsource(_L.order_campus))
    check("relevance_score() survives a dict, a bare number and null",
          _L.relevance_score({"relevance": {"score": 7}}) == 7.0
          and _L.relevance_score({"relevance": 4}) == 4.0
          and _L.relevance_score({"relevance": None}) == 0.0)

    # The publish read is bounded in both places a run might look.
    check("§7 bounds the pre-publish read to one call",
          "One read call, and only one" in _pi)
    check("STEP 9 bounds it too", "Exactly one read call" in _pr)

    # 16. every tuned constant in code must be named in a rule. §2 says
    # "hardcode these, do not re-derive" — a cap that lives only in code is one
    # nobody can find when it needs changing, and five of them were orphaned.
    # A module marked PARKED is exempt: the rule that named its cap was removed
    # with the step it belonged to (umd_dates, 2026-09-10). Documenting a cap
    # as live for a feature no run imports is worse than not documenting it.
    _orphans = []
    for _m, _n in (("collect", "MAX_SEARCHES_PER_RUN"),
                   ("collect", "MAX_FETCHES_PER_RUN"),
                   ("collect", "CACHE_DAYS"),
                   ("umd_dates", "REFRESH_AFTER_DAYS"),
                   ("opportunity", "LEAD_MAX_SURFACES"),
                   ("umd_calendar", "FAMILY_WINDOW_DAYS"),
                   ("reminder_url", "DETAILS_CAP"),
                   # Added 2026-09-10 with lifecycle/schedule/feedback. Six of
                   # these were documented by VALUE only ("3 or more", "under
                   # 20 minutes", "40%"), so the rule was findable and the code
                   # behind it was not. Naming them here makes that a gate
                   # failure rather than something to notice by hand.
                   ("lifecycle", "GROUP_MIN"),
                   ("lifecycle", "COMING_UP_MAX"),
                   ("lifecycle", "EXPIRE_AFTER_DAYS"),
                   ("lifecycle", "AUTO_DISMISS_AT"),
                   ("lifecycle", "STUDY_MIN_DAYS"),
                   ("schedule", "TIGHT_MINUTES"),
                   ("schedule", "POP_THRESHOLD"),
                   ("feedback", "MIN_DOWN"),
                   ("feedback", "MAX_WEIGHT")):
        if _m not in _mods:
            continue
        if getattr(_mods[_m], "PARKED", False):
            continue
        _v = getattr(_mods[_m], _n)
        if _n not in _ALL or str(_v) not in _ALL:
            _orphans.append("%s=%s" % (_n, _v))
    check("every tuned cap in code is named with its value in the rules",
          not _orphans, sorted(_orphans))

    # 17. terminology that changed meaning. A grep the consistency block above
    # cannot do, because these are not numbers stated twice — they are words
    # whose referent moved (the Drive copy stopped being HTML).
    for _term, _why in (("college_brief_YYYY-MM-DD.html", "the Drive record is JSON now"),
                        ("§8.2", "never existed"),
                        ("harvest(", "removed from collect.py"),
                        ("{{WEATHER_SUMMARY}}", "placeholder removed")):
        check("no stale reference to %r (%s)" % (_term, _why),
              _term not in _ALL)

    # 15. the load path: the tool it names must be one that exists
    check("the docs do not tell a run to use a nonexistent project_read tool",
          not re.search(r"Read them with `project_read`", _ALL))
    check("the docs point runs at /mnt/project for the Instructions",
          "/mnt/project" in _DOCS.get("DAILY_BRIEFING_PROMPT.md", ""))

print("\n%s" % ("-" * 60))
if FAILURES:
    print("FAILED (%d): %s" % (len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("All checks passed.")
