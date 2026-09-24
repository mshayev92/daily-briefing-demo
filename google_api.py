"""Gmail and Drive through this account's OWN OAuth credentials.

Why this module exists
----------------------
Every Gmail and Drive call in this pipeline used to go through an MCP
connector. Connector calls are made by the model, so every byte they return —
email bodies, the 47 KB state file, folder listings — lands in context and is
re-sent on every subsequent turn. Python calling the same APIs directly keeps
those bytes in RAM: only what a function RETURNS is a token cost.

That is the whole point, and it sets the rule the governance docs now carry:
**the API is primary, the connector is a reported fallback, never a first
choice.** See PROJECT_INSTRUCTIONS.md 1.8.

Credentials
-----------
`credentials.json`  the OAuth *client* (client id + secret). Downloaded once
                    from Cloud Console. Identifies the app, grants nothing.
`token.json`        the *authorization* — access + refresh token, and the
                    granted scope list. Written by the first interactive run.

Both live in the bound working folder. Neither is ever read into context: a run
calls `health()` and reads the verdict, and §1.8 forbids `cat` on either. They
are live credentials to a mailbox; a token in a transcript is a token leaked.

Token lifetime — this project's ACTUAL conditions
-------------------------------------------------
This OAuth client lives in the UMD Google Workspace org with consent screen
user type **Internal**, and the authorizing account is the same
`@terpmail.umd.edu` identity. Confirmed 2026-09-10.

**Internal apps have no 7-day refresh-token expiry.** That rule applies only
to External apps in Testing publishing status, where authorizations by a test
user expire seven days from consent. An earlier version of this module was
written for that case and warned about a deadline this project does not have —
which was worse than useless, because a MAJOR row every other day trains the
reader to ignore MAJOR rows.

What can still revoke this token, all from Google's general list:

1. **Six months without a successful refresh.** A daily run refreshes roughly
   every 24 hours, so this clock never advances past 1 day in practice. Note
   what "used" means: a successful *refresh* call, not an API call with an
   already-valid access token.
2. **A password change on the account, while the token holds Gmail scopes.**
   This one is live and unavoidable — `gmail.modify` is exactly such a scope.
   Change the terpmail password and the next morning's run has no Gmail.
3. **User revokes access** in Google Account permissions.
4. **Exceeding ~100 live refresh tokens for this client.** Reached only by
   re-authorizing repeatedly; the oldest is then invalidated without warning.
5. **A Workspace admin scope restriction** (`admin_policy_enforced`) or a
   session-control policy. UMD's IT could impose either without telling a
   student, and it would present as `invalid_grant` with an `error_subtype`.

So `health()` no longer counts down to a deadline. It reports **staleness** —
days since the last successful refresh — which is the only one of the five
that is measurable from disk, and it flags the rest by classifying the error
when a refresh actually fails. Cause (2) and (5) are not predictable; they are
reportable, which is what STEP 0 does with the verdict.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone

# --- scopes -----------------------------------------------------------------
# gmail.modify  read full bodies (§4.5 requires the whole body, so
#               gmail.metadata is disqualified — it returns headers only) plus
#               the label add/remove the sweep exists to do.
# drive         NOT drive.file. drive.file only reaches files this OAuth client
#               itself created, and every state file in `Claude Assistant` was
#               created by the connector under a different client. A drive.file
#               token lists an empty folder and reports no state — the worst
#               possible failure, because it looks like first-run empty state.
#
# Deliberately absent: gmail.send. §1.3's one email a day stays on the
# connector (§1.8's table). Adding a send scope to an unattended token buys
# nothing measurable and widens the blast radius of a leaked token to
# "can send mail as this account".
#
# calendar.readonly -- STEP 4 (§2.2): read-only access to the four calendars
# (`Class`, `Canvas`, `Syllabi Dates`, the personal calendar). This pipeline
# never creates, edits or deletes a calendar event, so the write scope
# (`calendar`) is never requested -- same "narrowest scope that does the job"
# rule §1.3 already applies to gmail.send.
#
# Added 2026-09-16. The existing `token.json` predates this line and does
# NOT carry the scope -- `health()`'s missing-scope check will say so until
# `python google_api.py --authorize` is run again, interactively, to grant
# it. A refresh cannot widen a granted scope (see `health()` below).
SCOPES = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/calendar.readonly",
)

CLIENT_FILE = "credentials.json"
TOKEN_FILE = "token.json"

# Google invalidates a refresh token unused for six consecutive months.
# Internal apps have no 7-day rule (see the module docstring).
IDLE_LIMIT_DAYS = 180
# Warn well before the cliff: a run that has not refreshed in this long means
# the pipeline has not actually run, which is its own problem worth surfacing.
WARN_IDLE_DAYS = 150


class AuthError(Exception):
    """Raised with a `kind` a run can branch on rather than parse."""

    def __init__(self, kind, detail=""):
        self.kind = kind          # missing_client | missing_token |
        self.detail = detail      # expired | scope_short | refresh_failed
        super().__init__("%s: %s" % (kind, detail) if detail else kind)


def classify_refresh_error(exc):
    """Turn an `invalid_grant` into the cause a run should report.

    Google returns the same error for revocation, idle expiry, a password
    change and an admin policy, so this cannot be certain. It names the
    likely causes in the order they matter for THIS project rather than
    printing a bare `invalid_grant` that tells the reader nothing.
    """
    text = str(exc)
    low = text.lower()
    if "invalid_rapt" in low or "admin_policy" in low:
        return ("%s — a UMD Workspace session-control or scope policy, not a "
                "token problem. Re-authorizing will not help until the policy "
                "changes." % text)
    if "invalid_grant" in low:
        return ("%s — most likely the terpmail password changed (revokes any "
                "token holding Gmail scopes), access was revoked in Google "
                "Account permissions, or a UMD admin policy applies. Not the "
                "7-day rule: this is an Internal app. Re-authorize with "
                "`python google_api.py --authorize`." % text)
    return text


def _paths(root=""):
    return (os.path.join(root, CLIENT_FILE), os.path.join(root, TOKEN_FILE))


def _read_token(root=""):
    """The token file as a dict. Returned to callers in this module only."""
    _, tok = _paths(root)
    if not os.path.exists(tok):
        raise AuthError("missing_token",
                        "%s not found; run `python google_api.py --authorize`"
                        % tok)
    with open(tok, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _staleness(data, now=None):
    """(last_refresh, idle_days) — how long since a successful refresh.

    google-auth writes `expiry` for the ACCESS token, which is an hour out and
    tells you nothing about the refresh token. `last_refresh` is ours, stamped
    by `_services()` every time a refresh succeeds, and it is the only input to
    the six-month idle rule that can be read off disk.

    Falls back to `consent_at` when no refresh has happened yet: a token
    authorized this morning is not stale, and reporting "unknown" for it would
    make the common case look like a problem.
    """
    now = now or datetime.now(timezone.utc)
    raw = data.get("last_refresh") or data.get("consent_at")
    if not raw:
        return None, None
    s = str(raw).replace("Z", "+00:00")
    try:
        at = datetime.fromisoformat(s)
    except ValueError:
        return None, None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at, (now - at).total_seconds() / 86400.0


def health(root="", now=None):
    """The STEP 0 precheck. Returns a verdict dict; never raises, never prints
    a credential.

    keys: ok, severity, scopes_ok, missing_scopes, idle_days, message
    """
    client, tok = _paths(root)
    out = {"ok": False, "severity": "CRITICAL", "scopes_ok": False,
           "missing_scopes": [], "idle_days": None, "message": ""}

    if not os.path.exists(client):
        out["message"] = "%s missing — no OAuth client; Gmail and Drive " \
                         "cannot be reached by API at all" % client
        return out
    try:
        data = _read_token(root)
    except AuthError as exc:
        out["message"] = exc.detail or str(exc)
        return out

    granted = set(str(data.get("scopes") or data.get("scope") or "").split()
                  if isinstance(data.get("scopes"), str)
                  else (data.get("scopes") or []))
    missing = [s for s in SCOPES if s not in granted]
    out["missing_scopes"] = missing
    out["scopes_ok"] = not missing

    if not data.get("refresh_token"):
        # An access-token-only file. Works for at most an hour, then the run
        # has no route and no way to get one unattended.
        out["message"] = ("%s has no refresh_token — re-authorize with "
                          "`python google_api.py --authorize`; an unattended "
                          "run cannot complete a consent flow" % tok)
        return out

    if missing:
        # A token that predates a scope addition. Refresh will not add it;
        # only a fresh consent will. Naming the scope is the whole message.
        out["message"] = ("token lacks %s — delete %s and re-authorize; a "
                          "refresh cannot widen a granted scope"
                          % (", ".join(missing), tok))
        return out

    _, idle = _staleness(data, now)
    out["idle_days"] = None if idle is None else round(idle, 2)

    if idle is None:
        out.update(ok=True, severity="MAJOR")
        out["message"] = ("token carries no last_refresh or consent_at stamp, "
                          "so idle time cannot be checked; re-authorize to "
                          "start tracking it")
        return out
    if idle >= IDLE_LIMIT_DAYS:
        out["message"] = ("token has not refreshed in %.0f days, past "
                          "Google's six-month idle limit; expect "
                          "invalid_grant. Re-authorize." % idle)
        return out
    if idle >= WARN_IDLE_DAYS:
        out.update(ok=True, severity="MAJOR")
        out["message"] = ("token has not refreshed in %.0f days (six-month "
                          "idle limit is %d). This also means the pipeline "
                          "has not run in that time, which is the larger "
                          "problem." % (idle, IDLE_LIMIT_DAYS))
        return out

    out.update(ok=True, severity="INFO")
    out["message"] = ("credentials ok; last refresh %.1f days ago, well "
                      "inside the six-month idle limit" % idle)
    return out



def _private_open(path):
    """Open a credential file for writing, owner-only (0600) from the first
    byte. A plain open() honours the umask (002 here), which left
    send_token.json -- a token that can SEND mail as this account --
    readable by every local user (found in the 2026-09-19 audit)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(path, 0o600)  # also tighten a file that already existed
    return os.fdopen(fd, "w", encoding="utf-8")


