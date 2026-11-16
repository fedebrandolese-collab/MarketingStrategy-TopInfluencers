"""Workstream H: LLM backend. Offline by default (conftest forces DRY_RUN=1); the live CLI check needs LIVE_LLM=1.
No test calls the Anthropic API: the SDK client is replaced by a fake."""
import json
import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from wearable_wishlist import config, costs, db, llm

CLASSIFICATION_SCHEMA = {
    "type": "object",
    "required": ["category", "us_based", "summary", "unknowns"],
    "properties": {"category": {"enum": list(config.CATEGORIES)}, "us_based": {"type": ["boolean", "null"]},
                   "summary": {"type": "string"}, "unknowns": {"type": "array"}},
}
GOOD = {"category": "vc", "us_based": None, "summary": "Seed investor in wearables.", "unknowns": ["location"]}

# Envelope recorded from `claude -p --output-format json ...` (Claude Code 2.1.284), trimmed to the fields we read.
CLI_ENVELOPE = {
    "type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "stop_reason": "end_turn",
    "duration_ms": 5300, "duration_api_ms": 2083, "session_id": "798584e9-e789-4572-bb32-54800d62c941",
    "total_cost_usd": 0.000744,
    "usage": {"input_tokens": 297, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 15},
    "modelUsage": {"claude-sonnet-5-5": {"inputTokens": 297, "outputTokens": 15, "costUSD": 0.000744, "costBasis": "list"}},
    "permission_denials": [], "terminal_reason": "completed", "api_error_status": None,
    "result": '{"ok": true, "n": 3}',
}


@pytest.fixture
def real_backend(monkeypatch):
    """Leave DRY_RUN so the real code paths run; every test using it mocks the transport."""
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_EFFORT", raising=False)
    monkeypatch.delenv("LLM_CLI_RECORD_ESTIMATE", raising=False)


# ---------------------------------------------------------------------------------------------------------- fixture

def test_fixture_backend_serves_classification_and_empty_otherwise():
    assert config.llm_backend() == "fixture"          # conftest: DRY_RUN=1
    out = llm.complete_json("classify this person", CLASSIFICATION_SCHEMA)
    assert out["category"] == "x_voice" and out["wearable_user"]["value"] is True
    assert out == json.loads((config.FIXTURES_DIR / "llm_classification.json").read_text())
    assert llm.complete_json("anything", {"type": "object", "required": ["foo"]}) == {}
    assert llm.complete_json("anything", {"properties": {"category": {}}}) != {}   # mentioned in properties only
    assert llm.complete_text("write a haiku") == ""


def test_dry_run_wins_over_backend_setting(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "anthropic")   # DRY_RUN=1 still forces the fixture: no client is built
    monkeypatch.setattr(llm, "_anthropic_client", lambda: pytest.fail("must not build a client under DRY_RUN"))
    assert llm.complete_json("x", CLASSIFICATION_SCHEMA)["category"] == "x_voice"


# ------------------------------------------------------------------------------------------------- JSON extraction

@pytest.mark.parametrize("text", [
    '{"category": "vc", "n": 3}',
    '  \n{"category": "vc", "n": 3}\n\n',
    '```json\n{"category": "vc", "n": 3}\n```',
    '```\n{"category": "vc",\n "n": 3}\n```',
    'Sure! Here is the classification:\n```json\n{"category": "vc", "n": 3}\n```\nLet me know if you need more.',
    'Result: {"category": "vc", "n": 3} -- done',
    'Here you go {"category": "vc", "n": 3}',
])
def test_extract_json_plain_fenced_and_noisy(text):
    assert llm.extract_json(text) == {"category": "vc", "n": 3}


def test_extract_json_nested_and_braces_in_strings():
    text = 'Note: the {weird} bio.\n{"summary": "likes {wearables} and }braces{", "evidence": [{"url": "https://x.com/a/status/1", "quote": "hi"}]}\nThanks.'
    out = llm.extract_json(text)
    assert out["summary"] == "likes {wearables} and }braces{" and out["evidence"][0]["url"].endswith("/1")


