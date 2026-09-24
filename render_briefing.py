"""
render_briefing.py — splice the template, then refuse to ship a broken page.

Rebuilt 2026-09-17 around the "Daily Briefing — reimagined" design: the page
is no longer three tabbed panes of type-named sections, it is one column read
top to bottom — Today (coursework cards, calendar, campus), This week (five
day columns), Needs your attention, Where you stand, Further out — and a row
lands where its DATE puts it. What a row can DO is unchanged: the pipeline
bucket it came from still picks its row template (Mark done only on an
assignment, Add reminder only on a coming-up row …), and build_row() stamps
that bucket on the row as data-kind so the gate checks behaviour per row
rather than per section.
"""

import datetime as _dt
import html as _html
import re
from urllib.parse import parse_qs, urlparse

MARKERS = {
    "pills": "<!-- INSERT MASTHEAD PILLS HERE -->",
    "today": "<!-- INSERT TODAY CARDS HERE -->",
    "schedule": "<!-- INSERT SCHEDULE HERE -->",
    "campus": "<!-- INSERT CAMPUS HERE -->",
    "week": "<!-- INSERT WEEK HERE -->",
    "attention": "<!-- INSERT ATTENTION ROWS HERE -->",
    # §5.1c. System-housekeeping rows and any cap overflow, behind their own
    # disclosure inside Needs your attention — demoted, not dropped.
    "attention_more": "<!-- INSERT SECONDARY ATTENTION ROWS HERE -->",
    "standing": "<!-- INSERT STANDING HERE -->",
    "further": "<!-- INSERT FURTHER OUT HERE -->",
    # §16c. A pure-FYI event/advising row (lifecycle.is_fyi()) behind its own
    # disclosure inside Further out — same "demoted, not dropped" treatment
    # attention_more above gives Needs your attention's own overflow.
    "further_more": "<!-- INSERT SECONDARY FURTHER ROWS HERE -->",
    "leads": "<!-- INSERT LEAD ROWS HERE -->",
    "changes": "<!-- INSERT CHANGES HERE -->",
    "opportunities": "<!-- INSERT OPPORTUNITY ROWS HERE -->",
    "coverage": "<!-- INSERT COVERAGE NOTES HERE -->",
}

# Sections that always render, with the one-line note an empty one shows.
EMPTY_NOTES = {
    "today": "Nothing due today.",
    "attention": "Nothing needs your attention.",
    # Says what happened, not "nothing is on": the fetch may have run and
    # matched nothing, which is a statement about the filter, not the campus.
    "campus": "No campus events matched your preferences this week.",
}

# Sections that are deleted as a unit when they have nothing to show. The
# design has no empty state for any of them, and an empty "Further out" or
# "Leads" heading is a sentence about nothing.
OPTIONAL_SECTIONS = {
    "schedule": "section-schedule",
    "changes": "section-changes",
    "standing": "section-standing",
    "further": "section-further",
    "leads": "section-leads",
    "opportunities": "section-opportunities",
    "coverage": "section-coverage",
}

SECTION_IDS = {
    "today": "section-today",
    "campus": "section-campus",
    "attention": "section-attention",
}


COURSE_CLASSES = {"engl", "econ", "math", "phil", "cmsc", "advising", "umd",
                  "personal", "none"}
COURSE_LABELS = {"ENGL001", "ECON001", "MATH001", "PHIL001", "CMSC001",
                 "CS-Advising", "UMD", "Personal", "Campus", "System", "Lead"}

# §2.1's table read as a constraint, not two independent lists: a row with
# class `engl` and label `UMD` passed every check before this existed.
COURSE_PAIRS = {
    "engl": {"ENGL001"},
    "econ": {"ECON001"},
    "math": {"MATH001"},
    "phil": {"PHIL001"},
    "cmsc": {"CMSC001"},
    "advising": {"CS-Advising"},
    "umd": {"UMD"},
    "personal": {"Personal"},
    # `Lead` is §14's second regime. The class stays `none`; the regime is a
    # stored item field, not a CSS class, so nothing about the label tells the
    # renderer which section it belongs in — check 24 does that.
    "none": {"Campus", "System", "Lead"},
}

# Generic course-tag colour slots for a course the fixed list above does
# not know yet (a new semester): orchestrator assigns `c1`..`c6`.
PALETTE_CLASSES = ("c1", "c2", "c3", "c4", "c5", "c6")


def course_sets(courses=None):
    """(labels, classes, class->labels pairs): the fixed system set plus the
    account's own courses (label -> class) when the spec supplies them."""
    labels = set(COURSE_LABELS)
    classes = set(COURSE_CLASSES) | set(PALETTE_CLASSES)
    pairs = {k: set(v) for k, v in COURSE_PAIRS.items()}
    for label, cls in (courses or {}).items():
        cls = cls or "none"
        labels.add(label)
        classes.add(cls)
        pairs.setdefault(cls, set()).add(label)
    return labels, classes, pairs


# §6.4: scheme https, host instructure.com or a subdomain, and a path shaped
# like a real Canvas object.
CANVAS_PATH_RE = re.compile(
    r"^/courses/[^/]+/(assignments|discussion_topics|quizzes|announcements)/.+")

# §2 / 7f caps, enforced rather than merely stated.
LIMITS = {"TLDR": 200, "DETAIL": 160, "EXPAND_CONTEXT": 3000}

_SCRIPT_RE = re.compile(r"<script>.*?</script>", re.S)
_STYLE_RE = re.compile(r"<style>.*?</style>", re.S)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_BLANK_RUN_RE = re.compile(r"\n(?:[ \t]*\n){2,}")

# A row may carry extra classes — `is-lead` does (§14.1) — so every pattern
# that finds rows must allow them. Hardcoding `class="row"` with its closing
# quote made _rows() and checks 5 and 6 blind to lead rows, which disabled the
# regime partition without failing anything.
# `class="row"` exactly, or `class="row ` plus more classes. NOT a bare
# prefix: `row[^"]*` also matches row-title, row-actions, row-menu and
# row-expand, which splits the document inside every row.
_ROW_OPEN = r'<div class="row(?:"|\s[^"]*")'
_ROW_ID = _ROW_OPEN + r' data-item-id="([^"]*)"' 


# ---------------------------------------------------------------------------
# reading the template
# ---------------------------------------------------------------------------

def normalize_blanks(text):
    """Make whitespace-only lines genuinely empty.

    Load-bearing. Every file in this project arrives with its blank lines
    holding a single space, so `\\n\\n` occurs ZERO times in the template and
    `block()`'s BEGIN/END match — which needs a real blank line either side of
    the markup — fails for all seven row templates. The symptom is not a crash
    but a briefing where every section shows its empty-note on a day with real
    work, and it passes all of the checks below. Normalize on read, once, and
    the whole class of failure goes away.
    """
    return "\n".join("" if line.strip() == "" else line
                     for line in text.replace("\r\n", "\n").split("\n"))


def read_template(src):
    """Accept a path or the template text itself; return normalized text.

    Taking text as well as a path means the caller can read the file once and
    hand the SAME string to block(), render() and validate(), so check 10
    cannot fail on a difference in how two reads were normalized.
    """
    if "\n" in src or "<" in src:
        return normalize_blanks(src)
    with open(src, encoding="utf-8") as fh:
        return normalize_blanks(fh.read())


# ---------------------------------------------------------------------------
# splice
# ---------------------------------------------------------------------------

def block(template, name):
    """Pull one BEGIN/END row template out of the file's comments."""
    t = normalize_blanks(template)
    m = re.search(r"BEGIN " + re.escape(name) + r" TEMPLATE.*?\n\n(.*?)\n\n\s*END "
                  + re.escape(name), t, re.S)
    if not m:
        raise KeyError("row template not found: " + name)
    return m.group(1)


def esc(value):
    """Escape one interpolated value for text OR attribute position.

    Beyond the five characters §10 names, two more are neutralized:

    * newline/CR/tab become numeric references. `{{EXPAND_CONTEXT}}` is a
      multi-line block that lands inside `data-context="…"`, and a raw newline
      there put attribute content in reach of the whitespace tidy at the end of
      render(), which silently dropped a line from the AI actions' only
      grounding. `&#10;` is decoded back to a newline by the HTML parser, so
      `.textContent` and check 19 both see the original string.
    * braces become numeric references, so no interpolated value can ever
      contain a `{{PLACEHOLDER}}` sequence. Item text comes from email bodies
      (§1.5), and an item whose detail read `{{HEAVY_DAY_WARNING}}` used to
      either inject that banner's raw markup into the row or, when no banner
      was due, delete the row's whole `.row-detail` line — with a clean gate.

    Both render identically to the character they replace.
    """
    out = _html.escape(str(value), quote=True)
    for ch, ref in (("\n", "&#10;"), ("\r", "&#13;"), ("\t", "&#9;"),
                    ("{", "&#123;"), ("}", "&#125;")):
        out = out.replace(ch, ref)
    return out


# The header is `<span>{{WEEKDAY}}</span><span>, {{MONTH_DAY}}</span>`, so the
# comma is IN THE TEMPLATE. A run that helpfully passes "Wednesday," produces
# "Wednesday, , September 9" — shipped 2026-09-09. Neither placeholder had a
# format defined in prompt 7f, so nothing was violated; the template's own
# punctuation was simply invisible from the value side.
_EDGE_PUNCT = {
    "WEEKDAY": ("right", ",;:\u2014- "),
    "MONTH_DAY": ("left", ",;:\u2014- "),
}


def normalize_edges(values):
    """Strip punctuation a placeholder's own template context already supplies.

    Applied inside `fill()`, so it cannot be forgotten by a caller. Only the
    two header fields are listed: this is a fix for a specific adjacency, not a
    licence to silently rewrite arbitrary values.
    """
    out = dict(values)
    for key, (side, chars) in _EDGE_PUNCT.items():
        v = out.get(key)
        if not isinstance(v, str):
            continue
        out[key] = v.rstrip(chars) if side == "right" else v.lstrip(chars)
    return out

def fill(tpl, values, drop_if_empty=()):
    values = normalize_edges(values)
    out = tpl
    for key in drop_if_empty:
        if not values.get(key):
            out = re.sub(r"^[^\n]*\{\{" + re.escape(key) + r"\}\}[^\n]*\n?", "",
                         out, flags=re.M)
    for k, v in values.items():
        out = out.replace("{{" + k + "}}", esc(v))
    return out


