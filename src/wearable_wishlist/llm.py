"""LLM backend: `complete_json` and `complete_text` over three interchangeable backends.

The backend is chosen by `config.llm_backend()`:
  claude_cli  Claude Code CLI run non-interactively. Uses the user's claude.ai subscription: no API key, nothing billed.
              One turn, no tools, no MCP servers, no session on disk, our own system prompt (not the coding-assistant one).
  anthropic   Official SDK (`anthropic`). Needs ANTHROPIC_API_KEY (or an `ant auth login` profile). Token usage is priced
              with costs.ANTHROPIC_PRICES, guarded with costs.guard and written to the costs ledger.
  fixture     Canned output, no network (DRY_RUN=1 forces this): data/fixtures/llm_classification.json for classification
              schemas (any schema mentioning `category`), {} / "" otherwise.

Both public functions keep the contract signature and accept two optional keywords, `conn=None, run_id=None`: when `conn`
is given, usage is recorded with `costs.record(provider="anthropic", ...)`. CLI usage is booked at cost 0 (subscription)
with the CLI's own list-price estimate in the note; set LLM_CLI_RECORD_ESTIMATE=1 to book that estimate as the cost.

`complete_json` validates that every key in `schema["required"]` is present and retries once with a repair prompt
(previous output + error) when the answer is not a JSON object or a key is missing; then it raises `LLMError`.

Optional env knobs: LLM_BACKEND, LLM_MODEL (an alias such as `sonnet` or a full id), LLM_EFFORT (low|medium|high|xhigh|max),
CLAUDE_CLI_PATH (default: `claude` on PATH, else ~/.local/bin/claude), LLM_CLI_TIMEOUT_S (default 120),
LLM_CLI_INHERIT_AUTH=1 (keep ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL in the CLI's environment; by
default they are dropped so the CLI uses the claude.ai login instead of billing an API key found in .env).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config, costs

log = logging.getLogger("wearable_wishlist.llm")

PROVIDER = "anthropic"                    # costs.provider for both real backends (same vendor, same budget)
DEFAULT_API_MODEL = "claude-sonnet-5-5"   # anthropic backend default (contract)
DEFAULT_CLI_MODEL = "sonnet"              # claude_cli default: same family, cheapest on the subscription quota
MODEL_ALIASES = {"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5", "haiku": "claude-haiku-4-5", "fable": "claude-fable-5-1"}
DEFAULT_SYSTEM = ("You are a careful research analyst. Follow the instructions exactly, never invent facts, "
                  "and say when something is unknown.")
JSON_SYSTEM_SUFFIX = "You answer with a single valid JSON object and nothing else: no prose, no markdown code fences."
CLI_TIMEOUT_S = 120.0
_REPAIR_MAX_CHARS = 6000


class LLMError(RuntimeError):
    """The model did not return a usable answer: transport failure, refusal, or invalid JSON after one repair attempt."""


@dataclass
class LLMResult:
    """One backend call. `text` is the model's answer; the rest is bookkeeping."""
    text: str
    stop_reason: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0


# ----------------------------------------------------------------------------------------------------------------------
# Public API (signatures are the contract; conn/run_id are optional extras)
# ----------------------------------------------------------------------------------------------------------------------

def complete_json(prompt: str, schema: dict, *, system: str | None = None, model: str | None = None,
                  max_tokens: int = 2000, conn=None, run_id: int | None = None) -> dict:
    """Ask for one JSON object satisfying `schema` (JSON-schema-like dict; only top-level `required` is enforced).
    Retries once with a repair prompt when the output is not a JSON object or misses a required key, then raises LLMError."""
    if _use_fixture():
        return _fixture_json(schema)
    schema = schema or {}
    sys_prompt = f"{system or DEFAULT_SYSTEM}\n\n{JSON_SYSTEM_SUFFIX}"
    first = _call(_json_prompt(prompt, schema), system=sys_prompt, model=model, max_tokens=max_tokens, conn=conn, run_id=run_id)
    try:
        return validate_required(extract_json(first.text), schema)
    except ValueError as err1:
        problem = _describe_problem(first, err1)
        log.warning("LLM output rejected (%s); retrying once with a repair prompt", problem)
        retry_tokens = max_tokens * 2 if first.stop_reason == "max_tokens" else max_tokens
        second = _call(_repair_prompt(prompt, schema, first.text, problem), system=sys_prompt, model=model,
                       max_tokens=retry_tokens, conn=conn, run_id=run_id)
        try:
            return validate_required(extract_json(second.text), schema)
        except ValueError as err2:
            raise LLMError(
                f"LLM did not return a valid JSON object for the schema after one repair attempt: "
                f"{_describe_problem(second, err2)}. Backend {config.llm_backend()!r}, model {second.model or model or '(default)'}, "
                f"required keys {list(schema.get('required') or [])}. Last output: {_excerpt(second.text)!r}") from err2