def test_extract_json_takes_outermost_object():
    text = '{"outer": {"inner": 1}, "list": [{"a": 1}, {"b": 2}]}'
    assert llm.extract_json(text) == {"outer": {"inner": 1}, "list": [{"a": 1}, {"b": 2}]}


def test_extract_json_rejects_non_objects():
    with pytest.raises(ValueError, match="no JSON object"):
        llm.extract_json("I cannot classify this person.")
    with pytest.raises(ValueError, match="JSON list"):
        llm.extract_json('[{"category": "vc"}]')
    with pytest.raises(ValueError, match="empty"):
        llm.extract_json("   ")


def test_validate_required_lists_missing_keys():
    assert llm.validate_required(GOOD, CLASSIFICATION_SCHEMA) is GOOD
    with pytest.raises(ValueError, match="missing required keys: us_based, unknowns"):
        llm.validate_required({"category": "vc", "summary": "x"}, CLASSIFICATION_SCHEMA)
    with pytest.raises(ValueError, match="expected a JSON object"):
        llm.validate_required(["vc"], CLASSIFICATION_SCHEMA)
    assert llm.validate_required({}, {}) == {}   # no required keys -> anything goes


# ------------------------------------------------------------------------------------------------------ retry path

def test_retry_once_with_repair_prompt_then_succeeds(real_backend, monkeypatch):
    answers = ['Sure, here it is:\n```json\n{"category": "vc", "summary": "Seed investor"}\n```', json.dumps(GOOD)]
    calls = []

    def fake_call(prompt, **kw):
        calls.append((prompt, kw))
        return llm.LLMResult(text=answers[len(calls) - 1], model="fake")

    monkeypatch.setattr(llm, "_call", fake_call)
    out = llm.complete_json("Classify @vc_test", CLASSIFICATION_SCHEMA, system="Be terse.", max_tokens=777)
    assert out == GOOD
    assert len(calls) == 2
    first_prompt, first_kw = calls[0]
    assert "Classify @vc_test" in first_prompt and "ONE JSON object" in first_prompt and '"required"' in first_prompt
    assert first_kw["system"].startswith("Be terse.") and "single valid JSON object" in first_kw["system"]
    assert first_kw["max_tokens"] == 777
    repair_prompt = calls[1][0]
    assert "missing required keys: us_based, unknowns" in repair_prompt        # the error
    assert '{"category": "vc", "summary": "Seed investor"}' in repair_prompt   # the previous output
    assert "Classify @vc_test" in repair_prompt                                # the original request


def test_retry_on_invalid_json_then_give_up_with_helpful_error(real_backend, monkeypatch):
    calls = []

    def fake_call(prompt, **kw):
        calls.append(prompt)
        return llm.LLMResult(text="I am not able to produce JSON for this.", model="fake", stop_reason="end_turn")

    monkeypatch.setattr(llm, "_call", fake_call)
    with pytest.raises(llm.LLMError) as exc:
        llm.complete_json("Classify", CLASSIFICATION_SCHEMA)
    msg = str(exc.value)
    assert len(calls) == 2, "exactly one repair attempt"
    assert "after one repair attempt" in msg and "no JSON object" in msg
    assert "category" in msg and "not able to produce JSON" in msg


def test_truncated_output_retries_with_more_tokens(real_backend, monkeypatch):
    seen = []

    def fake_call(prompt, **kw):
        seen.append(kw["max_tokens"])
        if len(seen) == 1:
            return llm.LLMResult(text='{"category": "vc", "summary": "Seed inv', stop_reason="max_tokens")
        return llm.LLMResult(text=json.dumps(GOOD))

    monkeypatch.setattr(llm, "_call", fake_call)
    assert llm.complete_json("Classify", CLASSIFICATION_SCHEMA, max_tokens=500) == GOOD
    assert seen == [500, 1000]


def test_complete_text_passes_through(real_backend, monkeypatch):
    monkeypatch.setattr(llm, "_call", lambda prompt, **kw: llm.LLMResult(text="  pong \n"))
    assert llm.complete_text("ping") == "pong"