def alert_heavy(date_label, titles):
    """Build {{HEAVY_DAY_WARNING}} (prompt 7b).

    The two alert placeholders are substituted as raw HTML, so anything the
    pipeline concatenates into them bypasses escaping — and both of them carry
    item titles, which come from email subjects. Building them here is the only
    way the escaping is guaranteed.
    """
    items = ", ".join(esc(t) for t in titles)
    return ('<div class="alert-heavy">\u26a0\ufe0f Heads up: %s is a heavy day '
            '\u2014 %s all due.</div>' % (esc(date_label), items))


def alert_tight(count):
    """Build {{TIGHT_TRANSITION_ALERT}} (prompt step 4)."""
    return ('<div class="alert-tight">\u26a0\ufe0f You have %d tight '
            'back-to-back transitions today.</div>' % int(count))


# §5.3b. Asserted on the page by check 27, not merely requested of the run.
CAMPUS_MAX = 3

# STEP 5d (2026-09-19). Asserted on the page by check 31, mirroring how
# CAMPUS_MAX/check 27 work: the real per-run value (orchestrator.py's own
# PORTFOLIO_NOTES_MAX, threaded through spec["caps"]["portfolio_notes"])
# wins when supplied; this is only the fallback.
PORTFOLIO_NOTES_MAX = 3


def change_strip(counts, comparable=True):
    """§5.0 — `2 new · 1 date moved · 3 resolved since yesterday`.

    Built here rather than in the run for the same reason as the two alert
    banners: it is one line assembled from four numbers, and assembling it by
    hand at the end of a 25-minute run is how `duration_seconds` came back
    null. It carries no item titles, so it is escaped as ordinary text.

    `comparable=False` returns "" — on a first run, or after a gap where
    `last_completed_date` is more than one weekday behind (STEP 0.4b), there is
    no yesterday to compare against and "nothing changed" would be a claim
    about a run that never happened. render() then deletes the whole div.
    """
    if not comparable:
        return ""
    counts = counts or {}
    order = (("new", "new"), ("moved", "date moved"),
             ("resolved", "resolved"), ("overdue", "newly overdue"))
    parts = ["%d %s" % (int(counts.get(k) or 0), label)
             for k, label in order if int(counts.get(k) or 0)]
    if not parts:
        return "Nothing changed since yesterday."
    return " \u00b7 ".join(parts) + " since yesterday"


def _indent(s, pad="    "):
    return "\n".join(pad + line if line.strip() else line
                     for line in s.split("\n"))


def _tidy_blank_lines(t):
    """Collapse runs of blank lines, outside the locked blocks only.

    The tidy used to run over the whole document, which is how it reached
    attribute values. It is masked here as well as scoped, so a locked script
    that ever grows a blank-line run cannot be reformatted into a check-10
    failure.
    """
    vault = []

    def stash(m):
        vault.append(m.group(0))
        return "\x00%d\x00" % (len(vault) - 1)

    t = _STYLE_RE.sub(stash, t)
    t = _SCRIPT_RE.sub(stash, t)
    t = _BLANK_RUN_RE.sub("\n\n", t)
    return re.sub(r"\x00(\d+)\x00", lambda m: vault[int(m.group(1))], t)

def _mark_empty(out, section_id):
    """Add `is-empty` to one section div (§5.2's one-line form)."""
    return re.sub(r'<div class="section" id="%s"' % re.escape(section_id),
                  '<div class="section is-empty" id="%s"' % section_id, out, count=1)


_ATTN_MORE_RE = re.compile(r'\n\s*<details class="more attn-more">.*?</details>', re.S)


def drop_attn_more(out):
    """Remove Needs-your-attention's overflow `<details>` as a unit. Deleting
    only its summary line would leave an empty disclosure on the page."""
    return _ATTN_MORE_RE.sub("", out)


_FURTHER_MORE_RE = re.compile(r'\n\s*<details class="more further-more">.*?</details>', re.S)


def drop_further_more(out):
    """Remove Further out's FYI-event overflow `<details>` as a unit (§16c),
    same reasoning as drop_attn_more() above."""
    return _FURTHER_MORE_RE.sub("", out)


def drop_section(out, section_id):
    """Delete one optional section, heading and all. Keyed on the template's
    `/section-id` end comment, so it must run BEFORE comments are stripped."""
    return re.sub(r'[ \t]*<div class="section" id="%s".*?</div><!-- /%s -->\n?'
                  % (re.escape(section_id), re.escape(section_id)), "", out,
                  flags=re.S)



def render(template=None, ctx=None, template_path=None):
    """ctx keys: scalars (TLDR, WEEKDAY, ...) plus `sections` -> {name: html},
    names as in MARKERS. build() is the normal caller; it computes every
    section's markup from the spec.

    `template` is a path or the template text (see read_template).
    `template_path=` is accepted as an alias for older callers.
    """
    t = read_template(template if template is not None else template_path)
    ctx = dict(ctx or {})
    sections = ctx.get("sections", {}) or {}

    # Optional scalar lines go FIRST, before any row markup is in the
    # document, so a line deletion can only remove the line it is aimed at.
    if not ctx.get("WEATHER_LINE"):
        t = re.sub(r"^[^\n]*\{\{WEATHER_LINE\}\}[^\n]*\n", "", t, flags=re.M)
    # No comparison available (first run, or a gap — STEP 0.4b) means no strip
    # at all: an empty `.changes` line reads as a failed computation.
    if not ctx.get("CHANGE_STRIP"):
        t = re.sub(r'[ \t]*<div class="changes">\s*\{\{CHANGE_STRIP\}\}\s*'
                   r'</div>\n', "", t)
    if not ctx.get("ATTENTION_MORE_LABEL") or not sections.get(
            "attention_more", "").strip():
        t = drop_attn_more(t)
    if not ctx.get("FURTHER_MORE_LABEL") or not sections.get(
            "further_more", "").strip():
        t = drop_further_more(t)
    for name, sec_id in OPTIONAL_SECTIONS.items():
        if not sections.get(name, "").strip():
            t = drop_section(t, sec_id)

    for name, marker in MARKERS.items():
        if marker not in t:
            continue
        body = sections.get(name, "")
        if not body.strip():
            if name in EMPTY_NOTES:
                # The note replaces the section's container, not just its
                # contents: an empty `.cards` grid or `.panel` would still
                # draw its border around nothing.
                note = '<div class="empty-note">%s</div>' % _html.escape(EMPTY_NOTES[name])
                t = re.sub(r'<div class="[^"]*">\s*' + re.escape(marker) + r'\s*</div>',
                           note, t, count=1)
                t = _mark_empty(t, SECTION_IDS[name])
                continue
            t = t.replace(marker, "")
            continue
        t = t.replace(marker, _indent(body.strip(), "      ").strip())

    t = _COMMENT_RE.sub("", t)

    # render() substitutes scalars itself rather than delegating to fill(), so
    # the edge-punctuation normalisation is applied here as well.
    ctx = normalize_edges(ctx)
    for k, v in ctx.items():
        if k == "sections" or not isinstance(v, (str, int, float)):
            continue
        t = t.replace("{{" + k + "}}", esc(v))

    return _tidy_blank_lines(t)


# ---------------------------------------------------------------------------
# validation gate
# ---------------------------------------------------------------------------

# The canonical section headings, in page order. Each entry is a tuple of the
# spellings that slot accepts. A heading outside this list, or out of order,
# fails check 4; the MANDATORY ones must be present.
CANONICAL_LABELS = (
    ("Today — Due",),
    ("Today — on your calendar",),
    ("Needs your attention",),
    ("What changed",),
    ("This week",),
    ("Opportunities",),
    ("Today — around campus", "This week — around campus"),
    ("Where you stand",),
    ("Further out",),
    ("Leads — unconfirmed, worth one look",),
    ("What I can't see",),
)
MANDATORY_LABELS = {"Today — Due", "Today — around campus",
                    "This week", "Needs your attention"}

# data-kind -> the §14.1 regime a row of that kind must carry.
KIND_REGIME = {
    "assignment": "confirmed", "assessment": "confirmed",
    "coming-up": "confirmed", "attention": "confirmed",
    "campus": "confirmed", "opportunity": "confirmed", "lead": "lead",
}

_SECTION_END = (r'(?=<div class="section(?: is-empty)?" id=|<div class="page-actions">'
                r'|<div class="footer">)')


def _section(out, section):
    m = re.search(r'id="%s"[^>]*>(.*?)%s' % (re.escape(section), _SECTION_END), out, re.S)
    return m.group(1) if m else ""


def _row_extent(seg, start):
    """End index of the div opened at `start`, by depth count."""
    depth = 0
    for m in re.finditer(r"<div\b|</div>", seg[start:]):
        depth += 1 if m.group(0) == "<div" else -1
        if depth == 0:
            return start + m.end()
    return len(seg)


def _rows_in(seg):
    """Yield (item_id, row_html) for every .row in a segment. Each row is cut
    at its OWN closing div, so a row never absorbs the markup that follows it
    (the next day's pill, the next section's heading)."""
    if not seg:
        return
    for m in re.finditer(_ROW_OPEN, seg):
        row = seg[m.start():_row_extent(seg, m.start())]
        rid = re.match(_ROW_OPEN + r' data-item-id="([^"]*)"', row)
        yield (rid.group(1) if rid else None), row


def _rows(out, section):
    """Yield (item_id, row_html) for every .row in one section."""
    yield from _rows_in(_section(out, section))


def _page(out):
    """The page body the rows live in: everything before the dialogs."""
    cut = out.find('<dialog')
    return out if cut < 0 else out[:cut]


def _all_rows(out):
    yield from _rows_in(_page(out))


def _kind(row):
    m = re.match(r'<div class="row[^>]*?data-kind="([a-z-]+)"', row)
    return m.group(1) if m else None


def _canvas_host_ok(host):
    """§6.4 check 2, as written there rather than as a suffix test.

    `host.endswith("instructure.com")` accepted `evil-instructure.com`, which
    is precisely the spoofed-Canvas-email case §6.4 exists to stop.
    """
    if "@" in host or not host:
        return False
    host = host.split(":")[0].lower().rstrip(".")
    return host == "instructure.com" or host.endswith(".instructure.com")


