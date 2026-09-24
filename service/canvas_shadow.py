"""
Phase 4 shadow-mode Canvas integration (see the "Canvas Knowledge Index"
architecture proposal, sections F and G, for the full design this
implements).

What this is: a standalone, READ-ONLY tool that reads canvas-scraper's
index (~/canvas-scraper/output/latest.json + cache.db) and this project's
own briefing.db `items` table, and reports how well the two sources agree
for assignment/assessment items -- Phase B/C of the staged migration
(compare the scraper against the existing Canvas-notification-email path
before either becomes authoritative for the other).

What this deliberately is NOT: wired into orchestrator.py's pipeline in
any way. Running `python service/canvas_shadow.py` touches nothing Daily
Briefing renders, sends, or persists -- it never writes to briefing.db,
only reads it (with PRAGMA query_only enforced defensively). A later
phase (not this one) would decide whether/how get_canvas_changes() feeds
an actual STEP; that decision needs real comparison data from this tool
first, which is the whole point of a shadow phase.

Run manually:
    python service/canvas_shadow.py --since-hours 24
    python service/canvas_shadow.py --json shadow_report.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

CANVAS_SCRAPER_ROOT = Path.home() / "canvas-scraper"
CANVAS_LATEST_JSON = CANVAS_SCRAPER_ROOT / "output" / "latest.json"
CANVAS_CACHE_DB = CANVAS_SCRAPER_ROOT / "cache.db"
BRIEFING_DB = Path(__file__).resolve().parent / "briefing.db"

_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


class NoCanvasScrapeError(RuntimeError):
    pass


def _normalize_title(title: Optional[str]) -> str:
    return _NORMALIZE_RE.sub(" ", (title or "").lower()).strip()


def _parse_date(d: Optional[str]) -> Optional[_dt.date]:
    if not d:
        return None
    try:
        return _dt.date.fromisoformat(d[:10])
    except ValueError:
        return None


def load_canvas_scrape(path: Path = CANVAS_LATEST_JSON) -> dict:
    if not path.exists():
        raise NoCanvasScrapeError(
            f"No canvas-scraper output at {path}. Run `canvas refresh` in ~/canvas-scraper first."
        )
    return json.loads(path.read_text())


def _course_code(course: dict) -> str:
    return course.get("course_code") or course.get("name") or str(course.get("id"))


def _course_code_by_id(scrape: dict) -> dict[int, str]:
    return {b["course"]["id"]: _course_code(b["course"]) for b in scrape.get("courses", [])}


# -- architecture proposal section F.1's interface -----------------------


@dataclass
class CanvasChangeEvent:
    """Matches architecture proposal section F.2's compact record shape."""

    change_kind: str  # "new" | "changed" | "removed"
    course: str
    object_type: str  # "assignment" | "quiz" | "discussion" | "page" | "announcement" | ...
    title: str
    canvas_url: Optional[str]
    field_changes: list[dict] = field(default_factory=list)
    detected_at: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "type": f"{self.object_type}_{self.change_kind}",
            "course": self.course,
            "title": self.title,
            "canvas_url": self.canvas_url,
            "field_changes": self.field_changes,
            "summary": self._summary(),
            "detected_at": self.detected_at,
        }

    def _summary(self) -> str:
        if self.change_kind in ("new", "removed"):
            return f"{self.object_type} {self.change_kind}: {self.title}"
        parts = ", ".join(f"{fc['field']}: {fc['before']} -> {fc['after']}" for fc in self.field_changes)
        return f"{self.object_type} changed: {self.title} ({parts})" if parts else f"{self.object_type} changed: {self.title}"