def test_unknown_backend_raises(real_backend, monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "openai")
    with pytest.raises(llm.LLMError, match="unknown LLM_BACKEND"):
        llm.complete_text("ping")


# ------------------------------------------------------------------------------------------ claude_cli (mocked run)

@pytest.fixture
def fake_cli(monkeypatch, real_backend):
    """Replace subprocess.run with a recorder returning a canned envelope; yields the list of recorded calls."""
    monkeypatch.setenv("LLM_BACKEND", "claude_cli")
    monkeypatch.setenv("CLAUDECODE", "1")                        # pretend we run nested inside Claude Code
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "[not-an-api-key]")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/claude-config")
    calls = []
    state = {"envelope": CLI_ENVELOPE, "returncode": 0, "stderr": "", "raise": None}

    def fake_run(argv, **kw):
        calls.append({"argv": argv, **kw})
        if state["raise"]:
            raise state["raise"]
        return subprocess.CompletedProcess(argv, state["returncode"], stdout=json.dumps(state["envelope"]), stderr=state["stderr"])

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    calls_state = SimpleNamespace(calls=calls, state=state)
    return calls_state


def test_cli_flags_env_and_stdin(fake_cli):
    out = llm.complete_json("Reply with {\"ok\": true, \"n\": 3}", {"type": "object", "required": ["ok", "n"]},
                            system="You are a JSON API.", model="opus")
    assert out == {"ok": True, "n": 3}
    assert len(fake_cli.calls) == 1
    call = fake_cli.calls[0]
    argv = call["argv"]
    assert argv[0] == llm.cli_path()
    for flag in (["-p"], ["--output-format", "json"], ["--max-turns", "1"], ["--tools", ""], ["--no-session-persistence"],
                 ["--strict-mcp-config"], ["--disable-slash-commands"], ["--permission-prompts", "none"], ["--model", "opus"],
                 ["--setting-sources", "project,local"]):    # user settings' env (an API key) would win over the login
        i = argv.index(flag[0])
        assert argv[i:i + len(flag)] == flag, flag
    assert argv[argv.index("--system-prompt") + 1].startswith("You are a JSON API.")
    assert "Reply with" in call["input"] and "ONE JSON object" in call["input"]     # prompt goes on stdin
    assert call["timeout"] == 120 and call["capture_output"] and call["text"]
    env = call["env"]
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_SESSION_ID" not in env and "ANTHROPIC_API_KEY" not in env
    assert env["CLAUDE_CONFIG_DIR"] == "/tmp/claude-config" and env.get("HOME")