def validate(out, template_path=None, items=None, caps=None, courses=None):
    """Return a list of problems. Empty list means publishable.

    `items` is an optional {item_id: item_dict} map of what the run rendered;
    supplying it turns on the data halves of checks 16, 18, 20 and 24.
    `caps` is an optional {"campus": N} override for the campus cap (§12.2).
    """
    p = []
    items_given = items is not None
    items = items or {}
    caps = caps or {}
    rows = list(_all_rows(out))

    # 1
    if re.search(r"\{\{[A-Z_]+\}\}", out):
        p.append("1: unfilled placeholder remains: %s"
                 % re.findall(r"\{\{[A-Z_]+\}\}", out)[:3])
    body = _SCRIPT_RE.sub("", out)
    if "{{" in body or "}}" in body:
        p.append("1: stray brace pair outside the locked scripts")
    # 2
    if "<!--" in out:
        p.append("2: HTML comment survived")
    visible = _STYLE_RE.sub("", _SCRIPT_RE.sub("", out))
    for bad in ("undefined", "null", "NaN", "None"):
        if re.search(r">\s*" + bad + r"\s*<", visible) \
                or re.search(r'="' + bad + r'"', visible) \
                or re.search(r":\s*" + bad + r"(?![A-Za-z0-9_-])", visible):
            p.append("2: literal %r visible in output" % bad)
    # 3
    for tag in ("<!DOCTYPE", "<html", "<head", "<body"):
        if tag.lower() in out.lower():
            p.append("3: document wrapper tag %s present" % tag)
    # 4 — headings: every one canonical, in page order, mandatory ones present.
    labels = []
    for raw in re.findall(r'<h2 class="section-label">(.*?)</h2>', out, re.S):
        raw = re.sub(r'<span class="m-only">.*?</span>', "", raw)
        labels.append(_html.unescape(re.sub(r"<[^>]+>", "", raw)).strip())
    at = -1
    for lab in labels:
        slot = next((i for i, names in enumerate(CANONICAL_LABELS)
                     if lab in names), None)
        if slot is None:
            p.append("4: unexpected section label %r" % lab)
        elif slot <= at:
            p.append("4: section %r out of order in %r" % (lab, labels))
        else:
            at = slot
    flat = set(labels)
    for want in MANDATORY_LABELS:
        slot = next(names for names in CANONICAL_LABELS if want in names)
        if not flat.intersection(slot):
            p.append("4: mandatory section %r missing in %r" % (want, labels))
    # 4b/4c — the preferences form and the detail dialog stay reachable.
    for frag in ('id="sheet-prefs"', 'data-action="prefs-open"',
                 'data-role="prefs-body"', 'data-action="prefs-save"'):
        if frag not in out:
            p.append("4b: preferences form unreachable — %s missing" % frag)
    if 'id="sheet-detail"' not in out or 'data-role="detail-body"' not in out:
        p.append("4c: detail dialog missing")
    # 5 — ids unique and non-empty; every row stamped with its kind.
    all_ids = [rid for rid, _ in rows]
    if len(all_ids) != len(set(all_ids)):
        p.append("5: duplicate data-item-id across rows")
    for rid, row in rows:
        if rid is None or not rid.strip():
            p.append("5: row with an empty data-item-id")
        if _kind(row) not in KIND_REGIME:
            p.append("5: row %s carries no known data-kind" % rid)
    # 6
    for rid, row in rows:
        for bid in re.findall(
                r'data-action="mark-(?:done|resolved)" data-item-id="([^"]+)"', row):
            if bid != rid:
                p.append("6: button id %s != row id %s" % (bid, rid))
    # 7
    for href in re.findall(r'class="menu-item canvas-link" role="menuitem" href="([^"]+)"', out):
        h = _html.unescape(href)
        u = urlparse(h)
        if u.scheme != "https" or not _canvas_host_ok(u.netloc):
            p.append("7: bad canvas link host/scheme %s" % h)
        elif not CANVAS_PATH_RE.match(u.path):
            p.append("7: canvas link path is not a Canvas object: %s" % h)
    # 8
    if re.search(r'<button[^>]*add-reminder', out):
        p.append("8: Add reminder rendered as a button")
    for href in re.findall(r'class="menu-item add-reminder" role="menuitem" href="([^"]+)"', out):
        h = _html.unescape(href)
        if not h.startswith("https://calendar.google.com/calendar/render?action=TEMPLATE"):
            p.append("8: add-reminder href not a Google prefill URL")
        qs = parse_qs(urlparse(h).query)
        dates = (qs.get("dates") or [""])[0]
        if not re.match(r"^\d{8}(T\d{6}Z)?/\d{8}(T\d{6}Z)?$", dates):
            p.append("8: add-reminder dates malformed: %r" % dates)
        else:
            a, b = dates.split("/")
            if ("T" in a) != ("T" in b):
                p.append("8: add-reminder dates halves differ in format")
    # 9
    for rid, row in rows:
        if _kind(row) == "assessment" and 'data-action="mark-done"' in row:
            p.append("9: mark-done on assessment row %s" % rid)
        if rid and rid.startswith("group-") and 'data-action="mark-done"' in row:
            p.append("9: mark-done on grouped row %s" % rid)
    if "window.open(" in _SCRIPT_RE.sub("", out):
        p.append("9: window.open outside the locked scripts")
    # 10
    if template_path:
        tsrc = _COMMENT_RE.sub("", read_template(template_path))
        for label, rx in (("style", _STYLE_RE), ("script", _SCRIPT_RE)):
            want, got = rx.findall(tsrc), rx.findall(out)
            if len(want) != len(got):
                p.append("10: %s block count %d != template %d"
                         % (label, len(got), len(want)))
            elif want != got:
                for i, (a, b) in enumerate(zip(want, got)):
                    if a != b:
                        p.append("10: %s block %d differs from template" % (label, i + 1))
        if len(_SCRIPT_RE.findall(out)) != 4:
            p.append("10: expected exactly 4 script blocks")
    # 11
    for rid, row in rows:
        if _kind(row) in ("assignment", "assessment", "coming-up", "campus") \
                and row.count('class="row-meta"') != 1:
            p.append("11: row %s has %d .row-meta"
                     % (rid, row.count('class="row-meta"')))
    # 13 -- the closed course set is the account's own course list (spec
    # "courses", label -> class) on top of the fixed system labels.
    labels_ok, classes_ok, pairs = course_sets(courses)
    for cls, txt in re.findall(r'<span class="course-tag ([a-z0-9]+)">([^<]*)</span>', out):
        if cls not in classes_ok:
            p.append("13: bad course class %r" % cls)
        if txt not in labels_ok:
            p.append("13: bad course label %r" % txt)
        if cls in pairs and txt in labels_ok \
                and txt not in pairs[cls]:
            p.append("13: class %r paired with label %r — §2.1 pairs it with %s"
                     % (cls, txt, sorted(pairs[cls])))
    # 14
    for rid, row in rows:
        grouped = bool(rid and rid.startswith("group-"))
        if grouped:
            if "data-context=" in row or 'data-action="row-menu"' in row or "row-expand" in row:
                p.append("14: grouped row carries a menu/context/expand")
            continue
        if not re.search(r'data-context="[^"]*[^"\s][^"]*"', row):
            p.append("14: row %s missing data-context" % rid)
        if row.count('data-action="row-menu"') != 1:
            p.append("14: row %s lacks exactly one row-menu button" % rid)
        if row.count('class="row-menu"') != 1:
            p.append("14: row %s lacks exactly one .row-menu" % rid)
        if row.count('class="row-expand"') != 1:
            p.append("14: row %s lacks exactly one .row-expand" % rid)
        else:
            eid = re.search(r'<div class="row-expand" data-item-id="([^"]*)"', row)
            if not eid or eid.group(1) != rid:
                p.append("14: .row-expand id %r != row id %r"
                         % (eid.group(1) if eid else None, rid))
    # 15
    for btn in re.findall(r'<button[^>]*data-action="row-menu"[^>]*>', out):
        if 'aria-label="' not in btn or 'aria-expanded="false"' not in btn:
            p.append("15: row-menu button missing aria-label/aria-expanded")
    # 16
    for menu in re.findall(r'<div class="row-menu" role="menu" hidden>(.*?)</div>\s*</div>', out, re.S):
        if not re.sub(r"<[^>]+>", "", menu).strip():
            p.append("16: empty .row-menu")
        if re.search(r"<(?:button|a)\b[^>]*\bdisabled\b", menu):
            p.append("16: disabled menu item")
    markup = _SCRIPT_RE.sub("", out)
    standing_seg = _section(out, "section-standing")
    for action, kind in (("mark-done", "assignment"), ("mark-resolved", "attention")):
        total = markup.count('data-action="%s"' % action)
        inside = sum(row.count('data-action="%s"' % action)
                     for _, row in rows if _kind(row) == kind)
        # §16b (2026-09-18): Where you stand may echo the SAME control for an
        # item that is ALSO a canonical row elsewhere on the page — a
        # .tbl-row has no data-kind of its own (it is invisible to
        # _all_rows() by construction), so it can't be counted the way
        # `inside` counts real rows. Counted here instead. When `items` was
        # actually supplied, each echo is cross-checked against the real
        # item's kind rather than trusted on sight: one whose id validate()
        # doesn't recognise, or whose real kind doesn't match the action, is
        # not waved through. Without `items` (the same "only checkable with
        # items" degrade every data-dependent check below already applies)
        # a standing echo is accepted structurally, same as any other check
        # this page has no data to verify.
        echoed = 0
        for m in re.finditer(r'data-action="%s" data-item-id="([^"]+)"' % action,
                              standing_seg or ""):
            if not items_given or (items.get(m.group(1)) or {}).get("kind") == kind:
                echoed += 1
        if total != inside + echoed:
            p.append("16: %d %s control(s) outside %s rows"
                     % (total - inside - echoed, action, kind))
    # 17
    n = len(re.findall(r'data-action="ai-page"', markup))
    if n != 1:
        p.append("17: %d page-level AI buttons (want 1)" % n)
    # 18
    for rid, row in rows:
        if _kind(row) in ("coming-up", "campus") and 'data-action="ai"' in row:
            p.append("18: AI action offered on %s row %s" % (_kind(row), rid))
        if not rid or rid.startswith("group-"):
            if rid and 'data-action="ai"' in row:
                p.append("18: AI action on grouped row %s" % rid)
            continue
        rendered = set(re.findall(r'data-action="ai" data-task="([a-z]+)"', row))
        if rid in items:
            earned = set(items[rid].get("ai_actions") or [])
            for extra in sorted(rendered - earned):
                p.append("18: %s renders data-task=%r, not in its ai_actions"
                         % (rid, extra))
            for missing in sorted(earned - rendered):
                p.append("18: %s earned %r but no button was rendered"
                         % (rid, missing))
    # 19
    for rid, row in rows:
        hm = re.search(r'class="menu-item add-reminder" role="menuitem" href="([^"]+)"', row)
        if not hm:
            continue
        h = _html.unescape(hm.group(1))
        qs = parse_qs(urlparse(h).query)
        details = (qs.get("details") or [""])[0]
        if not details.strip():
            p.append("19: add-reminder has an empty details parameter")
        cm = re.search(r'data-context="([^"]*)"', row)
        ctxt = _html.unescape(cm.group(1)) if cm else ""
        carried = details + " " + (qs.get("location") or [""])[0]
        for u in set(re.findall(r"https://[^\s<>\"']+", ctxt)):
            if u.rstrip(".,;") not in carried:
                p.append("19: %s captured %s but the reminder does not carry it"
                         % (rid, u))
    for url in re.findall(r'(?:href|src)="([^"]*)"', markup):
        u = _html.unescape(url).strip()
        scheme = (urlparse(u).scheme or "").lower()
        if scheme and scheme != "https":
            p.append("19: non-https URL in page: %s" % u)
    # 20
    for span in re.findall(r'<span class="row-new">([^<]*)</span>', out):
        if span != "New":
            p.append("20: .row-new span contains %r, expected 'New'" % span)
    for rid, row in rows:
        chipped = 'class="row-new"' in row
        if chipped and not re.search(r'<div class="row-meta"><span class="row-new">', row):
            p.append("20: .row-new outside a .row-meta on %s" % rid)
        if rid and rid.startswith("group-") and chipped:
            p.append("20: .row-new on grouped row %s" % rid)
        if not rid or rid not in items:
            continue
        first = (items[rid].get("times_surfaced") or 0) == 0
        if chipped and not first:
            p.append("20: %s carries the New chip but times_surfaced=%r"
                     % (rid, items[rid].get("times_surfaced")))
        if first and not chipped and not rid.startswith("group-") \
                and 'class="row-meta"' in row:
            p.append("20: %s is new but carries no New chip" % rid)
    # 21
    tm = re.search(r"<title>([^<]*)</title>", out)
    if not tm or not tm.group(1).strip():
        p.append("21: empty <title>")
    elif not tm.group(1).startswith("UMD Daily Briefing"):
        p.append("21: <title> no longer starts with 'UMD Daily Briefing'")
    elif not re.search(r"\d{4}", tm.group(1)):
        p.append("21: <title> carries no date")
    # 22
    m = re.search(r'<dialog[^>]*id="sheet-prefs".*?</dialog>', out, re.S)
    if not m:
        p.append("22: preferences dialog missing")
    else:
        seg = m.group(0)
        for need in ('data-action="prefs-save"', 'data-role="prefs-body"',
                     'data-role="prefs-status"'):
            if need not in seg:
                p.append("22: preferences shell missing %s" % need)
        if 'data-action="prefs-open"' not in out.replace(seg, ""):
            p.append("22: nothing opens the preferences dialog")
        if "{{" in seg:
            p.append("22: pipeline wrote a placeholder into the preferences shell")
    # 24 — THE REGIME PARTITION (§14.1), per row now rather than per section.
    lead_ids = {rid for rid, _ in _rows(out, "section-leads")}
    for rid, row in rows:
        kind = _kind(row)
        is_lead_markup = 'class="row is-lead"' in row or 'class="lead-conf"' in row
        if kind == "lead":
            if rid not in lead_ids:
                p.append("24: lead row %s renders outside Leads" % rid)
            if "is-lead" not in row:
                p.append("24: lead row %s is missing class is-lead" % rid)
            for need, why in (('class="lead-conf"', "confidence tier"),
                              ('class="lead-basis"', "basis lines"),
                              ('data-action="lead-confirmed"', "a confirm control"),
                              ('data-action="lead-killed"', "a kill control")):
                if need not in row:
                    p.append("24: lead row %s has no %s (§14.3)" % (rid, why))
            if not re.search(r'<li>[^<]', row):
                p.append("24: lead row %s renders an empty basis list (§14.1)" % rid)
        else:
            if rid in lead_ids:
                p.append("24: %s row %s renders in Leads" % (kind, rid))
            if is_lead_markup:
                p.append("24: lead markup on %s row %s" % (kind, rid))
        if not rid or rid.startswith("group-") or kind not in KIND_REGIME:
            continue
        got = (items.get(rid) or {}).get("regime")
        if got is None:
            if rid in items:
                p.append("24: %s has no regime — it must be stored, never "
                         "inferred (§3.3)" % rid)
            continue
        if got != KIND_REGIME[kind]:
            p.append("24: %s is regime %r but renders as a %s row"
                     % (rid, got, kind))
    # 23 — the length caps in §2 and 7f.
    tl_text = re.search(r'<p class="tldr">(.*?)</p>', out, re.S)
    if tl_text and len(_html.unescape(tl_text.group(1)).strip()) > LIMITS["TLDR"]:
        p.append("23: {{TLDR}} exceeds %d characters" % LIMITS["TLDR"])
    for rid, row in rows:
        d = re.search(r'<div class="row-detail">(.*?)</div>', row, re.S)
        if d and len(_html.unescape(d.group(1)).strip()) > LIMITS["DETAIL"]:
            p.append("23: %s .row-detail exceeds %d characters"
                     % (rid, LIMITS["DETAIL"]))
        c = re.search(r'data-context="([^"]*)"', row)
        if c and len(_html.unescape(c.group(1))) > LIMITS["EXPAND_CONTEXT"]:
            p.append("23: %s data-context exceeds %d characters"
                     % (rid, LIMITS["EXPAND_CONTEXT"]))
    # 26
    cs = re.search(r'<div class="changes">(.*?)</div>', out, re.S)
    if cs is not None and not _html.unescape(cs.group(1)).strip():
        p.append("26: .changes rendered empty — pass no CHANGE_STRIP instead")
    # 28 — §10's one-fact-one-place: due flag and days-out are one slot.
    for rid, row in rows:
        m = re.search(r'<div class="row-meta">(.*?)</div>', row, re.S)
        if not m:
            continue
        inner = m.group(1)
        flag = re.search(r'<span class="due-flag">(.*?)</span>', inner)
        rest = _html.unescape(re.sub(r'<span[^>]*>.*?</span>', "", inner)).strip()
        if flag and rest:
            p.append("28: %s carries both a due flag and a days-out "
                     "(%r) — they are one slot (§10)" % (rid, rest[:30]))
        if flag is not None and not flag.group(1).strip():
            p.append("28: %s renders an empty due-flag span" % rid)
    # 27 — §5.3b: the campus cap, and campus rows never among obligations.
    campus_cap = caps.get("campus", CAMPUS_MAX)
    campus_rows = [(rid, row) for rid, row in rows if _kind(row) == "campus"]
    if len(campus_rows) > campus_cap:
        p.append("27: around campus carries %d rows, cap is %d"
                 % (len(campus_rows), campus_cap))
    campus_ids = {rid for rid, _ in _rows(out, "section-campus")}
    for rid, row in campus_rows:
        tag = re.search(r'<span class="course-tag ([^"]*)">([^<]*)</span>', row)
        if tag and tag.group(2) != "Campus":
            p.append("27: campus row %s is labelled %r, not 'Campus'"
                     % (rid, tag.group(2)))
        if rid not in campus_ids:
            p.append("27: campus row %s renders outside around campus" % rid)
    for rid, row in rows:
        if _kind(row) in ("coming-up", "assignment", "assessment") \
                and ">Campus</span>" in row:
            p.append("27: campus-labelled row %s renders as a %s row — campus "
                     "events belong around campus (§5.3b)" % (rid, _kind(row)))
    # 29 — §5.1c: a System row never renders inline in Needs your attention.
    attn_seg = _section(out, "section-attention")
    attn_inline = _ATTN_MORE_RE.sub("", "\n" + attn_seg) if attn_seg else ""
    for rid, row in _rows_in(attn_inline):
        if ">System</span>" in row:
            p.append("29: System row %s renders inline in Needs your "
                     "attention — it belongs behind the attn-more "
                     "disclosure (§5.1c)" % rid)
    # 30 — the date placement (2026-09-17). A row in Today must be due today
    # (or earlier); a row in This week must fall inside the five-day window.
    # Only checkable with `items`, which carries each row's date.
    for sec, rule in (("section-today", "today"), ("section-week", "week")):
        for rid, row in _rows(out, sec):
            info = items.get(rid) or {}
            d, today = _as_date(info.get("date")), _as_date(info.get("today"))
            if d is None or today is None:
                continue
            if rule == "today" and d > today:
                p.append("30: %s renders under Today but is dated %s" % (rid, d))
            if rule == "week" and not (today < d <= today + _dt.timedelta(days=WEEK_DAYS)):
                p.append("30: %s renders under This week but is dated %s" % (rid, d))
    # 31 — STEP 5d (2026-09-19): Where you stand's trend/portfolio notes
    # (`.tbl-row.tbl-note`) are asserted bounded, not merely trusted to stay
    # short — PORTFOLIO_NOTES_MAX in orchestrator.py is the producer-side
    # cap this mirrors; a page with more than that read like a second
    # gradebook, which the pasted task's own "non-repetitive" rule forbids.
    portfolio_cap = caps.get("portfolio_notes", PORTFOLIO_NOTES_MAX)
    note_count = len(re.findall(r'<div class="tbl-row tbl-note">', out))
    if note_count > portfolio_cap:
        p.append("31: Where you stand carries %d portfolio/trend notes, "
                 "cap is %d" % (note_count, portfolio_cap))
    return p


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------