def get_canvas_changes(
    since: Optional[float] = None,
    until: Optional[float] = None,
    courses: Optional[set[str]] = None,
    cache_db: Path = CANVAS_CACHE_DB,
    scrape: Optional[dict] = None,
) -> list[dict]:
    """
    Reads canvas-scraper's cache.db `change_log` directly. canvas-scraper
    doesn't expose this as an importable library function yet (out of
    scope for this phase to add a public API surface to that project);
    reading its SQLite file directly -- opened read-only via PRAGMA
    query_only -- is this phase's stable interface for a same-machine
    consumer, matching how canvas-scraper's own CLI (`canvas changes`)
    already reads the identical table.
    """
    if not cache_db.exists():
        return []
    scrape = scrape if scrape is not None else load_canvas_scrape()
    code_by_id = _course_code_by_id(scrape)

    conn = sqlite3.connect(str(cache_db))
    conn.execute("PRAGMA query_only = TRUE")
    clauses, params = [], []
    if since is not None:
        clauses.append("run_at > ?")
        params.append(since)
    if until is not None:
        clauses.append("run_at <= ?")
        params.append(until)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT course_id, entity_type, kind, title, before_json, after_json "
        f"FROM change_log {where} ORDER BY run_at DESC",
        params,
    ).fetchall()
    conn.close()

    events = []
    for course_id, entity_type, kind, title, before_json, after_json in rows:
        code = code_by_id.get(course_id, str(course_id))
        if courses and code not in courses:
            continue
        before = json.loads(before_json) if before_json else None
        after = json.loads(after_json) if after_json else None
        field_changes = []
        if kind == "changed" and before and after:
            for key in sorted(set(before) | set(after)):
                if before.get(key) != after.get(key):
                    field_changes.append({"field": key, "before": before.get(key), "after": after.get(key)})
        events.append(
            CanvasChangeEvent(
                change_kind=kind,
                course=code,
                object_type=entity_type,
                title=title or "(untitled)",
                canvas_url=(after or before or {}).get("html_url"),
                field_changes=field_changes,
            ).as_dict()
        )
    return events


def get_upcoming_assignments(
    within_days: int = 7,
    courses: Optional[set[str]] = None,
    scrape: Optional[dict] = None,
    now: Optional[_dt.datetime] = None,
) -> list[dict]:
    scrape = scrape if scrape is not None else load_canvas_scrape()
    now = now or _dt.datetime.now(_dt.timezone.utc)
    cutoff = now + _dt.timedelta(days=within_days)
    out = []
    for bundle in scrape.get("courses", []):
        code = _course_code(bundle["course"])
        if courses and code not in courses:
            continue
        for a in bundle.get("assignments", []):
            due = a.get("due_at")
            if not due:
                continue
            try:
                due_dt = _dt.datetime.fromisoformat(due.replace("Z", "+00:00"))
            except ValueError:
                continue
            if now <= due_dt <= cutoff:
                out.append(
                    {
                        "course": code,
                        "title": a["name"],
                        "due_at": due,
                        "canvas_url": a.get("html_url"),
                        "object_id": a["id"],
                    }
                )
    out.sort(key=lambda i: i["due_at"])
    return out


# -- Phase 5: cutover -- feeding orchestrator.py's own reconcile step -----
#
# Rather than building a second, parallel merge path, this shapes
# canvas-scraper's assignments/quizzes into the EXACT candidate dict
# shape orchestrator.py's step1_2_gmail_sweep already produces, so
# step3_reconcile's existing per-(course, title, date-window) clustering
# and source-authority ordering does the actual merging -- one
# reconciliation mechanism, not two. The caller (orchestrator.py) is
# responsible for running each candidate's `course` through the same
# `_normalize_course()` grounding gmail candidates already go through,
# and for filtering to `state["courses"]` -- this function stays
# state-shape-agnostic on purpose, matching how canvas_shadow.py's other
# functions take pre-loaded data rather than reading Daily Briefing's own
# config.
#
# For this to actually take precedence over a same-item Gmail candidate,
# orchestrator.py's step3_reconcile._AUTHORITY table must rank
# "canvas_scraper" above "gmail" (see the architecture proposal, section
# F.3: Syllabi Dates > Canvas scraper > Canvas calendar > Canvas email).