def test_cli_default_model_and_env_overrides(fake_cli, monkeypatch):
    llm.complete_text("ping")
    argv = fake_cli.calls[-1]["argv"]
    assert argv[argv.index("--model") + 1] == "sonnet" and "--effort" not in argv
    monkeypatch.setenv("LLM_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("LLM_EFFORT", "low")
    monkeypatch.setenv("LLM_CLI_TIMEOUT_S", "30")
    monkeypatch.setenv("LLM_CLI_INHERIT_AUTH", "1")
    llm.complete_text("ping")
    call = fake_cli.calls[-1]
    assert call["argv"][call["argv"].index("--model") + 1] == "claude-opus-5-5"
    assert call["argv"][call["argv"].index("--effort") + 1] == "low"
    assert call["timeout"] == 30 and call["env"]["ANTHROPIC_API_KEY"] == "[not-an-api-key]"
    assert "--setting-sources" not in call["argv"]           # inheriting auth: user settings apply as usual


def test_cli_records_zero_cost_with_note_only_when_conn_given(fake_cli, conn, monkeypatch):
    llm.complete_text("ping")                                       # no conn -> nothing recorded
    assert db.one(conn, "SELECT COUNT(*) AS n FROM costs")["n"] == 0
    run_id = db.new_run(conn, "classify")
    llm.complete_text("ping", conn=conn, run_id=run_id)
    rows = db.rows(conn, "SELECT * FROM costs")
    assert len(rows) == 1
    r = rows[0]
    assert r["provider"] == "anthropic" and r["run_id"] == run_id and r["endpoint"] == "claude_cli/claude-sonnet-5-5"
    assert r["units"] == 297 + 15 and r["cost_usd"] == 0 and r["unit_cost_usd"] == 0
    assert "claude_cli" in r["note"] and "$0.0007" in r["note"]
    assert costs.spent(conn, "anthropic", run_id) == 0
    monkeypatch.setenv("LLM_CLI_RECORD_ESTIMATE", "1")               # opt-in: book the CLI's list-price estimate
    llm.complete_text("ping", conn=conn, run_id=run_id)
    r = db.rows(conn, "SELECT * FROM costs ORDER BY cost_id DESC LIMIT 1")[0]
    assert abs(r["cost_usd"] - 0.000744) < 1e-6 and "booked" in r["note"]


def test_cli_error_envelope_timeout_and_missing_binary(fake_cli):
    fake_cli.state["envelope"] = {**CLI_ENVELOPE, "is_error": True, "terminal_reason": "api_error", "api_error_status": 403,
                                  "result": "Failed to authenticate. API Error: 403 The requested model 'nope' is not available"}
    fake_cli.state["returncode"] = 1
    with pytest.raises(llm.LLMError, match="claude CLI failed .*api_error.*model 'nope'"):
        llm.complete_text("ping")
    fake_cli.state["returncode"] = 0
    fake_cli.state["raise"] = subprocess.TimeoutExpired(cmd="claude", timeout=120)
    with pytest.raises(llm.LLMError, match="timed out after 120s"):
        llm.complete_text("ping")
    fake_cli.state["raise"] = FileNotFoundError("claude")
    with pytest.raises(llm.LLMError, match="not found"):
        llm.complete_text("ping")


def test_cli_envelope_after_warning_lines(fake_cli, monkeypatch):
    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout="some warning line\n" + json.dumps(CLI_ENVELOPE) + "\n", stderr="")
    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    assert llm.complete_text("ping") == '{"ok": true, "n": 3}'

    def bad_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout="not json at all", stderr="")
    monkeypatch.setattr(llm.subprocess, "run", bad_run)
    with pytest.raises(llm.LLMError, match="did not print a JSON envelope"):
        llm.complete_text("ping")


# ------------------------------------------------------------------------------------------ anthropic (mocked SDK)

class FakeMessages:
    def __init__(self, text, stop_reason="end_turn", in_tokens=1200, out_tokens=300):
        self.kwargs, self.text, self.stop_reason, self.in_tokens, self.out_tokens = [], text, stop_reason, in_tokens, out_tokens

    def create(self, **kwargs):
        self.kwargs.append(kwargs)
        usage = SimpleNamespace(input_tokens=self.in_tokens, output_tokens=self.out_tokens,
                                cache_creation_input_tokens=0, cache_read_input_tokens=0)
        content = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=self.text)]
        return SimpleNamespace(content=content, stop_reason=self.stop_reason, usage=usage, model=kwargs["model"], stop_details=None)


@pytest.fixture
def fake_anthropic(monkeypatch, real_backend):
    monkeypatch.setenv("LLM_BACKEND", "anthropic")
    messages = FakeMessages(json.dumps(GOOD))
    monkeypatch.setattr(llm, "_anthropic_client", lambda: SimpleNamespace(messages=messages))
    return messages


def test_anthropic_request_shape_and_default_model(fake_anthropic):
    out = llm.complete_json("Classify @vc_test", CLASSIFICATION_SCHEMA, system="Be terse.")
    assert out == GOOD
    (req,) = fake_anthropic.kwargs
    assert req["model"] == "claude-sonnet-5-5" and req["max_tokens"] == 2000
    assert req["system"].startswith("Be terse.") and "single valid JSON object" in req["system"]
    assert req["messages"] == [{"role": "user", "content": req["messages"][0]["content"]}]
    assert "Classify @vc_test" in req["messages"][0]["content"] and "ONE JSON object" in req["messages"][0]["content"]
    assert "thinking" not in req and "output_config" not in req