SECTION_BLOCK = {
    "assignments": "ASSIGNMENT ROW",
    "assessments": "ASSESSMENT ROW",
    "coming_up": "COMING-UP ROW",
    # A demoted row is an ordinary row, not a lesser one: same block, so it
    # keeps its menu, its detail and its Add-reminder link (§5.3).
    "coming_up_more": "COMING-UP ROW",
    "attention": "ATTENTION ROW",
    # A demoted attention row is an ordinary attention row (§5.1c) — same
    # reasoning as coming_up_more reusing COMING-UP ROW above.
    "attention_more": "ATTENTION ROW",
    # A campus row is an ordinary Coming-up row: same markup, same menu, same
    # Add-reminder link. Only its section and its cap differ (§5.3b).
    "campus": "COMING-UP ROW",
    "leads": "LEAD ROW",
    "opportunities": "OPPORTUNITY ROW",
}

# Menu items that are conditional, and the marker identifying their line.
# Everything not listed here is unconditional for its section.
_CONDITIONAL = (
    # `{{DUE_FLAG_TEXT}}` is NOT here: since 2026-09-11 it is a span sharing
    # the .row-meta line, removed by _drop_span above rather than by deleting
    # a line that also carries the days-out and the New chip.
    ("canvas-link", "canvas_url"),
    ("add-reminder", "add_reminder_url"),
    ("{{ACT_BY_LINE}}", "act_by_line"),
    ("lead-source", "source_url"),
    # A second marker for the SAME key: the Assignment/Assessment/Coming-up/
    # Attention templates' own "Open the source" link (added 2026-09-18),
    # keyed off the fallback orchestrator._fallback_source_url() computes
    # only when canvas_url is empty -- never the same row as a.canvas-link.
    ("source-link", "source_url"),
    ("{{OPP_META}}", "opp_meta"),
    ("{{INELIGIBLE_NOTE}}", "ineligible_note"),
    ("{{DOSSIER_HTML}}", "dossier_html"),
    ("<b>To confirm:</b>", "confirm_action"),
    ("<b>Drop it if:</b>", "kill_criteria"),
)
_AI_TASKS = ("steps", "study", "email")