def _creds(root=""):
    """Build valid OAuth credentials, refreshing and re-stamping as needed.

    Split out of `_services()` (was inlined there) so `_services()` and
    `_calendar_service()` share one refresh/stamp path rather than each
    growing its own copy — the `last_refresh` write is what `health()`'s
    staleness check reads, and two copies drifting apart would make that
    check lie about which call last actually refreshed.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as exc:
        raise AuthError("missing_library",
                        "%s — pip install google-api-python-client "
                        "google-auth-oauthlib" % exc)

    _, tok = _paths(root)
    creds = Credentials.from_authorized_user_file(tok, list(SCOPES))
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:
                raise AuthError("refresh_failed", classify_refresh_error(exc))
            # Preserve our own stamps across the rewrite: google-auth drops
            # unknown keys, and `consent_at` / `last_refresh` are the only
            # record of when this token was granted and last worked.
            try:
                with open(tok, "r", encoding="utf-8") as fh:
                    prior = json.load(fh).get("consent_at")
            except (OSError, ValueError):
                prior = None
            data = json.loads(creds.to_json())
            if prior:
                data["consent_at"] = prior
            data["last_refresh"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            with _private_open(tok) as fh:
                json.dump(data, fh)
        else:
            raise AuthError("missing_token", "no usable refresh token in %s"
                            % tok)
    return creds


def _services(root=""):
    """Build (gmail, drive). Imports are local so `health()` works without
    google-auth installed — a missing library must be reportable, not an
    ImportError at module load that takes STEP 0 down with it."""
    creds = _creds(root)
    from googleapiclient.discovery import build as _b
    return (_b("gmail", "v1", credentials=creds, cache_discovery=False),
            _b("drive", "v3", credentials=creds, cache_discovery=False))


def _calendar_service(root=""):
    """Build the read-only Calendar client. STEP 4's entry point (§2.2)."""
    creds = _creds(root)
    from googleapiclient.discovery import build as _b
    return _b("calendar", "v3", credentials=creds, cache_discovery=False)


