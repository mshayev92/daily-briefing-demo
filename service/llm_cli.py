"""claude -p subprocess wrapper -- Option B for the orchestrator's LLM calls.

Michael's decision (2026-09-16): no separate Anthropic API key, no rethinking
which steps need a model. The orchestrator shells out to the already-
authenticated `claude` CLI on this box, headlessly, once per call, for
STEP 1-2 (Gmail candidate extraction), 5c (discovery interpretation) and 7
(the TLDR line -- the one genuinely generative piece of compose; see
orchestrator.py's docstring for why DETAIL/EXPAND_CONTEXT are NOT routed
through this even though STEP 7 as a whole is "generative" in the prompt's
own words).

Confirmed empirically against the installed CLI before wiring this in:

  - `--output-format json` returns one JSON object on stdout with a `result`
    field (text) plus cost/usage metadata (`total_cost_usd`, `is_error`,
    `permission_denials`, `subtype`).
  - `--json-schema <schema>` additionally returns `structured_output`,
    already parsed against that schema -- no markdown-fence stripping or
    manual json.loads() needed on our side.
  - `--tools ""` disables every built-in tool. Combined with `--restricted`
    (also ignores project/user/local settings files), `--strict-mcp-config`
    (no MCP servers), and `--permission-mode manual --permission-prompts
    none` (anything that would still need a prompt is auto-DENIED, never
    auto-approved), a call made through `call()` CANNOT invoke Gmail,
    Drive, Bash, or any other tool -- PROJECT_INSTRUCTIONS.md §1.1's
    boundary ("This boundary applies identically to every subagent") is
    enforced structurally here, not by asking nicely in the prompt.
  - `call_with_tools()` is the ONE deliberate exception, for STEP 5c only:
    DISCOVERY.md's whole mechanism is search-then-fetch
    (`collect.py` "never fetches... the run performs the web_search and
    WebFetch calls" -- §13.5), and under this architecture the `claude -p`
    subprocess IS "the run" for that one step. It grants ONLY
    WebSearch/WebFetch, never Gmail/Drive/Bash -- the never-call list
    (§1.1) is unaffected because those tools were never on it; they were
    always meant to be used here, just by a different caller.
"""

import json
import os
import subprocess
import threading
import time

DEFAULT_TIMEOUT = 180
DEFAULT_MODEL = "haiku"       # cheap, appropriate for extraction/classification
DEFAULT_BUDGET_USD = "0.50"   # per call -- STEP-level batching keeps call count low
# Extended thinking is OFF unless a call site asks for it. Measured
# 2026-09-22: the one-sentence TLDR call spent 4,455 thinking tokens, 42 s
# and $0.024; with thinking off, 0 tokens, 2 s and $0.001, same sentence.
DEFAULT_THINKING_TOKENS = 0

# Per-process accounting. Every call's envelope reports its own cost and
# token usage; this keeps them so the orchestrator can store what a run
# actually spent (run_stats used to have no idea) and so a run can stop
# spending once it reaches `RUN_BUDGET_USD`. The lock is there because the
# Gmail extraction batches run in a small thread pool.
CALLS = []
RUN_BUDGET_USD = None         # None = no cap; the orchestrator sets one per run
_LOCK = threading.Lock()


class ClaudeCliError(Exception):
    def __init__(self, message, envelope=None):
        super().__init__(message)
        self.envelope = envelope


class BudgetExceeded(ClaudeCliError):
    """The run's LLM budget is used up. Callers treat it like any other
    failed call: their deterministic fallback ships instead."""


def spent_usd():
    with _LOCK:
        return sum(c.get("cost_usd") or 0.0 for c in CALLS)


def reset_accounting(budget_usd=None):
    global RUN_BUDGET_USD
    with _LOCK:
        del CALLS[:]
        RUN_BUDGET_USD = budget_usd


def _check_budget():
    if RUN_BUDGET_USD is not None and spent_usd() >= RUN_BUDGET_USD:
        raise BudgetExceeded(
            "run LLM budget of $%.2f reached ($%.2f spent) -- call skipped"
            % (RUN_BUDGET_USD, spent_usd()))


def _record(label, model, envelope, started, ok):
    usage = (envelope or {}).get("usage") or {}
    with _LOCK:
        CALLS.append({
            "label": label or "unlabelled", "model": model, "ok": ok,
            "cost_usd": float((envelope or {}).get("total_cost_usd") or 0.0),
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
            "cache_write_tokens": int(
                usage.get("cache_creation_input_tokens") or 0),
            "seconds": round(time.time() - started, 1),
        })