# §15. Which sections hold rows a "Worth showing?" question is meaningful on.
# The template's own CSS has said "only on suggested rows — coursework is not a
# suggestion" since the control was written, while the markup emitted it on
# every Coming-up row: an email-sourced deadline and an advising appointment
# both carried it, and `feedback.family_of()` generalises over recurring
# SUGGESTIONS, so a rating on an obligation had no consumer. Derived from the
# section rather than asked of the caller, because since §5.3b the section IS
# the statement "this was suggested, not owed".
RATEABLE_SECTIONS = ("campus", "opportunities")


def _drop_line(tpl, marker):
    return "\n".join(l for l in tpl.split("\n") if marker not in l)


def _drop_span(tpl, cls):
    """Remove an inline `<span class="cls">…</span>` wherever it appears.

    `.due-flag` and `{{DAYS_OUT_META}}` share one `.row-meta` line now (§10's
    one-fact-one-place), so exactly one of them has to go and neither can be
    removed by deleting a line — that would take the whole meta line with it.
    """
    return re.sub(r'<span class="%s">.*?</span>' % re.escape(cls), "", tpl)


def resolve_meta_slot(tpl, due_flag_text):
    """§10: `span.due-flag` and `{{DAYS_OUT_META}}` are ONE slot in `.row-meta`.

    Public because `build_row()` is not the only thing that assembles a row —
    the regression fixtures build them straight from the template blocks, and a
    rule applied in one of two places is how the two stacked lines survived in
    the first place. Gate check 28 is the backstop.
    """
    if due_flag_text:
        return "\n".join(
            l.replace("{{DAYS_OUT_META}}", "") if 'class="row-meta"' in l else l
            for l in tpl.split("\n"))
    return _drop_span(tpl, "due-flag")


def _drop_block(tpl, marker, closer="</div>"):
    """Delete from the line carrying `marker` through its closing line.

    `_drop_line` cannot remove `div.menu-rate`: it is five lines, and dropping
    only the one with the class on it leaves an orphaned label and two live
    buttons inside the menu. Depth-counted rather than "to the next `</div>`",
    so a nested element inside the block could not end it early.
    """
    out, depth, dropping = [], 0, False
    for line in tpl.split("\n"):
        if not dropping and marker in line:
            dropping = True
            depth = line.count("<div") - line.count(closer)
            if depth <= 0:
                dropping = False
            continue
        if dropping:
            depth += line.count("<div") - line.count(closer)
            if depth <= 0:
                dropping = False
            continue
        out.append(line)
    return "\n".join(out)


BASIS_CLAIM_MAX = 280
# orchestrator._search_lead_item() stores fetched text cut at exactly this
# many characters, so a claim of this length was truncated upstream.
_UPSTREAM_CUT = 300


def _clip_claim(text, limit=BASIS_CLAIM_MAX):
    """A lead's basis claim, ending on a whole word with an ellipsis when it
    was (or has to be) shortened -- never mid-word ("Video Game-Pla")."""
    t = " ".join(str(text or "").split())
    if len(t) <= limit and len(str(text or "")) < _UPSTREAM_CUT:
        return t
    t = t[:limit]
    if " " in t:
        t = t.rsplit(" ", 1)[0]
    return t.rstrip(" ,;:\u2014-") + "\u2026"


def build_row(template, section, item):
    """One rendered row, stamped with its data-kind (see SECTION_KIND) and,
    when known, the date the pipeline first saw it (data-seen) -- what the
    page's "since you last looked" marker compares against."""
    if isinstance(item.get("dossier"), dict) and not item.get("dossier_html"):
        item = dict(item, dossier_html=dossier_html(item["dossier"]))
    return _stamp_kind(_build_row(template, section, item), section,
                       seen=item.get("first_seen"))


_DOSSIER_FACTS = (("deadline", "Deadline"), ("eligibility", "Who can apply"),
                  ("requirements", "You'll need"), ("format", "Format"),
                  ("compensation", "Pay"))


def dossier_html(d):
    """A researched opportunity (service/research.py) as one line of markup.
    Quoted fields were checked against the source page by code and render
    as quotes; the one interpretive line is labelled as Claude's read."""
    if not d:
        return ""
    parts = []
    if d.get("summary"):
        parts.append('<p class="dos-sum">%s</p>' % esc(d["summary"]))
    elif d.get("note"):
        parts.append('<p class="dos-note">Not researched: %s.</p>' % esc(d["note"]))
    if d.get("summary") and d.get("relevant") is False:
        parts.append('<p class="dos-note">The linked page did not describe '
                     'this opportunity; details are from the email.</p>')
    facts = []
    for key, label in _DOSSIER_FACTS:
        v = d.get(key)
        if not v:
            continue
        if isinstance(v, (list, tuple)):
            shown = " \u00b7 ".join("<q>%s</q>" % esc(x) for x in v if x)
        else:
            shown = "<q>%s</q>" % esc(v)
        if shown:
            facts.append("<div><dt>%s</dt><dd>%s</dd></div>" % (esc(label), shown))
    if facts:
        parts.append('<dl class="dos-facts">%s</dl>' % "".join(facts))
    if d.get("why_it_fits"):
        parts.append('<p class="dos-fit"><span class="dos-label">Claude\u2019s '
                     'read</span> %s</p>' % esc(d["why_it_fits"]))
    url = str(d.get("source_url") or "")
    on = _as_date(d.get("researched_on"))
    if d.get("from_email") and d.get("summary"):
        parts.append('<p class="dos-src">Quotes checked against the original '
                     'email%s</p>' % ((" \u00b7 %s" % esc(_dow_md(on))) if on else ""))
    elif url.startswith("https://") and d.get("summary"):
        host = urlparse(url).netloc.replace("www.", "")
        parts.append('<p class="dos-src">Quotes checked against '
                     '<a href="%s" target="_blank" rel="noopener">%s</a>%s</p>'
                     % (esc(url), esc(host),
                        (" \u00b7 %s" % esc(_dow_md(on))) if on else ""))
    return "".join(parts)