# --- Gmail ------------------------------------------------------------------

def labels(root=""):
    """{name: id} for every label. §2.2b's resolve-once source of truth.

    The account is authoritative for names (§2.2b): this returns what exists,
    not what any doc claims exists.
    """
    gmail, _ = _services(root)
    res = gmail.users().labels().list(userId="me").execute()
    return {l["name"]: l["id"] for l in res.get("labels", [])}


def search_threads(query, root="", cap=400):
    """Thread ids and subjects for a Gmail query. Bodies NOT fetched.

    Returns [{"id","subject","from","date","snippet_len"}]. Deliberately no
    snippet text: the caller decides which threads deserve a body read, and a
    snippet in the return value is a body read by the back door (§4.5).
    """
    gmail, _ = _services(root)
    out, token = [], None
    while len(out) < cap:
        res = gmail.users().threads().list(
            userId="me", q=query, pageToken=token,
            maxResults=min(100, cap - len(out))).execute()
        for t in res.get("threads", []):
            out.append({"id": t["id"], "snippet_len": len(t.get("snippet", ""))})
        token = res.get("nextPageToken")
        if not token:
            break
    return out


def _walk_parts(part, acc, mime="text/plain"):
    if part.get("mimeType") == mime and part.get("body", {}).get("data"):
        acc.append(part["body"]["data"])
    for sub in part.get("parts", []) or []:
        _walk_parts(sub, acc, mime)