CANVAS_SCRAPER_SOURCE = "canvas_scraper"


def _due_date_and_time(due_at: Optional[str], tz) -> tuple[Optional[str], Optional[str]]:
    """Canvas's due_at is UTC; a student reads Canvas's own UI in local
    time, and Daily Briefing's `items.time` column is already always
    local "HH:MM" (see real rows: "23:59", "08:00") -- converting here
    keeps a scraper-sourced item's displayed time consistent with what a
    Gmail-extracted item for the same deadline would have shown."""
    if not due_at:
        return None, None
    try:
        dt_utc = _dt.datetime.fromisoformat(due_at.replace("Z", "+00:00"))
    except ValueError:
        return None, None
    local = dt_utc.astimezone(tz)
    return local.date().isoformat(), local.strftime("%H:%M")



def canvas_submission_summary(submission: Optional[dict]) -> dict:
    """Canvas's own record of whether this student is DONE with an object,
    reduced to the three facts the briefing acts on (2026-09-19 audit).

    `settled` -- Canvas holds a submission, a grade, or an excusal: there is
    nothing left for the student to hand in, whatever the title or due date
    says. `workflow_state == "graded"` counts even with no submission (an
    on-paper quiz the instructor graded, or a zero entered for missed work):
    either way it is no longer something to act on, and a low score is the
    grade-trend readout's job, not "may have been missed".
    `missing` -- Canvas's own missing flag, the strongest overdue signal
    there is (set by Canvas, not inferred from a date by this pipeline).
    """
    s = submission or {}
    state = s.get("workflow_state")
    settled = bool(
        state in ("submitted", "graded", "pending_review")
        or s.get("submitted_at") or s.get("score") is not None
        or s.get("excused"))
    return {"state": state, "settled": settled,
            "missing": bool(s.get("missing")),
            "excused": bool(s.get("excused"))}


def scrape_age_hours(scrape: dict, now: Optional[_dt.datetime] = None) -> Optional[float]:
    """Hours since canvas-scraper's own `scraped_at` stamp, or None when the
    scrape carries no parseable stamp. A stale scrape is still the best
    Canvas data available -- the caller decides how loudly to say so."""
    raw = (scrape or {}).get("scraped_at")
    if not raw:
        return None
    try:
        at = _dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=_dt.timezone.utc)
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return max(0.0, (now - at).total_seconds() / 3600.0)