def _build_row(template, section, item):
    if item.get("grouped"):
        return fill(block(template, "GROUPED ROW"), {
            "COURSE": item.get("course", ""),
            "PATTERN_SLUG": item.get("pattern_slug", ""),
            "COURSE_CLASS": item.get("course_class", "none"),
            "COURSE_LABEL": item.get("course_label", "UMD"),
            "TITLE": item.get("title", ""),
            "GROUP_DATES": item.get("group_dates", ""),
            "DAYS_OUT_META": item.get("days_out_meta", ""),
        })

    tpl = block(template, SECTION_BLOCK[section])

    # §15's rating control, deleted as a BLOCK. `rateable` lets a demoted
    # campus row keep the thumbs it earned wherever it renders; absent the
    # flag, the section decides.
    if not (section in RATEABLE_SECTIONS or item.get("rateable")):
        tpl = _drop_block(tpl, 'class="menu-rate"')

    # §10 / §5.6: the due flag and the days-out are ONE slot inside .row-meta,
    # and exactly one of them renders (gate check 28).
    tpl = resolve_meta_slot(tpl, item.get("due_flag_text"))

    # action-tag (2026-09-18) shares .row-meta's line with due-flag/days-out,
    # so it can't be dropped via _CONDITIONAL's whole-line _drop_line() the
    # way canvas-link/source-link/add-reminder are — that would take the
    # entire .row-meta line, chip and all, with it. _drop_span(), same as
    # due-flag's own removal above, deletes only the inline span (§10: never
    # render an empty one).
    if not item.get("action_tag"):
        tpl = _drop_span(tpl, "action-tag")

    # Optional elements: delete the whole line, never render it empty (§10).
    for marker, key in _CONDITIONAL:
        if not item.get(key):
            tpl = _drop_line(tpl, marker)

    # AI menu items are earned per item and rendered from `ai_actions` only,
    # which is the pairing gate check 18 tests in both directions.
    earned = set(item.get("ai_actions") or [])
    for task in _AI_TASKS:
        if task not in earned:
            tpl = _drop_line(tpl, 'data-task="%s"' % task)

    # The chip means times_surfaced == 0 and nothing else (§5.6). The template
    # ships the with-chip variant; swap in the plain one otherwise, never an
    # empty span.
    if (item.get("times_surfaced") or 0) != 0:
        tpl = "\n".join(
            re.sub(r'<span class="row-new">New</span>', "", l) if 'class="row-new"' in l else l
            for l in tpl.split("\n"))

    # §14.1: a lead's basis is rendered as one escaped <li> per entry and is
    # never empty. Built here rather than passed as markup, so no caller can
    # hand the renderer a basis line it did not escape.
    if "{{DOSSIER_HTML}}" in tpl:
        # Markup built by dossier_html() from escaped values; spliced in
        # before fill() so it is not escaped a second time.
        tpl = tpl.replace("{{DOSSIER_HTML}}", item.get("dossier_html") or "")

    if "{{BASIS_ITEMS}}" in tpl:
        entries = item.get("basis") or ()
        if not entries:
            raise ValueError("lead %r has no basis — §14.1" % item.get("id"))
        # Claim and source used to be one escaped string — "claim — source" —
        # so the citation read in the SAME weight and colour as the claim
        # itself, indistinguishable from it. `.basis-src` mutes the source the
        # way `.row-meta` mutes a date relative to `.row-detail`: two different
        # kinds of fact, two different weights. `<li>` still opens directly on
        # the claim's own text (not a child tag), which is what gate check 24
        # actually requires — it does not care what comes after.
        # The source is dropped when it merely repeats the row's own
        # headline (a search result's page title IS its headline, so every
        # lead read "…claim — <same title again>"), and a claim cut at a
        # fixed width ends on a word boundary with an ellipsis rather than
        # mid-word ("Video Game-Pla").
        headline = str(item.get("title") or "").strip().lower()

        def _basis_li(b):
            src = str(b.get("source", "")).strip()
            tail = ('' if not src or src.lower() == headline else
                    ' <span class="basis-src">— %s</span>' % esc(src))
            return '<li>%s%s</li>' % (esc(_clip_claim(b.get("claim", ""))), tail)
        tpl = tpl.replace("{{BASIS_ITEMS}}", "".join(
            _basis_li(b) for b in entries))

    # §15: the two keys a rating generalises over, INJECTED as attributes
    # rather than carried as {{FAMILY}}/{{ORGANIZER}} placeholders.
    #
    # Placeholders were tried first and were wrong: the template would then
    # always carry them, and every path that builds a row by hand — the test
    # fixtures, and any future caller assembling markup directly — would leave
    # them unfilled and trip gate check 1. Injection means a row that has no
    # family simply has no attribute, and the client reads "" for it.
    #
    # Computed here rather than asked of the caller, because feedback.family_of()
    # is the single definition of "the same recurring thing" and a hand-built
    # value would drift from it.
    if 'data-action="rate"' in tpl:
        try:
            import feedback as _fb
            _fam = _fb.family_of(item) or ""
            _org = (_fb.organizer_of(item) or "")[4:]   # strip the "org:" tag
        except Exception:
            _fam, _org = "", ""
        _attrs = ""
        if _fam:
            _attrs += ' data-family="%s"' % esc(_fam)
        if _org:
            _attrs += ' data-organizer="%s"' % esc(_org)
        if _attrs:
            tpl = tpl.replace('data-context="{{EXPAND_CONTEXT}}">',
                              'data-context="{{EXPAND_CONTEXT}}"%s>' % _attrs, 1)

    return fill(tpl, {
        "ITEM_ID": item.get("id", ""),
        "EXPAND_CONTEXT": item.get("expand_context", ""),
        "COURSE_CLASS": item.get("course_class", "none"),
        "COURSE_LABEL": item.get("course_label", "UMD"),
        "TITLE": item.get("title", ""),
        "DETAIL": item.get("detail", ""),
        "DAYS_OUT_META": item.get("days_out_meta", ""),
        "DUE_FLAG_TEXT": item.get("due_flag_text", ""),
        "CANVAS_URL": item.get("canvas_url", ""),
        "ACTION_TAG": item.get("action_tag", ""),
        "ADD_REMINDER_URL": item.get("add_reminder_url", ""),
        "CONFIDENCE": item.get("confidence_tier", ""),
        "CONFIRM_ACTION": item.get("confirm_action", ""),
        "KILL_CRITERIA": item.get("kill_criteria", ""),
        "ACT_BY_LINE": item.get("act_by_line", ""),
        "SOURCE_URL": item.get("source_url", ""),
        "OPP_META": item.get("opp_meta", ""),
        "INELIGIBLE_NOTE": item.get("ineligible_note", ""),
    })

# The pipeline bucket -> the data-kind stamped on its rows.
SECTION_KIND = {
    "assignments": "assignment",
    "assessments": "assessment",
    "coming_up": "coming-up",
    "coming_up_more": "coming-up",
    "attention": "attention",
    "attention_more": "attention",
    "campus": "campus",
    "leads": "lead",
    "opportunities": "opportunity",
}


def _stamp_kind(html, section, seen=None):
    """Append data-kind (and data-seen) to the row's opening tag."""
    kind = SECTION_KIND.get(section)
    if not kind:
        return html
    m = re.search(_ROW_OPEN + r'[^>]*>', html)
    if not m:
        return html
    end = m.end() - 1
    attrs = ' data-kind="%s"' % kind
    if seen and re.match(r"^\d{4}-\d{2}-\d{2}$", str(seen)):
        attrs += ' data-seen="%s"' % seen
    return html[:end] + attrs + html[end:]


# ---------------------------------------------------------------------------
# layout: where each row goes (2026-09-17 design)
# ---------------------------------------------------------------------------

WEEK_DAYS = 5          # "This week" = the five days after today
STANDING_DETAIL_MAX = 60
_EXAM_RE = re.compile(r"\b(exam|midterm|final)s?\b", re.I)
# Obligation buckets, in the order rows sort within one day.
_OBLIGATIONS = ("assessments", "assignments", "coming_up", "coming_up_more")
# The "Where you stand" course order, as the design lists them.
STANDING_COURSES = ("ENGL001", "ECON001", "MATH001", "PHIL001", "CMSC001",
                    "CS-Advising")


def _as_date(value):
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    if not value:
        return None
    try:
        return _dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _minutes(value):
    """'14:00' / '4:00 PM' / '9:30 AM' -> minutes after midnight, or None."""
    if not value:
        return None
    s = str(value).strip().upper()
    m = re.match(r"^(\d{1,2}):(\d{2})\s*(AM|PM)?$", s)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    if ap == "PM" and h != 12:
        h += 12
    if ap == "AM" and h == 12:
        h = 0
    return h * 60 + mi if h < 24 and mi < 60 else None


def _clock(mins, short=False):
    if mins is None:
        return "TBD"
    h, m = divmod(mins, 60)
    ap = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    if short and m == 0:
        return "%d %s" % (h12, ap)
    return "%d:%02d %s" % (h12, m, ap)


def _md(d):
    return "%d/%d" % (d.month, d.day)


def _dow_md(d):
    return "%s %s" % (d.strftime("%a"), _md(d))


def _today_of(spec):
    d = _as_date(spec.get("today"))
    if d:
        return d
    title = (spec.get("scalars") or {}).get("ARTIFACT_TITLE", "")
    m = re.search(r"([A-Z][a-z]+ \d{1,2}), (\d{4})", title)
    if m:
        try:
            return _dt.datetime.strptime("%s %s" % (m.group(1), m.group(2)),
                                         "%B %d %Y").date()
        except ValueError:
            pass
    return _dt.date.today()


def _pill(text, cls="", accent=False, count=None, one=None, many=None):
    """A masthead pill. With `count`, it also carries data-count/data-one/
    data-many so the page can re-count it after a Done/Resolved click
    ("{n}" in the two formats is the number)."""
    classes = "pill" + (" is-accent" if accent else "") + (" " + cls if cls else "")
    attrs = ""
    if count:
        attrs = ' data-count="%s" data-one="%s" data-many="%s"' % (
            esc(count), esc(one), esc(many))
    return '<span class="%s"%s>%s</span>' % (classes, attrs, esc(text))


def _tag(item):
    return '<span class="course-tag %s">%s</span>' % (
        esc(item.get("course_class") or "none"), esc(item.get("course_label") or "UMD"))


def _time_cell(mins):
    return ('<span class="t-time"><span class="t-long">%s</span>'
            '<span class="t-short">%s</span></span>'
            % (esc(_clock(mins)), esc(_clock(mins, short=True))))


def _plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _is_exam(section, item):
    return section == "assessments" and bool(_EXAM_RE.search(item.get("title") or ""))


def _build_today(t, entries):
    entries = sorted(entries, key=lambda e: (
        _minutes(e[1].get("time")) if _minutes(e[1].get("time")) is not None else 24 * 60,
        _OBLIGATIONS.index(e[0])))
    return "\n".join(build_row(t, sec, item) for sec, item in entries)


def _build_week(t, today, by_day):
    out = []
    for i in range(1, WEEK_DAYS + 1):
        d = today + _dt.timedelta(days=i)
        entries = sorted(by_day.get(d, []), key=lambda e: _OBLIGATIONS.index(e[0]))
        groups, order = {}, []
        for sec, item in entries:
            key = item.get("course_label") or "UMD"
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append((sec, item))
        # An exam day is the ink card; its exam group leads the day.
        order.sort(key=lambda k: not any(_is_exam(s, it) for s, it in groups[k]))
        day_exam = any(_is_exam(s, it) for s, it in entries)
        cls = "wk-day" + (" is-exam" if day_exam else "") + ("" if entries else " is-empty")
        parts = ['<div class="%s">' % cls,
                 '  <span class="eyebrow">%s</span>' % esc(_dow_md(d))]
        for key in order:
            rows = groups[key]
            exam = any(_is_exam(s, it) for s, it in rows)
            parts.append('  <div class="wk-group%s">' % (" is-exam" if exam else ""))
            parts.append('    <span class="wk-date eyebrow">%s</span>' % esc(_md(d)))
            parts.append('    ' + _tag(rows[0][1]))
            parts.append('    <div class="wk-items">')
            for sec, item in rows:
                parts.append(_indent(build_row(t, sec, item), "      "))
            parts.append('    </div>')
            parts.append('  </div>')
        parts.append('  <p class="wk-none">Nothing due.</p>')
        parts.append('</div>')
        out.append("\n".join(parts))
    return "\n".join(out)