def thread_bodies(thread_id, root=""):
    """Full plain-text bodies for one thread. §4.5's once-ever body read.

    Returns {"id","headers":{...},"messages":[text,...]}. HTML-only messages
    fall back to the HTML part, tags intact — stripping them here would be a
    silent content edit, and §1.5 makes body text data to be escaped
    downstream, not cleaned up in transit.
    """
    import base64
    gmail, _ = _services(root)
    msg = gmail.users().threads().get(
        userId="me", id=thread_id, format="full").execute()
    texts, headers = [], {}
    for m in msg.get("messages", []):
        payload = m.get("payload", {})
        if not headers:
            headers = {h["name"]: h["value"]
                       for h in payload.get("headers", [])
                       if h["name"] in ("From", "Subject", "Date", "To",
                                        "List-Id")}
        parts = []
        _walk_parts(payload, parts)
        if not parts:
            # multipart/alternative with no text/plain alternative at all
            # (common for newsletters) -- the HTML part is the only body.
            _walk_parts(payload, parts, "text/html")
        if not parts and payload.get("body", {}).get("data"):
            parts = [payload["body"]["data"]]
        for data in parts:
            texts.append(base64.urlsafe_b64decode(data).decode(
                "utf-8", "replace"))
    return {"id": thread_id, "headers": headers, "messages": texts}


def relabel(thread_id, add=(), remove=(), root=""):
    """Add/remove label ids on a thread. The sweep's write (§2.3).

    Ids only, never names — §2.2b forbids constructing an id, and this
    signature is where that rule is enforced rather than trusted.
    """
    gmail, _ = _services(root)
    body = {"addLabelIds": list(add), "removeLabelIds": list(remove)}
    gmail.users().threads().modify(
        userId="me", id=thread_id, body=body).execute()
    return True


# --- Calendar (read-only) ----------------------------------------------------

def list_calendars(root=""):
    """{summary: id} for every calendar on this account.

    §2.2's resolve-once source of truth for `Class` / `Canvas` /
    `Syllabi Dates` / the personal calendar's ids, mirroring `labels()` for
    Gmail. Not needed on the common path — `state.calendars` already caches
    the four ids — only when a cached id stops resolving and §2.2 says
    re-match by name once.
    """
    calendar = _calendar_service(root)
    out, token = {}, None
    while True:
        res = calendar.calendarList().list(pageToken=token).execute()
        out.update({c["summary"]: c["id"] for c in res.get("items", [])})
        token = res.get("nextPageToken")
        if not token:
            return out