def canvas_authoritative_candidates(scrape: Optional[dict] = None, tz=None) -> list[dict]:
    """
    One candidate dict per due-dated assignment/quiz, in
    step1_2_gmail_sweep's exact output shape (thread_id, course, title,
    kind, due_date, due_time, end_time, location, canvas_url, organizer,
    description, links, confidence, source). `confidence` is always
    "confirmed" -- unlike an LLM's parse of prose, these dates come
    directly from the Canvas API, so the "inferred" case (architecture
    proposal section G.1) doesn't apply.

    `thread_id` is synthetic (`canvas-scraper:<type>:<course_id>:<id>`)
    since these never came from a Gmail thread -- step3_reconcile only
    uses thread_id to build `source_refs` entries, so a stable synthetic
    id is exactly as useful there as a real one, and lets a later run
    trace a merged item's scraper-derived component back to the exact
    canvas-scraper object.

    Items with no due date are omitted -- undated coursework isn't
    something step3_reconcile's date-clustering can merge against
    anything, and isn't "due soon" in the sense the briefing surfaces.
    """
    if tz is None:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("America/New_York")
    scrape = scrape if scrape is not None else load_canvas_scrape()
    out = []
    for bundle in scrape.get("courses", []):
        code = _course_code(bundle["course"])
        course_id = bundle["course"]["id"]
        for a in bundle.get("assignments", []):
            due_date, due_time = _due_date_and_time(a.get("due_at"), tz)
            if not due_date:
                continue
            out.append(
                {
                    "thread_id": f"canvas-scraper:assignment:{course_id}:{a['id']}",
                    "course": code,
                    "title": a["name"],
                    "kind": "assignment",
                    "due_date": due_date,
                    "due_time": due_time,
                    "end_time": None,
                    "location": None,
                    "canvas_url": a.get("html_url"),
                    "organizer": None,
                    "description": None,
                    "links": [],
                    "confidence": "confirmed",
                    "source": CANVAS_SCRAPER_SOURCE,
                    # Canvas's own record of what handing this in even means
                    # -- e.g. `["none"]` for a discussion-less participation
                    # entry, `["external_tool"]` for a clicker/LTI activity,
                    # `["online_text_entry"]` for real prose to submit. Kept
                    # as the raw list rather than collapsed here: the
                    # pipeline's job is to preserve what Canvas actually
                    # said so lifecycle.action_tag() (and Michael, via the
                    # item's own detail) can inspect it, not to decide once
                    # and discard the evidence.
                    "submission_types": a.get("submission_types") or [],
                    "canvas_submission": canvas_submission_summary(a.get("submission")),
                    "points_possible": a.get("points_possible"),
                }
            )
        for q in bundle.get("quizzes", []):
            due_date, due_time = _due_date_and_time(q.get("due_at"), tz)
            if not due_date:
                continue
            # A quiz has no `submission_types` (that's an assignment-object
            # field); `quiz_type` is its own analogue -- "survey"/
            # "graded_survey" is filled in for opinion but never graded on
            # correctness, closer to a participation check than a real
            # assessment. Folded into the same field name so
            # lifecycle.action_tag() has one place to look regardless of
            # which Canvas object produced the row.
            out.append(
                {
                    "thread_id": f"canvas-scraper:quiz:{course_id}:{q['id']}",
                    "course": code,
                    "title": q["title"],
                    "kind": "assessment",
                    "due_date": due_date,
                    "due_time": due_time,
                    "end_time": None,
                    "location": None,
                    "canvas_url": q.get("html_url"),
                    "organizer": None,
                    "description": None,
                    "links": [],
                    "confidence": "confirmed",
                    "source": CANVAS_SCRAPER_SOURCE,
                    "submission_types": [q["quiz_type"]] if q.get("quiz_type") else [],
                    # A quiz object carries no submission, but `attempts_taken`
                    # is Canvas's own count of finished attempts.
                    "canvas_submission": {
                        "state": None,
                        "settled": bool(q.get("attempts_taken")),
                        "missing": False, "excused": False},
                    "points_possible": q.get("points_possible"),
                }
            )
    return out


# -- Phase B/C: shadow comparison against Daily Briefing's email-sourced items --


def _briefing_canvas_email_items(briefing_db: Path) -> list[dict]:
    """Every item in briefing.db whose sources include at least one Gmail
    (Canvas-notification-email) reference and is assignment/assessment-
    shaped work -- the population this migration is meant to eventually
    replace. Read-only: PRAGMA query_only defensively refuses any write
    this connection might otherwise attempt."""
    conn = sqlite3.connect(str(briefing_db))
    conn.execute("PRAGMA query_only = TRUE")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, course, title, kind, date, canvas_url, extra_json FROM items "
        "WHERE kind IN ('assignment', 'assessment')"
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        extra = json.loads(r["extra_json"] or "{}")
        refs = extra.get("source_refs") or []
        if not any(ref.get("source") == "gmail" for ref in refs):
            continue
        out.append(
            {
                "id": r["id"],
                "course": r["course"],
                "title": r["title"],
                "kind": r["kind"],
                "date": r["date"],
                "canvas_url": r["canvas_url"],
                "source_refs": refs,
            }
        )
    return out