def _build_campus(t, today, rows):
    """Today's events in Midday / Evening bands; any other day under its own
    date. Location takes the sub-line, as in the design; the full description
    stays one click away in the row's detail."""
    def key(item):
        d = _as_date(item.get("date")) or today
        m = _minutes(item.get("time"))
        return (d, m if m is not None else 24 * 60)

    out, current = [], None
    for item in sorted(rows, key=key):
        d = _as_date(item.get("date")) or today
        m = _minutes(item.get("time"))
        if d == today:
            band = ("Anytime" if m is None and current is None else
                    current if m is None else
                    "Morning" if m < 11 * 60 else
                    "Midday" if m < 17 * 60 else "Evening")
        else:
            band = _dow_md(d)
        if band != current:
            out.append('<div class="t-group eyebrow">%s</div>' % esc(band))
            current = band
        shown = dict(item)
        shown["detail"] = item.get("location") or ""
        out.append('<div class="t-row">%s%s</div>'
                   % (_time_cell(m), build_row(t, "campus", shown)))
    return "\n".join(out)


_ADDR_TAIL = re.compile(r",?\s*(College Park|Md\.?|MD|Maryland)\b.*$")


def place_name(location):
    """"Tydings Hall, College Park, MD 20742" -> "Tydings Hall"; a street
    address after a building name is dropped too. Only ever shortens what
    the calendar stated."""
    loc = " ".join(str(location or "").split())
    if not loc:
        return ""
    first = loc.split(",")[0].strip()
    return first if first and not re.match(r"^\d", first) else _ADDR_TAIL.sub("", loc).strip(" ,")


def _gap_text(text):
    return re.sub(r"(\d+) minutes, (.+) to (.+)$",
                  lambda m: "%s min between %s and %s" % (
                      m.group(1), place_name(m.group(2)), place_name(m.group(3))),
                  str(text or ""))


def _title_key(title):
    return " ".join(re.findall(r"[a-z0-9]+", str(title or "").lower()))


def _build_schedule(entries, extras=()):
    """Today's calendar, with today's events from email/campus (`extras`,
    already shaped like timeline entries) slotted in by time. An event is a
    time on the day, not a deadline, so it lives here, not in Due cards."""
    listed = {_title_key(e.get("title")) for e in entries if "gap" not in e}
    extras = [x for x in extras if _title_key(x.get("title")) not in listed]
    timed = [e for e in entries]
    for x in extras:
        m = _minutes(x.get("time"))
        pos = len(timed)
        for i, e in enumerate(timed):
            em = _minutes(e.get("time")) if "gap" not in e else None
            if m is None or (em is not None and em > m):
                pos = i
                break
        timed.insert(pos, x)
    out = []
    for e in timed:
        if "gap" in e:
            if e.get("tight"):
                out.append('<div class="t-row"><span class="t-time"></span>'
                           '<p class="t-note tight">%s</p></div>' % esc(_gap_text(e["gap"])))
            continue
        place = place_name(e.get("location"))
        loc = ('<p class="t-sub">%s</p>' % esc(place)) if place else ""
        tag = _tag(e) if e.get("course_label") not in (None, "", "Personal") else ""
        title = esc(e.get("title", ""))
        if str(e.get("url") or "").startswith("https://"):
            title = '<a class="t-link" href="%s" target="_blank" rel="noopener">%s</a>' % (
                esc(e["url"]), title)
        cell = (_time_cell(_minutes(e.get("time"))) if _minutes(e.get("time")) is not None
                else '<span class="t-time">All day</span>')
        seen = (' data-seen="%s"' % esc(e["first_seen"])) if e.get("first_seen") else ""
        out.append('<div class="t-row%s"%s>%s<div><p class="t-title">%s%s</p>%s</div></div>'
                   % (" is-extra" if e.get("url") is not None else "", seen, cell, tag,
                      title, loc))
    return "\n".join(out)


def _as_schedule_entry(item):
    return {"time": item.get("time"), "title": item.get("title", ""),
            "location": item.get("location") or "",
            "course_class": item.get("course_class") or "none",
            "course_label": item.get("course_label") or "UMD",
            "url": item.get("source_url") or item.get("canvas_url") or "",
            "first_seen": item.get("first_seen")}


_CHANGE_KIND_CLASS = {"Announcement": "ann", "Moved": "moved", "Graded": "graded",
                      "New": "new", "Posted": "posted", "New page": "page",
                      "Updated": "page"}


def _build_changes(groups, courses, today=None):
    """spec["changes"] (service/digest.py) -> one block per course."""
    seen = (' data-seen="%s"' % today.isoformat()) if today else ""
    out = []
    for g in groups or ():
        label = g.get("course") or "UMD"
        cls = (courses or {}).get(label) or next(
            (c for c, labels in COURSE_PAIRS.items() if label in labels), "none")
        lis = []
        for e in g.get("entries") or ():
            text = esc(e.get("text", ""))
            if str(e.get("url") or "").startswith("https://"):
                text = '<a href="%s" target="_blank" rel="noopener">%s</a>' % (
                    esc(e["url"]), text)
            summary = ('<p class="chg-sum">%s</p>' % esc(e["summary"])
                       if e.get("summary") and e.get("summary") != e.get("text") else "")
            lis.append('<li class="chg chg-%s"><span class="chg-kind">%s</span>'
                       '<div class="chg-body"><p class="chg-text">%s</p>%s</div></li>'
                       % (_CHANGE_KIND_CLASS.get(e.get("label"), "new"),
                          esc(e.get("label", "")), text, summary))
        if lis:
            out.append('<div class="chg-course"%s><span class="course-tag %s">%s</span>'
                       '<ul class="chg-list">%s</ul></div>'
                       % (seen, esc(cls), esc(label), "".join(lis)))
    return "\n".join(out)


def _standing_row(template, item, bucket):
    """One Where-you-stand summary line (§16b, 2026-09-18). `item` is ALSO a
    full row somewhere else on the page (Today, This week, Further out, or
    Needs your attention), so this carries `data-ref`, never a second
    `data-item-id` -- gate check 5 would fail a real duplicate `.row`, and a
    bare `.tbl-row` is invisible to that check by construction (`_ROW_OPEN`
    requires the class to start with "row"). Its Mark done/Resolved buttons
    DO carry the real item's `data-item-id`: the generic click handler in
    the template's bootstrap script (`STORES.forEach`) reads the id off the
    BUTTON, not off the row, and posts to the exact same /items/{id}/done or
    /attention/{id}/resolved endpoint the canonical row's own button would.

    `bucket` is the pipeline section `item` actually came from
    ("assignments", "assessments", "coming_up", "coming_up_more", or
    "attention" for the no-upcoming-item fallback) -- it gates which action
    this line is even allowed to offer, matching app.py's own rules exactly
    (mark-done: assignment kind only; mark-resolved: the attention case).
    """
    tpl = block(template, "STANDING ROW")
    if bucket != "assignments":
        tpl = _drop_line(tpl, 'data-action="mark-done"')
    if bucket != "attention":
        tpl = _drop_line(tpl, 'data-action="mark-resolved"')
    for marker, key in (("canvas-link", "canvas_url"), ("source-link", "source_url")):
        if not item.get(key):
            tpl = _drop_line(tpl, marker)
    return fill(tpl, {
        "ITEM_ID": item.get("id", ""),
        "EXPAND_CONTEXT": item.get("expand_context", ""),
        "COURSE_CLASS": item.get("course_class", "none"),
        "COURSE_LABEL": item.get("course_label", "UMD"),
        "TEXT": item.get("standing_text", ""),
        "WHEN": item.get("standing_when", ""),
        "CANVAS_URL": item.get("canvas_url", ""),
        "SOURCE_URL": item.get("source_url", ""),
    })


def _standing_courses(spec):
    """The design's course order for the courses it knows, then any other
    course the account has (a new semester's), then CS-Advising last."""
    courses = spec.get("courses")
    if not courses:
        return STANDING_COURSES
    real = [c for c, cls in courses.items()
            if cls not in ("advising", "umd", "personal", "none")]
    ordered = [c for c in STANDING_COURSES if c in real] + sorted(
        c for c in real if c not in STANDING_COURSES)
    return tuple(ordered) + tuple(c for c, cls in courses.items()
                                  if cls == "advising")


def _build_standing(t, today, spec, placed):
    """The next open obligation per course; an overdue attention row stands in
    for a course with nothing upcoming."""
    sections = spec.get("sections") or {}
    lines = []
    for course in _standing_courses(spec):
        best = None
        for sec, item in placed:
            if item.get("course_label") != course:
                continue
            d = _as_date(item.get("date"))
            k = (d is None, d or _dt.date.max)
            if best is None or k < best[0]:
                best = (k, sec, item, d)
        if best:
            _, bucket, item, d = best
            when = ("today" if d is not None and d <= today else
                    _dow_md(d) if d is not None else "check deadline")
        else:
            late = [i for i in (sections.get("attention") or []) +
                    (sections.get("attention_more") or [])
                    if i.get("course_label") == course]
            if not late:
                continue
            item, when, bucket = late[0], "overdue", "attention"
        text = item.get("title") or ""
        detail = (item.get("detail") or "").strip()
        if detail and len(detail) <= STANDING_DETAIL_MAX and detail != text:
            text = "%s · %s" % (text, detail.rstrip("."))
        # STEP 5d (2026-09-19) -- a compact, honest grade readout, when
        # service/grades.py had one for this course. Appended here (not a
        # new template slot) so it shares TEXT's existing " · "-joined
        # sentence rather than adding a fourth grid column; grades.
        # grade_display_text() already refuses to claim more than the
        # sample size backs ("not yet graded (0 of N)" rather than a bare
        # score from one data point), so nothing here re-decides that.
        grade_text = (spec.get("grades") or {}).get(course) or ""
        if grade_text:
            # Labelled: "Weekly SmartBook — 100% (6 of 45 graded)" read as
            # if that one upcoming item had scored 100%. The number is the
            # COURSE's current grade, so it says so.
            text = "%s · %s" % (text, (
                "no grades posted yet" if grade_text.startswith("not yet graded")
                else "course grade " + grade_text))
        if item.get("id") and not item.get("grouped"):
            shown = dict(item)
            shown["standing_text"] = text
            shown["standing_when"] = when
            lines.append(_standing_row(t, shown, bucket))
        else:
            # A §5.8 grouped row (3+ recurring items collapsed into one) has
            # no single id to echo an action for -- same reason the GROUPED
            # ROW template itself carries no menu. Render the same inert
            # summary line the design always showed here rather than
            # dropping the course from the table because its nearest
            # obligation happens to be a group.
            lines.append(
                '<div class="tbl-row"><span class="course-tag %s">%s</span>'
                '<p class="tbl-text">%s</p><span class="tbl-when">%s</span></div>'
                % (esc(item.get("course_class") or "none"),
                   esc(item.get("course_label") or "UMD"), esc(text), esc(when)))
    for note in spec.get("portfolio") or []:
        lines.append('<div class="tbl-row tbl-note">%s</div>' % esc(note))
    return "\n".join(lines)