def calendar_events(calendar_id, time_min, time_max, root=""):
    """Timed + all-day events on one calendar within [time_min, time_max).

    `time_min`/`time_max` are RFC 3339 timestamps (a bare offset like
    `2026-09-16T00:00:00-04:00` is fine). Returns
    `[{"title","location","description","start","end","all_day"}]`, `start`/
    `end` as ISO strings (`schedule.py`'s `_dt()` parses either a timed
    `dateTime` or an all-day `date`) — the caller decides what to do with an
    all-day event; this function does not drop one (`schedule._clean()`
    already does, per §2.2's "all-day personal events are dropped").

    `singleEvents=True` expands recurring events (every "Class"-calendar
    meeting is one) into individual instances — without it a recurring
    event returns once, with its FIRST occurrence's time, which would show
    today's lecture at whatever hour the semester started.

    Cancelled instances (a class meeting removed from one day only) are
    skipped rather than rendered as a zero-length or phantom row.
    """
    calendar = _calendar_service(root)
    out, token = [], None
    while True:
        res = calendar.events().list(
            calendarId=calendar_id, timeMin=time_min, timeMax=time_max,
            singleEvents=True, orderBy="startTime", maxResults=250,
            pageToken=token).execute()
        for e in res.get("items", []):
            if e.get("status") == "cancelled":
                continue
            start, end = e.get("start", {}), e.get("end", {})
            out.append({
                "title": e.get("summary") or "",
                "location": (e.get("location") or "").strip(),
                "description": e.get("description") or "",
                "start": start.get("dateTime") or start.get("date"),
                "end": end.get("dateTime") or end.get("date"),
                "all_day": "dateTime" not in start,
            })
        token = res.get("nextPageToken")
        if not token:
            return out


# --- Drive ------------------------------------------------------------------

def folder_ok(folder_id, root=""):
    """True if the id resolves to a live (non-trashed) folder."""
    _, drive = _services(root)
    try:
        meta = drive.files().get(
            fileId=folder_id, fields="id,mimeType,trashed").execute()
    except Exception:
        return False
    return (meta.get("mimeType") == "application/vnd.google-apps.folder"
            and not meta.get("trashed"))


def find_folder(name, root=""):
    """Folder id by exact name, or None. The §7 / STEP 9 recovery path."""
    _, drive = _services(root)
    res = drive.files().list(
        q=("mimeType='application/vnd.google-apps.folder' and trashed=false "
           "and name='%s'" % name.replace("'", "\\'")),
        fields="files(id,name)", pageSize=10).execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def list_folder(folder_id, prefix="", root=""):
    """[{"id","name"}] in a folder, optionally name-prefixed.

    Replaces `search_files`, and structurally cannot repeat its worst habit:
    the connector returns content snippets by default, which is why every
    documented call had to pass `excludeContentSnippets: true`. This asks for
    id and name, so there is nothing to exclude.
    """
    _, drive = _services(root)
    q = "'%s' in parents and trashed=false" % folder_id
    if prefix:
        q += " and name contains '%s'" % prefix.replace("'", "\\'")
    out, token = [], None
    while True:
        res = drive.files().list(
            q=q, fields="nextPageToken,files(id,name)", pageSize=200,
            pageToken=token).execute()
        out.extend({"id": f["id"], "name": f["name"]}
                   for f in res.get("files", []))
        token = res.get("nextPageToken")
        if not token:
            return out


def read_text(file_id, root=""):
    """A file's bytes as text. No base64 round-trip through context (§3.1)."""
    _, drive = _services(root)
    return drive.files().get_media(fileId=file_id).execute().decode(
        "utf-8", "replace")