def test_anthropic_model_aliases_and_effort(fake_anthropic, monkeypatch):
    llm.complete_text("ping", model="opus", max_tokens=50)
    assert fake_anthropic.kwargs[-1]["model"] == "claude-opus-5-5" and fake_anthropic.kwargs[-1]["max_tokens"] == 50
    monkeypatch.setenv("LLM_MODEL", "haiku")
    monkeypatch.setenv("LLM_EFFORT", "low")
    llm.complete_text("ping")
    assert fake_anthropic.kwargs[-1]["model"] == "claude-haiku-4-5"
    assert fake_anthropic.kwargs[-1]["output_config"] == {"effort": "low"}


def test_anthropic_records_priced_tokens_and_guards_budget(fake_anthropic, conn, monkeypatch):
    run_id = db.new_run(conn, "classify")
    llm.complete_text("ping", conn=conn, run_id=run_id)
    rows = db.rows(conn, "SELECT * FROM costs ORDER BY cost_id")
    pin, pout = costs.ANTHROPIC_PRICES["claude-sonnet-5-5"]
    assert [r["units"] for r in rows] == [1200, 300]
    assert all(r["provider"] == "anthropic" and r["endpoint"] == "messages/claude-sonnet-5-5" and r["run_id"] == run_id for r in rows)
    assert abs(rows[0]["cost_usd"] - 1200 * pin / 1e6) < 1e-9 and abs(rows[1]["cost_usd"] - 300 * pout / 1e6) < 1e-9
    assert abs(costs.spent(conn, "anthropic", run_id) - (1200 * pin + 300 * pout) / 1e6) < 1e-9
    monkeypatch.setenv("BUDGET_ANTHROPIC_USD", "0.001")                  # guard runs before the call
    n_before = len(fake_anthropic.kwargs)
    with pytest.raises(costs.BudgetExceeded):
        llm.complete_text("ping", conn=conn, run_id=run_id)
    assert len(fake_anthropic.kwargs) == n_before


def test_anthropic_refusal_raises(fake_anthropic):
    fake_anthropic.stop_reason = "refusal"
    with pytest.raises(llm.LLMError, match="refused"):
        llm.complete_text("ping")


# ------------------------------------------------------------------------------------------------------- live check

LIVE = os.environ.get("LIVE_LLM") == "1"


@pytest.mark.skipif(not LIVE, reason="live Claude Code CLI round trip: run with LIVE_LLM=1 (subscription, no API key)")
def test_live_claude_cli_json_roundtrip(conn, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("LLM_BACKEND", "claude_cli")      # never the paid API from tests
    run_id = db.new_run(conn, "test")
    t0 = time.perf_counter()
    out = llm.complete_json('Reply with exactly this JSON object and nothing else: {"ok": true, "n": 3}',
                            {"type": "object", "required": ["ok", "n"], "properties": {"ok": {"type": "boolean"}, "n": {"type": "integer"}}},
                            conn=conn, run_id=run_id)
    dt = time.perf_counter() - t0
    print(f"\n[live] claude_cli complete_json {dt:.1f}s -> {out}")
    assert out == {"ok": True, "n": 3}
    (row,) = db.rows(conn, "SELECT * FROM costs WHERE run_id=?", (run_id,))
    assert "claude_cli" in row["note"] and row["cost_usd"] == 0 and row["units"] > 0


@pytest.mark.skipif(not LIVE, reason="live Claude Code CLI round trip: run with LIVE_LLM=1 (subscription, no API key)")
def test_live_claude_cli_text(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("LLM_BACKEND", "claude_cli")
    t0 = time.perf_counter()
    text = llm.complete_text("Answer with the single word: pong", system="You answer with one word.")
    print(f"\n[live] claude_cli complete_text {time.perf_counter() - t0:.1f}s -> {text!r}")
    assert "pong" in text.lower()