def _next_exam_pill(today, spec):
    best = None
    for item in (spec.get("sections") or {}).get("assessments") or []:
        d = _as_date(item.get("date"))
        if d is None or d < today or not _is_exam("assessments", item):
            continue
        if best is None or d < best[0]:
            best = (d, item)
    if not best:
        return ""
    d, item = best
    n = (d - today).days
    when = "today" if n == 0 else "tomorrow" if n == 1 else "in %d days" % n
    return _pill("%s (%s) %s" % (item.get("title", ""), item.get("course_label", ""), when),
                 "pill-wide")


def build(spec, template):
    """spec -> (html, problems). `template` is a path or the template text.

    spec = {"today": "2026-09-17", "scalars": {...}, "timeline": [...],
            "sections": {"assignments": [item, ...], ...},
            "coverage": [...], "portfolio": [...], "caps": {...}}

    Placement (the 2026-09-17 design): an obligation dated today or earlier is
    a Today card; one inside the next WEEK_DAYS days goes in its day column;
    anything later, or undated, is Further out. Campus rows keep their own
    section, attention rows theirs. The `items` map the gate needs is derived
    from the spec, so it can never disagree with what was rendered.
    """
    t = read_template(template)
    today = _today_of(spec)
    S = spec.get("sections") or {}
    ctx = dict(spec.get("scalars") or {})
    items = {}

    def remember(section, item):
        if item.get("grouped"):
            return
        items[item.get("id", "")] = {
            "ai_actions": item.get("ai_actions") or [],
            "times_surfaced": item.get("times_surfaced") or 0,
            "regime": item.get("regime") or ("lead" if section == "leads" else "confirmed"),
            "date": item.get("date"),
            "today": today.isoformat(),
            # The row-kind this item's CANONICAL row renders as — validate()
            # check 16 cross-checks Where-you-stand's echoed mark-done/
            # mark-resolved buttons against this, since a .tbl-row is
            # invisible to _all_rows() and so has no _kind() of its own.
            "kind": SECTION_KIND.get(section),
        }

    today_rows, by_day, further, placed = [], {}, [], []
    schedule_extras = []
    horizon = today + _dt.timedelta(days=WEEK_DAYS)
    for sec in _OBLIGATIONS:
        for item in S.get(sec) or []:
            d = _as_date(item.get("date"))
            if sec in ("coming_up", "coming_up_more") and \
                    item.get("kind") in ("event", "advising") and d == today:
                # A talk or workshop today is a time on the day, not
                # something due: it goes on the calendar, not in Due cards.
                schedule_extras.append(_as_schedule_entry(item))
                continue
            remember(sec, item)
            placed.append((sec, item))
            if d is None or d > horizon:
                further.append((sec, item))
            elif d <= today:
                today_rows.append((sec, item))
            else:
                by_day.setdefault(d, []).append((sec, item))
    for sec in ("attention", "attention_more", "campus", "leads", "opportunities"):
        for item in S.get(sec) or []:
            remember(sec, item)

    sections = {}
    sections["today"] = _build_today(t, today_rows)
    sections["week"] = _build_week(t, today, by_day)

    def further_key(e):
        d = _as_date(e[1].get("date"))
        return (d is None, d or _dt.date.max, _OBLIGATIONS.index(e[0]))
    further.sort(key=further_key)
    # §16c: a pure-FYI event/advising row (lifecycle.is_fyi(), set on the
    # item by orchestrator._prep_row()) is demoted into further_more's own
    # disclosure rather than crowding the top of Further out alongside real
    # deadlines — reviewing the 2026-09-18 brief found exactly this: a Town
    # Hall announcement and a bare career-fair listing sorting ahead of a
    # midterm purely because both had a nearer date. Assignments/assessments
    # are never FYI (is_fyi() returns False for them unconditionally), so
    # this never touches real coursework.
    further_primary = [(sec, item) for sec, item in further if not item.get("fyi")]
    further_secondary = [(sec, item) for sec, item in further if item.get("fyi")]

    def _further_row(sec, item):
        shown = dict(item)
        d = _as_date(item.get("date"))
        shown["due_flag_text"] = ""
        shown["days_out_meta"] = _dow_md(d) if d else (item.get("days_out_meta") or "no date")
        return build_row(t, sec, shown)

    sections["further"] = "\n".join(_further_row(sec, item) for sec, item in further_primary)
    if further_secondary:
        sections["further_more"] = "\n".join(
            _further_row(sec, item) for sec, item in further_secondary)

    campus = S.get("campus") or []
    sections["campus"] = _build_campus(t, today, campus)
    sections["schedule"] = _build_schedule(spec.get("timeline") or [],
                                           schedule_extras)
    sections["changes"] = _build_changes(spec.get("changes"), spec.get("courses"),
                                         today)
    def attention_row(section, item):
        # The attention card has one sub-line. With no detail, say which
        # course and when, so "Quiz 1" is never a bare title.
        shown = dict(item)
        if not (item.get("detail") or "").strip():
            bits = [b for b in (item.get("course_label") if item.get("course_label")
                                not in ("System", "UMD", "Personal") else "",
                                item.get("days_out_meta") or "") if b]
            shown["detail"] = " · ".join(bits)
        return build_row(t, section, shown)
    sections["attention"] = "\n".join(attention_row("attention", i) for i in S.get("attention") or [])
    sections["attention_more"] = "\n".join(
        attention_row("attention_more", i) for i in S.get("attention_more") or [])
    sections["leads"] = "\n".join(build_row(t, "leads", i) for i in S.get("leads") or [])
    sections["opportunities"] = "\n".join(
        build_row(t, "opportunities", i) for i in S.get("opportunities") or [])
    sections["standing"] = _build_standing(t, today, spec, placed)
    sections["coverage"] = "\n".join(
        '<div class="coverage-note">%s</div>' % esc(n) for n in spec.get("coverage") or [])

    # Masthead pills, in the design's order. A zero is never a pill.
    campus_today = sum(1 for i in campus if (_as_date(i.get("date")) or today) == today)
    n_attn = len(S.get("attention") or [])
    pills = []
    if today_rows:
        pills.append(_pill("%d due today" % len(today_rows), count="today",
                           one="{n} due today", many="{n} due today"))
    if campus_today:
        pills.append(_pill(_plural(campus_today, "campus event") + " today", "pill-wide"))
    if n_attn:
        pills.append(_pill("%d need%s a look" % (n_attn, "s" if n_attn == 1 else ""),
                           accent=True, count="attention",
                           one="{n} needs a look", many="{n} need a look"))
    exam = _next_exam_pill(today, spec)
    if exam:
        pills.append(exam)
    # Item 5 (2026-09-19): the masthead's own "shape of the day" strip named
    # every obligation-side count (due, campus, attention, next exam) but
    # nothing from Leads -- opening the page gave no signal that discovery
    # found anything worth reading until you scrolled all the way past
    # Further out. A lead is opportunity, not obligation, so it goes last,
    # same ordering principle as the page's own sections.
    n_opps = len(S.get("opportunities") or [])
    n_leads = len(S.get("leads") or [])
    if n_opps:
        pills.append(_pill(_plural(n_opps, "opportunity").replace(
            "opportunitys", "opportunities") + " to consider"))
    elif n_leads:
        pills.append(_pill(_plural(n_leads, "lead") + " worth a look"))
    sections["pills"] = "\n".join(pills)
    ctx["sections"] = sections

    days = [today + _dt.timedelta(days=i) for i in (1, WEEK_DAYS)]
    ctx.setdefault("WEEKDAY", today.strftime("%A"))
    ctx.setdefault("WEEKDAY_SHORT", str(ctx["WEEKDAY"]).strip(" ,")[:3])
    ctx.setdefault("TODAY_COUNT_LABEL", _plural(len(today_rows), "item"))
    ctx.setdefault("SCHEDULE_COUNT_LABEL", _plural(
        sum(1 for e in spec.get("timeline") or [] if "gap" not in e)
        + len(schedule_extras), "event"))
    ctx.setdefault("CHANGES_RANGE", "since yesterday's brief")
    ctx.setdefault("OPPS_STAMP", "")
    ctx.setdefault("CAMPUS_HEADING", "Today — around campus"
                   if campus_today or not campus else "This week — around campus")
    ctx.setdefault("WEEK_RANGE", "%s – %s" % (days[0].strftime("%a"), days[1].strftime("%a")))
    ctx.setdefault("ATTENTION_COUNT", str(n_attn))
    # §16c: describes what's actually inline (further_primary), matching how
    # ATTENTION_COUNT above counts only Needs-your-attention's own primary
    # rows, not its attn-more overflow.
    last = max((_as_date(i.get("date")) for _, i in further_primary if _as_date(i.get("date"))),
               default=None)
    ctx.setdefault("FURTHER_RANGE", _plural(len(further_primary), "item")
                   + (" · through %s %d" % (last.strftime("%b"), last.day) if last else ""))
    if further_secondary:
        ctx.setdefault("FURTHER_MORE_LABEL", "%s — informational, no action needed" % (
            _plural(len(further_secondary), "item")))
    ctx.setdefault("RUN_SHORT", _md(today))
    ctx.setdefault("WEEK_STAMP", "")

    html = render(t, ctx)
    caps = spec.get("caps") or {}
    return html, validate(html, template_path=t, items=items, caps=caps,
                          courses=spec.get("courses"))


def main(argv):
    """CLI so step 8 is one call and the page never enters context.

        python3 render_briefing.py spec.json template.html briefing.html

    Prints nothing on success and the problem list on failure.
    """
    import json
    spec = json.load(open(argv[1], encoding="utf-8"))
    html, problems = build(spec, argv[2])
    with open(argv[3], "w", encoding="utf-8") as fh:
        fh.write(html)
    for line in problems:
        print(line)
    return 1 if problems else 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv))