def state_reader(folder_id, root=""):
    """(names, read_text_by_name) to hand straight to state_io.load().

    state_io.load takes a `read_text(name)` callable precisely so it does not
    know whether it is reading a local folder or Drive. This is the Drive half.
    """
    entries = list_folder(folder_id, "college_assistant_state", root=root)
    by_name = {e["name"]: e["id"] for e in entries}

    def _read(name):
        return read_text(by_name[name], root=root)

    return list(by_name), _read


def create_json(folder_id, name, text, root=""):
    """Upload `text` as a new JSON file. Returns the new file id.

    This is the one write that genuinely does not need the bytes in context:
    the caller passes a path or a string Python already holds, so the state
    file's ~47 KB stops crossing context on the write as well as the read.
    """
    from googleapiclient.http import MediaInMemoryUpload
    _, drive = _services(root)
    media = MediaInMemoryUpload(text.encode("utf-8"),
                                mimetype="application/json", resumable=False)
    created = drive.files().create(
        body={"name": name, "parents": [folder_id]},
        media_body=media, fields="id").execute()
    return created["id"]


def create_file_from_path(folder_id, name, path, mimetype="application/octet-stream",
                          root=""):
    """Upload the file AT `path` as a new Drive file. Returns the new file id.

    Additive, Phase 5 (nightly SQLite backup): `create_json()` takes a
    string, which is right for the ~47 KB JSON state but wrong for a binary
    SQLite file -- `MediaFileUpload` streams it from disk instead of ever
    holding it as a Python string.
    """
    from googleapiclient.http import MediaFileUpload
    _, drive = _services(root)
    media = MediaFileUpload(path, mimetype=mimetype, resumable=False)
    created = drive.files().create(
        body={"name": name, "parents": [folder_id]},
        media_body=media, fields="id").execute()
    return created["id"]


# §1.2's two named sets, as patterns: a dated state file, or a legacy backup
# from the pre-2026-09-06 write scheme. Anything else is not trashable here.
# The `briefing_backup_` pattern is Phase 5's addition, for the new SQLite
# nightly backup's own retention pruning -- same guard, same reasoning: a
# filename this function does not recognize is refused, not guessed at.
TRASHABLE = (
    re.compile(r"^college_assistant_state_\d{4}-\d{2}-\d{2}T\d{6}Z\.json$"),
    re.compile(r"^college_assistant_state_superseded_.*\.json$"),
    re.compile(r"^college_assistant_state_backup_.*\.json$"),
    re.compile(r"^college_assistant_state_temp.*$"),
    re.compile(r"^briefing_backup_\d{4}-\d{2}-\d{2}T\d{6}Z\.sqlite$"),
)
# The legacy active file. §1.2: "leave it alone forever."
NEVER_TRASH = ("college_assistant_state.json",)


def trashable_name(name):
    """Does §1.2 permit this FILENAME to be trashed at all?

    Name-level only: the 14-day age test and the keep-the-14-most-recent rule
    stay with the caller, because neither can be answered from one filename.
    """
    n = str(name or "").strip()
    if not n or n in NEVER_TRASH:
        return False
    return any(rx.match(n) for rx in TRASHABLE)


def trash(file_id, name=None, root=""):
    """Move to trash. Refuses any name §1.2 does not permit.

    Passing `name` turns §1.2's file-set rule from something the caller is
    trusted to have applied into something this function enforces. This is the
    most destructive call in the project and it used to take an opaque id and
    do as it was told, so a wrong id — a mis-sorted list, an archive copy, one
    of Michael's own files — was an unrecoverable delete with nothing between
    the mistake and the data.

    Called without a name it still works, for the connector-fallback path
    where only an id is in hand, but it reports `guard: "bypassed-no-name"`
    rather than passing silently: an unchecked destructive call should be
    visible in the run's own output.
    """
    if name is not None and not trashable_name(name):
        raise ValueError(
            "\u00a71.2 forbids trashing %r \u2014 not a dated state file and not a "
            "legacy backup. If this is genuinely old-state cleanup the name is "
            "wrong; if it is anything else the call is wrong." % name)
    _, drive = _services(root)
    drive.files().update(fileId=file_id, body={"trashed": True}).execute()
    return {"trashed": True, "name": name,
            "guard": "enforced" if name is not None else "bypassed-no-name"}