def _scraper_work_items(scrape: dict) -> list[dict]:
    out = []
    for bundle in scrape.get("courses", []):
        code = _course_code(bundle["course"])
        for a in bundle.get("assignments", []):
            out.append(
                {"course": code, "title": a["name"], "date": (a.get("due_at") or "")[:10] or None,
                 "canvas_url": a.get("html_url"), "kind": "assignment"}
            )
        for q in bundle.get("quizzes", []):
            out.append(
                {"course": code, "title": q["title"], "date": (q.get("due_at") or "")[:10] or None,
                 "canvas_url": q.get("html_url"), "kind": "quiz"}
            )
    return out


def compare_with_briefing_items(
    briefing_db: Path = BRIEFING_DB, scrape: Optional[dict] = None
) -> dict:
    """
    Cross-references every Gmail-sourced assignment/assessment item Daily
    Briefing currently tracks against canvas-scraper's own assignments/
    quizzes for the same course, matched by (course, normalized title,
    date within +/-1 day) -- the same tolerance Daily Briefing's own
    internal reconciliation already uses (SCHEMA_AND_STATE.md's item-
    matching rule), so this comparison holds itself to the same notion
    of "the same item" the production system already applies to itself.

    Never mutates either database. Returns counts + the actual matched/
    email-only/scraper-only lists for a human (or a later automated
    validation pass) to inspect before any cutover decision is made.
    """
    scrape = scrape if scrape is not None else load_canvas_scrape()
    scraper_items = _scraper_work_items(scrape)
    email_items = _briefing_canvas_email_items(briefing_db)

    matched, email_only = [], []
    matched_scraper_idx: set[int] = set()

    for item in email_items:
        item_date = _parse_date(item["date"])
        item_norm = _normalize_title(item["title"])
        found = None
        for idx, s in enumerate(scraper_items):
            if idx in matched_scraper_idx or s["course"] != item["course"]:
                continue
            s_date = _parse_date(s["date"])
            if item_date and s_date and abs((item_date - s_date).days) > 1:
                continue
            s_norm = _normalize_title(s["title"])
            if item_norm and s_norm and (item_norm == s_norm or item_norm in s_norm or s_norm in item_norm):
                found = idx
                break
        if found is not None:
            matched_scraper_idx.add(found)
            matched.append({"briefing_item": item, "scraper_item": scraper_items[found]})
        else:
            email_only.append(item)

    scraper_only = [s for idx, s in enumerate(scraper_items) if idx not in matched_scraper_idx]

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "counts": {
            "matched": len(matched),
            "email_only": len(email_only),
            "scraper_only": len(scraper_only),
        },
        "matched": matched,
        "email_only": email_only,
        "scraper_only": scraper_only,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since-hours", type=float, default=24.0)
    parser.add_argument("--json", type=Path, help="Write the full comparison report to this path")
    args = parser.parse_args(argv)

    scrape = load_canvas_scrape()
    since = time.time() - args.since_hours * 3600
    changes = get_canvas_changes(since=since, scrape=scrape)
    upcoming = get_upcoming_assignments(scrape=scrape)
    comparison = compare_with_briefing_items(scrape=scrape)

    print(f"canvas-scraper changes in the last {args.since_hours:.0f}h: {len(changes)}")
    for c in changes[:20]:
        print(f"  [{c['course']}] {c['type']}: {c['title']}")

    print(f"\nUpcoming assignments (next 7d): {len(upcoming)}")
    for u in upcoming[:20]:
        print(f"  [{u['course']}] {u['due_at']}: {u['title']}")

    print("\nShadow comparison vs Daily Briefing's Gmail-sourced items:")
    print(f"  matched:      {comparison['counts']['matched']}")
    print(f"  email-only:   {comparison['counts']['email_only']}  (Daily Briefing has it, scraper match not found)")
    print(f"  scraper-only: {comparison['counts']['scraper_only']}  (scraper has it, not surfaced from email)")
    for item in comparison["email_only"]:
        print(f"    email-only: [{item['course']}] {item['title']} ({item['date']})")

    if args.json:
        args.json.write_text(
            json.dumps({"changes": changes, "upcoming": upcoming, "comparison": comparison}, indent=2, default=str)
        )
        print(f"\nFull report written to {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