def complete_text(prompt: str, *, system: str | None = None, model: str | None = None, max_tokens: int = 1000,
                  conn=None, run_id: int | None = None) -> str:
    """Free-text completion (one user message). Returns "" on the fixture backend."""
    if _use_fixture():
        return ""
    res = _call(prompt, system=system or DEFAULT_SYSTEM, model=model, max_tokens=max_tokens, conn=conn, run_id=run_id)
    return res.text.strip()


# ----------------------------------------------------------------------------------------------------------------------
# JSON extraction and validation (pure functions, unit-tested offline)
# ----------------------------------------------------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*[ \t]*\r?\n?(.*?)```", re.S)


def extract_json(text: str) -> dict:
    """Return the JSON object contained in `text`. Tries, in order: the whole text, each fenced ``` block, the outermost
    {...} span after stripping fence markers, and finally the first decodable {...} anywhere. Raises ValueError otherwise."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty output")
    # A well-formed answer (whole text or a fenced block) is authoritative: if it is a list/scalar, say so instead of
    # fishing an object out of it, so the repair prompt can ask for the right shape.
    for cand in [text.strip()] + [m.group(1).strip() for m in _FENCE_RE.finditer(text)]:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
        raise ValueError(f"expected a JSON object, got a JSON {type(obj).__name__}")
    # Noisy prose around the object: outermost {...} span (fence markers stripped), then the first decodable object.
    unfenced = _FENCE_RE.sub(lambda m: m.group(1), text)
    start, end = unfenced.find("{"), unfenced.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(unfenced[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    decoder = json.JSONDecoder()
    for i, ch in enumerate(unfenced):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(unfenced, i)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("no JSON object found in the output")


def validate_required(data: Any, schema: dict) -> dict:
    """Check that `data` is a dict holding every key in schema['required']; returns it. Raises ValueError listing what is missing."""
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object, got {type(data).__name__}")
    required = list((schema or {}).get("required") or [])
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"missing required keys: {', '.join(missing)}")
    return data


def _json_prompt(prompt: str, schema: dict) -> str:
    required = list(schema.get("required") or [])
    req = f" Required keys: {', '.join(required)}." if required else ""
    return "\n".join([
        prompt.rstrip(), "", "--- Output format ---",
        "Respond with ONE JSON object and nothing else: no prose before or after it, no markdown code fences.",
        f"The object must satisfy this JSON schema.{req} Use null for anything unknown; never invent evidence.",
        json.dumps(schema, ensure_ascii=False, default=str),
    ])


def _repair_prompt(prompt: str, schema: dict, previous: str, problem: str) -> str:
    return "\n".join([
        f"Your previous answer to the request below could not be used: {problem}.",
        "Previous answer (fix it; do not repeat its mistakes):", "<<<", _excerpt(previous, _REPAIR_MAX_CHARS), ">>>",
        "Answer the request again, correctly this time.", "", "=== Request ===", _json_prompt(prompt, schema),
    ])


def _describe_problem(result: LLMResult, err: Exception) -> str:
    msg = str(err)
    if result.stop_reason == "max_tokens":
        msg += " (the output was cut off by max_tokens; answer more concisely)"
    return msg


def _excerpt(text: str, n: int = 500) -> str:
    text = text or ""
    return text if len(text) <= n else text[:n] + f"... [{len(text) - n} more chars]"


# ----------------------------------------------------------------------------------------------------------------------
# Backend dispatch
# ----------------------------------------------------------------------------------------------------------------------

def _use_fixture() -> bool:
    return config.dry_run() or config.llm_backend() == "fixture"


def _fixture_json(schema: dict | None) -> dict:
    if "category" in json.dumps(schema or {}, default=str):
        return json.loads((config.FIXTURES_DIR / "llm_classification.json").read_text())
    return {}


def _call(prompt: str, *, system: str, model: str | None, max_tokens: int, conn, run_id: int | None) -> LLMResult:
    """One single-turn call on the configured backend. Tests monkeypatch this to exercise the retry logic offline."""
    backend = config.llm_backend()
    if backend == "claude_cli":
        return _call_cli(prompt, system=system, model=model, max_tokens=max_tokens, conn=conn, run_id=run_id)
    if backend == "anthropic":
        return _call_anthropic(prompt, system=system, model=model, max_tokens=max_tokens, conn=conn, run_id=run_id)
    if backend == "fixture":
        return LLMResult(text="", model="fixture")
    raise LLMError(f"unknown LLM_BACKEND {backend!r}: use claude_cli | anthropic | fixture")


# ----------------------------------------------------------------------------------------------------------------------
# Backend: Claude Code CLI (subscription, free)
# ----------------------------------------------------------------------------------------------------------------------

def cli_path() -> str:
    return config.env("CLAUDE_CLI_PATH") or shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")


def _cli_env() -> dict[str, str]:
    """Environment for the nested CLI. The CLI is Claude Code itself, so drop every marker of the session we may be
    running inside (CLAUDECODE, CLAUDE_CODE_SESSION_ID, messaging sockets, ...): with them the child can behave as an
    attached/nested session instead of a plain one-shot print call. CLAUDE_CONFIG_DIR is kept (it locates the login).
    API-key auth is dropped too (unless LLM_CLI_INHERIT_AUTH=1) so the call uses the claude.ai login, not a key from .env."""
    env = {k: v for k, v in os.environ.items() if not (k.startswith("CLAUDE") and k != "CLAUDE_CONFIG_DIR")}
    if config.env("LLM_CLI_INHERIT_AUTH", "0") != "1":
        for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
            env.pop(k, None)
    env.setdefault("HOME", str(Path.home()))
    return env


def _cli_argv(system: str, model: str | None) -> list[str]:
    """Flags verified against `claude --help` (Claude Code 2.1.284):
    -p (print, non-interactive) --output-format json (one JSON envelope on stdout) --max-turns 1 (single turn)
    --tools "" (no built-in tools) --no-session-persistence (nothing written to disk) --strict-mcp-config (no MCP servers)
    --disable-slash-commands (no skills in context) --permission-prompts none (nothing can block on a prompt)
    --system-prompt (replaces Claude Code's own system prompt) --model (alias like sonnet/opus or a full id) [--effort].
    The user prompt goes on stdin."""
    argv = [cli_path(), "-p", "--output-format", "json", "--max-turns", "1", "--tools", "", "--no-session-persistence",
            "--strict-mcp-config", "--disable-slash-commands", "--permission-prompts", "none", "--system-prompt", system]
    if model:
        argv += ["--model", model]
    effort = config.env("LLM_EFFORT")
    if effort:
        argv += ["--effort", effort]
    if config.env("LLM_CLI_INHERIT_AUTH", "0") != "1":
        # An `env` block in ~/.claude/settings.json (ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL) is applied inside the CLI, after
        # _cli_env has dropped the variables, and wins over the claude.ai login: on 30 Sep a rejected key there made every
        # call retry until the 120 s timeout. Skipping user settings keeps this one-shot call on the login.
        argv += ["--setting-sources", "project,local"]
    return argv


def _call_cli(prompt: str, *, system: str, model: str | None, max_tokens: int, conn, run_id: int | None) -> LLMResult:
    model = model or config.llm_model() or DEFAULT_CLI_MODEL
    argv = _cli_argv(system, model)
    timeout = float(config.env("LLM_CLI_TIMEOUT_S", str(CLI_TIMEOUT_S)))
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(argv, input=prompt, capture_output=True, text=True, env=_cli_env(),
                              cwd=tempfile.gettempdir(), timeout=timeout)
    except FileNotFoundError as e:
        raise LLMError(f"claude CLI not found at {argv[0]!r}: install Claude Code, set CLAUDE_CLI_PATH, "
                       f"or use LLM_BACKEND=anthropic") from e
    except subprocess.TimeoutExpired as e:
        raise LLMError(f"claude CLI timed out after {timeout:.0f}s (model {model}, prompt {len(prompt)} chars)") from e
    duration = time.perf_counter() - t0
    envelope = _parse_cli_envelope(proc)
    text = envelope.get("result")
    if not isinstance(text, str):
        text = "" if text is None else json.dumps(text)
    usage = envelope.get("usage") or {}
    in_tokens = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    out_tokens = int(usage.get("output_tokens") or 0)
    estimate = float(envelope.get("total_cost_usd") or 0.0)
    canonical = next(iter((envelope.get("modelUsage") or {}).keys()), None) or model
    result = LLMResult(text=text, stop_reason=envelope.get("stop_reason"), model=canonical, input_tokens=in_tokens,
                       output_tokens=out_tokens, cost_usd=0.0, duration_s=duration)
    log.debug("claude_cli %s: %d in / %d out tokens, %.1fs, list-price estimate %s", canonical, in_tokens, out_tokens,
              duration, costs.fmt(estimate))
    if conn is not None:
        book = config.env("LLM_CLI_RECORD_ESTIMATE", "0") == "1"
        units = in_tokens + out_tokens
        unit_cost = (estimate / units) if (book and units) else 0.0
        note = (f"claude_cli {canonical} in={in_tokens} out={out_tokens} {duration:.1f}s; "
                + ("CLI list-price estimate booked as cost" if book
                   else f"subscription, not billed; CLI list-price estimate {costs.fmt(estimate)}"))
        result.cost_usd = costs.record(conn, PROVIDER, f"claude_cli/{canonical}", units, unit_cost, run_id, note)
    return result


def _parse_cli_envelope(proc: subprocess.CompletedProcess) -> dict:
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    envelope: Any = None
    if stdout:
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError:
            for line in reversed(stdout.splitlines()):   # warnings may precede the JSON line
                line = line.strip()
                if line.startswith("{"):
                    try:
                        envelope = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue
    if not isinstance(envelope, dict):
        raise LLMError(f"claude CLI (rc={proc.returncode}) did not print a JSON envelope: {_excerpt(stdout or stderr, 400)!r}")
    if proc.returncode != 0 or envelope.get("is_error"):
        why = envelope.get("terminal_reason") or envelope.get("subtype") or "error"
        raise LLMError(f"claude CLI failed (rc={proc.returncode}, {why}): "
                       f"{_excerpt(str(envelope.get('result') or stderr or ''), 500)}")
    return envelope


# ----------------------------------------------------------------------------------------------------------------------
# Backend: Anthropic SDK (paid)
# ----------------------------------------------------------------------------------------------------------------------

_client = None


def _anthropic_client():
    """Lazily built, cached `anthropic.Anthropic()` (credentials from ANTHROPIC_API_KEY or an `ant auth login` profile).
    Tests monkeypatch this function to inject a fake client."""
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def _resolve_api_model(model: str | None) -> str:
    m = model or config.llm_model() or DEFAULT_API_MODEL
    return MODEL_ALIASES.get(m.lower(), m)


def _prices(model: str) -> tuple[tuple[float, float], bool]:
    """(input, output) USD per 1M tokens from costs.ANTHROPIC_PRICES; falls back to the default model's rate (flagged)."""
    table = costs.ANTHROPIC_PRICES
    if model in table:
        return table[model], True
    for key, price in table.items():
        if model.startswith(key) or key.startswith(model):
            return price, True
    return table.get(DEFAULT_API_MODEL, (3.0, 15.0)), False


def _call_anthropic(prompt: str, *, system: str, model: str | None, max_tokens: int, conn, run_id: int | None) -> LLMResult:
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover
        raise LLMError("the `anthropic` package is not installed (pip install anthropic), or use LLM_BACKEND=claude_cli") from e
    model = _resolve_api_model(model)
    (pin, pout), known = _prices(model)
    if conn is not None:
        est_in = (len(prompt) + len(system or "")) / 4 + 50           # rough chars -> tokens, before spending
        projected = (est_in * pin + max_tokens * pout) / 1e6
        costs.guard(conn, PROVIDER, projected, run_id)
    kwargs: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
    if system:
        kwargs["system"] = system
    effort = config.env("LLM_EFFORT")
    if effort:
        kwargs["output_config"] = {"effort": effort}
    t0 = time.perf_counter()
    try:
        resp = _anthropic_client().messages.create(**kwargs)
    except anthropic.AuthenticationError as e:
        raise LLMError("Anthropic API authentication failed: set ANTHROPIC_API_KEY in .env (or run `ant auth login`), "
                       "or use LLM_BACKEND=claude_cli") from e
    except anthropic.RateLimitError as e:
        raise LLMError(f"Anthropic API rate limited (retry-after {e.response.headers.get('retry-after', '?')}s): {e.message}") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"Anthropic API error {e.status_code} for model {model}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError(f"Anthropic API connection error: {e}") from e
    duration = time.perf_counter() - t0
    if getattr(resp, "stop_reason", None) == "refusal":
        details = getattr(resp, "stop_details", None)
        raise LLMError(f"Anthropic API refused the request (category {getattr(details, 'category', None)}): "
                       f"{getattr(details, 'explanation', '') or ''}")
    text = "".join(getattr(b, "text", "") for b in (resp.content or []) if getattr(b, "type", None) == "text")
    u = getattr(resp, "usage", None)
    in_tokens = sum(int(getattr(u, k, 0) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    out_tokens = int(getattr(u, "output_tokens", 0) or 0)
    result = LLMResult(text=text, stop_reason=getattr(resp, "stop_reason", None), model=getattr(resp, "model", None) or model,
                       input_tokens=in_tokens, output_tokens=out_tokens, duration_s=duration)
    log.debug("anthropic %s: %d in / %d out tokens, %.1fs", model, in_tokens, out_tokens, duration)
    if conn is not None:
        note = f"anthropic sdk {duration:.1f}s" + ("" if known else f"; no price for {model}, {DEFAULT_API_MODEL} rate assumed")
        result.cost_usd = (costs.record(conn, PROVIDER, f"messages/{model}", in_tokens, pin / 1e6, run_id, note + "; input tokens")
                           + costs.record(conn, PROVIDER, f"messages/{model}", out_tokens, pout / 1e6, run_id, note + "; output tokens"))
    else:
        result.cost_usd = (in_tokens * pin + out_tokens * pout) / 1e6
    return result
