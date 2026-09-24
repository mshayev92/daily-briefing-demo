# Daily Briefing (public snapshot)

A personal pipeline that reads my coursework, email, calendar and campus events every morning and turns them into one prioritized page that I read on my phone.

> **About this copy.** This is a code-only public snapshot of a private repository. The planning and spec documents are left out because they describe my own schedule and preferences. Personal details in the code are replaced with placeholders: my course codes (`ECON001`, `MATH001`, ...), Canvas IDs, email address and server hostname. No real data, credentials or tokens are included.

## How it runs

- A **systemd timer** (`service/systemd/`) starts `service/run_daily.sh` at 06:30 every day. The script refreshes Canvas through a separate scraper, runs `service/orchestrator.py --live`, then backs the result up to Google Drive.
- **Collection** (`collect.py`, `google_api.py`, `schedule.py`, `umd_calendar.py`) pulls from Gmail, Google Calendar, Drive and the public UMD events calendar.
- **Lifecycle** (`lifecycle.py`, `ledger.py`) tracks each item from first sighting to resolution, so the brief says what changed instead of repeating the same list every day.
- **LLM calls** go through `service/llm_cli.py`, which calls the Claude CLI headlessly with an explicit tool allow-list per call. Every call is logged with its token cost, and each run has a budget. Most sections use deterministic templates, not generated text.
- **Serving**: a FastAPI app (`service/app.py`) renders the page from SQLite state (`service/schema.sql`) and serves it privately over Tailscale.

## Tests

```sh
python -m pytest -q     # 406 pass; 6 skip because they check the private spec docs
```

## Stack

Python · FastAPI · SQLite · Google APIs (Gmail, Calendar, Drive) · systemd · Claude CLI