def _base_cmd(prompt, system_prompt, model, max_budget_usd):
    return [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--strict-mcp-config",
        "--permission-mode", "manual",
        "--permission-prompts", "none",
        "--max-budget-usd", str(max_budget_usd),
        "--model", model,
        "--system-prompt", system_prompt,
    ]


def _run(cmd, timeout, json_schema, allow_tool_use, label=None,
         thinking_tokens=DEFAULT_THINKING_TOKENS):
    _check_budget()
    started = time.time()
    model = cmd[cmd.index("--model") + 1]
    envelope = None
    try:
        result = _run_once(cmd, timeout, json_schema, allow_tool_use,
                           thinking_tokens)
        envelope = result[1]
        return result
    except ClaudeCliError as exc:
        envelope = exc.envelope
        raise
    finally:
        _record(label, model, envelope, started,
                ok=envelope is not None and not envelope.get("is_error"))


def _run_once(cmd, timeout, json_schema, allow_tool_use, thinking_tokens=None):
    env = None
    if thinking_tokens is not None:
        env = dict(os.environ, MAX_THINKING_TOKENS=str(int(thinking_tokens)))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        raise ClaudeCliError("claude -p timed out after %ss" % timeout) from exc

    if proc.returncode != 0:
        raise ClaudeCliError(
            "claude -p exited %d: %s" % (proc.returncode, proc.stderr[-2000:]))

    try:
        envelope = json.loads(proc.stdout)
    except ValueError as exc:
        raise ClaudeCliError(
            "claude -p did not return parseable JSON: %s" % proc.stdout[:500]
        ) from exc

    if envelope.get("is_error"):
        raise ClaudeCliError(
            "claude -p reported an error: %s" % envelope.get("result"),
            envelope)

    denials = envelope.get("permission_denials") or []
    if denials and not allow_tool_use:
        # Should be structurally impossible with --tools "" below; treat it
        # as a hard failure rather than silently continuing if it ever isn't.
        raise ClaudeCliError(
            "claude -p attempted a denied tool call: %r -- this call site's "
            "tool boundary was supposed to make that impossible" % denials,
            envelope)

    if json_schema is not None:
        if "structured_output" not in envelope:
            raise ClaudeCliError(
                "claude -p did not return structured_output despite a "
                "--json-schema", envelope)
        return envelope["structured_output"], envelope

    return envelope.get("result"), envelope


def call(prompt, *, system_prompt, json_schema=None, model=DEFAULT_MODEL,
         timeout=DEFAULT_TIMEOUT, max_budget_usd=DEFAULT_BUDGET_USD,
         label=None, thinking_tokens=DEFAULT_THINKING_TOKENS):
    """One tool-free `claude -p` call. Returns (result, envelope).

    `result` is `structured_output` (already parsed against `json_schema`)
    when a schema was given, else the raw text `result` string.

    `system_prompt` is required, not optional with a default: every call
    site must state in one place what this specific call is and is not
    allowed to do (PROJECT_INSTRUCTIONS.md §1.5/§1.6's grounding rules,
    the same discipline §6.5 requires of the on-demand AI prompts).
    """
    cmd = _base_cmd(prompt, system_prompt, model, max_budget_usd)
    cmd += ["--tools", "", "--restricted"]
    if json_schema is not None:
        cmd += ["--json-schema", json.dumps(json_schema)]
    return _run(cmd, timeout, json_schema, allow_tool_use=False, label=label,
                thinking_tokens=thinking_tokens)


def call_with_tools(prompt, *, system_prompt, tools, json_schema=None,
                    model=DEFAULT_MODEL, timeout=DEFAULT_TIMEOUT,
                    max_budget_usd=DEFAULT_BUDGET_USD, label=None,
                    thinking_tokens=None):
    """STEP 5c ONLY. `tools` must be an explicit allow-list (e.g.
    "WebSearch,WebFetch") -- never "default", so a call site can never
    accidentally inherit tool access wider than it asked for.
    """
    if not tools or tools == "default":
        raise ValueError(
            "call_with_tools() requires an explicit tool list, never "
            "'default' -- name exactly what §13.5 needs (WebSearch/WebFetch)")
    cmd = _base_cmd(prompt, system_prompt, model, max_budget_usd)
    cmd += ["--tools", tools, "--restricted", "--allowedTools", tools]
    if json_schema is not None:
        cmd += ["--json-schema", json.dumps(json_schema)]
    return _run(cmd, timeout, json_schema, allow_tool_use=True, label=label,
                thinking_tokens=thinking_tokens)