# --- one-time authorization --------------------------------------------------

def authorize(root=""):
    """Interactive consent. Run by hand, never by the scheduled task.

    Stamps `consent_at` so `health()` can track the 7-day Testing deadline.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow
    client, tok = _paths(root)
    flow = InstalledAppFlow.from_client_secrets_file(client, list(SCOPES))
    # access_type=offline is what yields a refresh token at all; prompt=consent
    # forces Google to re-issue one even if this account already granted these
    # scopes to this client before, which is the usual reason a re-authorize
    # silently produces a token that dies in an hour.
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
        open_browser=False,
    )
    data = json.loads(creds.to_json())
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["consent_at"] = stamp
    data["last_refresh"] = stamp
    with _private_open(tok) as fh:
        json.dump(data, fh)
    if not data.get("refresh_token"):
        # Without `access_type=offline` there is no refresh token and the
        # unattended run dies in an hour. Say so now, not at 7:00 AM.
        return {"wrote": tok, "scopes": list(SCOPES), "consent_at": stamp,
                "WARNING": "no refresh_token was issued — re-run consent; an "
                           "unattended run needs offline access"}
    return {"wrote": tok, "scopes": list(SCOPES), "consent_at": stamp}


# --- Phase 5 addition: send_daily_briefing_email() ---------------------------
# EXECUTION_PLAN_reviewed.md Phase 5 replaces the interactive pipeline's Gmail
# CONNECTOR send (PROJECT_INSTRUCTIONS.md 1.3, 1.9's delivery-email row) with a
# hardcoded function, because the new orchestrator has no agent harness at
# runtime to make that connector call. Additive only: SCOPES above, and every
# function that reads it, are UNCHANGED. Nothing in the current interactive
# pipeline imports or calls anything below this line.
#
# BLOCKED on EXECUTION_PLAN_reviewed.md 6 item 2: "Verify, don't assume,
# gmail.send scope." token.json was deliberately issued WITHOUT a send scope
# (see the SCOPES comment above -- an unattended credential that can send mail
# is a larger risk than the tokens it saves). Sending therefore needs a
# SEPARATE, wider-scoped credential, granted by Michael running
# `python google_api.py --authorize-send` once, interactively -- a scheduled
# task cannot complete an OAuth consent screen. This does NOT touch or widen
# what the existing token.json can do; `_send_services()` below loads
# credentials under SEND_SCOPES independently of `_services()`.

SEND_SCOPES = SCOPES + ("https://www.googleapis.com/auth/gmail.send",)
SEND_TOKEN_FILE = "send_token.json"  # separate file: never widen token.json itself

BRIEFING_RECIPIENT = "student@terpmail.umd.edu"  # 1.3 -- hardcoded, never a parameter


def send_scope_ok(root=""):
    """True if send_token.json already carries gmail.send. Never raises."""
    _, tok = _paths(root)
    tok = os.path.join(root, SEND_TOKEN_FILE)
    if not os.path.exists(tok):
        return False
    try:
        with open(tok, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    granted = set(data.get("scopes") or [])
    return "https://www.googleapis.com/auth/gmail.send" in granted


def authorize_send(root=""):
    """Interactive consent for the SEPARATE send-capable credential.

    Run by hand, never by the scheduled task -- identical reasoning to
    `authorize()` above. Writes SEND_TOKEN_FILE, not TOKEN_FILE, so a run
    using `_services()` (SCOPES) is completely unaffected by this ever having
    been called.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow
    client, _ = _paths(root)
    tok = os.path.join(root, SEND_TOKEN_FILE)
    flow = InstalledAppFlow.from_client_secrets_file(client, list(SEND_SCOPES))
    creds = flow.run_local_server(port=8080,
                                  prompt="consent", open_browser=False)
    data = json.loads(creds.to_json())
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["consent_at"] = stamp
    data["last_refresh"] = stamp
    with _private_open(tok) as fh:
        json.dump(data, fh)
    if not data.get("refresh_token"):
        return {"wrote": tok, "scopes": list(SEND_SCOPES), "consent_at": stamp,
                "WARNING": "no refresh_token was issued -- re-run consent"}
    return {"wrote": tok, "scopes": list(SEND_SCOPES), "consent_at": stamp}


def _send_gmail_service(root=""):
    """Build a Gmail service under SEND_SCOPES. Mirrors `_services()`'s
    refresh handling but reads/writes SEND_TOKEN_FILE, never TOKEN_FILE."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    tok = os.path.join(root, SEND_TOKEN_FILE)
    if not os.path.exists(tok):
        raise AuthError("missing_token",
                        "%s not found; run "
                        "`python google_api.py --authorize-send`" % tok)
    creds = Credentials.from_authorized_user_file(tok, list(SEND_SCOPES))
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:
                raise AuthError("refresh_failed", classify_refresh_error(exc))
            data = json.loads(creds.to_json())
            data["last_refresh"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            with _private_open(tok) as fh:
                json.dump(data, fh)
        else:
            raise AuthError("missing_token", "no usable refresh token in %s"
                            % tok)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _last_sent_marker(root):
    """Guard file for the once-per-calendar-day rule (1.3), independent of
    `state.last_completed_date` -- SCHEMA_AND_STATE.md 3.6 explains why a
    single marker written after delivery is not enough on its own: a crash
    between send and state-write must still be recognized as "already sent"
    on the next invocation the same day."""
    return os.path.join(root, ".last_briefing_email_sent")


def send_daily_briefing_email(summary_line, page_url, today_iso, root=""):
    """The ONLY function permitted to send the daily briefing email.

    Every guarantee PROJECT_INSTRUCTIONS.md 1.3 asks a run to uphold is
    structural here rather than requested in a prompt: the recipient is the
    module constant above, never a parameter; the subject is derived only
    from `today_iso`; the body is exactly the summary line, a blank line,
    then the bare URL; and a second call on the same date is refused before
    it can touch the network.

    Raises RuntimeError if the send-capable token doesn't exist yet -- this
    must fail loudly, not silently degrade to "no email sent", so a missing
    scope reads as a configuration gap (5.7 exception 2) rather than a
    permanent, quiet non-delivery a run has no way to surface.
    """
    if not send_scope_ok(root):
        raise RuntimeError(
            "no send-capable credential -- run "
            "`python google_api.py --authorize-send` once, interactively, "
            "before this function can be used. See "
            "EXECUTION_PLAN_reviewed.md 6 item 2.")

    marker = _last_sent_marker(root)
    if os.path.exists(marker):
        with open(marker, "r", encoding="utf-8") as fh:
            if fh.read().strip() == today_iso:
                return {"sent": False,
                        "reason": "already sent today (%s)" % today_iso}

    import base64
    from email.mime.text import MIMEText

    d = datetime.strptime(today_iso, "%Y-%m-%d")
    subject = "Daily Briefing — %s, %s %d" % (
        d.strftime("%A"), d.strftime("%b"), d.day)
    body = "%s\n\n%s\n" % (summary_line, page_url)

    msg = MIMEText(body)
    msg["to"] = BRIEFING_RECIPIENT
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

    gmail = _send_gmail_service(root)
    gmail.users().messages().send(userId="me", body={"raw": raw}).execute()

    with open(marker, "w", encoding="utf-8") as fh:
        fh.write(today_iso)
    return {"sent": True, "to": BRIEFING_RECIPIENT, "subject": subject}


if __name__ == "__main__":
    import sys
    if "--authorize" in sys.argv:
        print(json.dumps(authorize(), indent=2))
    elif "--authorize-send" in sys.argv:
        print(json.dumps(authorize_send(), indent=2))
    else:
        print(json.dumps(health(), indent=2))
