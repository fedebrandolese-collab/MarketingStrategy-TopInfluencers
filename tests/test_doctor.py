"""The doctor (src/ring_wishlist/doctor.py): offline, read-only check-up of an install."""
import hashlib
import shutil
import socket
import sqlite3

import pytest

from ring_wishlist import config, db, doctor as D
from ring_wishlist.ingest_map import RAW_FILES

KEYS = ("CARLO_X_HANDLE", "MONID_API_KEY", "X_BEARER_TOKEN", "ANTHROPIC_API_KEY", "MAP_PASSPHRASE", "SOURCE_BULK",
        "SOURCE_VERIFY", "LLM_BACKEND", "CLAUDE_CLI_PATH", "BUDGET_IGNORE_MONTH", *D.BUDGET_VARS)


@pytest.fixture
def clean_env(monkeypatch):
    """No key from a real .env leaks in (config loads it at import), and no DRY_RUN warning."""
    for k in KEYS + ("DRY_RUN",):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def statuses(lines):
    return [s for s, _ in lines]


def find(lines, text):
    hits = [(s, t) for s, t in lines if text in t]
    assert hits, f"no line mentions {text!r}: {lines}"
    return hits[0]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_env_needs_the_keys_of_the_configured_sources_and_backend(tmp_path, clean_env):
    clean_env.setenv("LLM_BACKEND", "anthropic")
    lines = D.check_env(tmp_path)
    assert find(lines, "no .env")[0] == D.WARN
    for key in ("CARLO_X_HANDLE", "MONID_API_KEY", "X_BEARER_TOKEN", "ANTHROPIC_API_KEY"):
        assert find(lines, f"{key} missing")[0] == D.FAIL
    for key in ("CARLO_X_HANDLE", "MONID_API_KEY", "X_BEARER_TOKEN", "ANTHROPIC_API_KEY"):
        clean_env.setenv(key, "secret-value-123")
    (tmp_path / ".env").write_text("CARLO_X_HANDLE=x\n")
    (tmp_path / ".env").chmod(0o600)
    lines = D.check_env(tmp_path)
    assert D.FAIL not in statuses(lines) and D.WARN not in statuses(lines), lines
    assert "secret-value-123" not in repr(lines)                  # values are never printed


def test_env_x_api_bulk_needs_the_bearer_token_and_fixture_or_unknown_sources_are_flagged(tmp_path, clean_env):
    clean_env.setenv("SOURCE_BULK", "x_api")
    clean_env.setenv("SOURCE_VERIFY", "fixture")
    clean_env.setenv("LLM_BACKEND", "anthropic")
    lines = D.check_env(tmp_path)
    assert find(lines, "X_BEARER_TOKEN missing")[1].endswith("needed by SOURCE_BULK=x_api: add it to .env")
    assert find(lines, "SOURCE_VERIFY=fixture")[0] == D.WARN
    assert not any("MONID_API_KEY" in t for _, t in lines)
    clean_env.setenv("SOURCE_BULK", "apify")
    assert find(D.check_env(tmp_path), "SOURCE_BULK=apify")[0] == D.FAIL


def test_env_claude_cli_backend_needs_the_cli(tmp_path, clean_env):
    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    clean_env.setenv("CLAUDE_CLI_PATH", str(cli))
    assert find(D.check_env(tmp_path), "claude CLI at")[0] == D.OK
    clean_env.setenv("CLAUDE_CLI_PATH", str(tmp_path / "nowhere" / "claude"))
    s, text = find(D.check_env(tmp_path), "no claude CLI")
    assert s == D.FAIL and "LLM_BACKEND=anthropic" in text


def test_env_private_file_bad_budget_and_month_bypass(tmp_path, clean_env):
    (tmp_path / ".env").write_text("X=1\n")
    (tmp_path / ".env").chmod(0o644)
    clean_env.setenv("BUDGET_MONID_USD", "ten")
    clean_env.setenv("BUDGET_IGNORE_MONTH", "1")
    clean_env.setenv("DRY_RUN", "1")
    lines = D.check_env(tmp_path)
    assert find(lines, "chmod 600 .env")[0] == D.WARN
    assert find(lines, "BUDGET_MONID_USD='ten'")[0] == D.FAIL
    assert find(lines, "BUDGET_IGNORE_MONTH=1")[0] == D.WARN
    assert find(lines, "DRY_RUN=1")[0] == D.WARN


def test_missing_database_is_a_fresh_install_and_is_not_created(tmp_path):
    p = tmp_path / "data" / "wishlist.db"
    s, text = D.check_db(p)[0]
    assert s == D.WARN and "fresh install" in text
    assert not p.exists() and not p.parent.exists()


def test_healthy_database_is_checked_read_only(tmp_path):
    p = tmp_path / "w.db"
    c = db.connect(p)
    rid = db.new_run(c, "all")
    db.finish_run(c, rid)
    c.close()
    before = sha(p)
    lines = D.check_db(p)
    assert statuses(lines) == [D.OK]
    assert "quick_check ok" in lines[0][1] and f"latest run #{rid}" in lines[0][1] and "schema up to date" in lines[0][1]
    assert sha(p) == before


def test_pending_migration_is_reported_not_applied(tmp_path):
    p = tmp_path / "w.db"
    db.connect(p).close()
    c = sqlite3.connect(p)
    c.execute("ALTER TABLE scores DROP COLUMN engagement")
    c.execute("DROP TABLE run_stages")
    c.commit()
    c.close()
    before = sha(p)
    lines = D.check_db(p)
    assert statuses(lines) == [D.OK, D.WARN]
    assert "column scores.engagement" in lines[1][1] and "table run_stages" in lines[1][1]
    assert "column run_stages." not in lines[1][1]                # a missing table's columns are implied
    assert sha(p) == before
    c = sqlite3.connect(p)
    assert "engagement" not in {r[1] for r in c.execute("PRAGMA table_info(scores)")}
    c.close()


def test_a_file_that_is_not_a_database_fails(tmp_path):
    p = tmp_path / "w.db"
    p.write_bytes(b"not a database, just bytes " * 200)
    s, text = D.check_db(p)[0]
    assert s == D.FAIL and "does not open" in text


def test_map_mirror_present_missing_with_and_without_passphrase(tmp_path, clean_env):
    raw = tmp_path / "raw"
    shutil.copytree(config.RAW_DIR, raw)
    s, text = D.check_map(raw)[0]
    assert s == D.OK and "4 files" in text
    (raw / RAW_FILES["timelines"]).unlink()
    clean_env.setenv("MAP_PASSPHRASE", "pass")
    assert D.check_map(raw)[0][0] == D.WARN
    clean_env.delenv("MAP_PASSPHRASE")
    s, text = D.check_map(raw)[0]
    assert s == D.FAIL and "MAP_PASSPHRASE is not set" in text
    shutil.copy(config.RAW_DIR / RAW_FILES["timelines"], raw)
    (raw / RAW_FILES["meta"]).write_text("{broken")
    assert D.check_map(raw)[0][0] == D.FAIL


def test_the_repo_config_parses():
    lines = D.check_config()
    assert D.FAIL not in statuses(lines), lines
    assert find(lines, "config parses")[0] == D.OK
    assert find(lines, "schedule: daily at")[0] == D.OK


def test_broken_config_files_are_named(tmp_path):
    d = tmp_path / "config"
    shutil.copytree(config.CONFIG_DIR, d)
    (d / "weights.yaml").write_text("presets: [unclosed\n")
    (d / "contact_routes" / "batch_9.yaml").write_text("routes: []\n")
    (d / "queries.yaml").unlink()
    (d / "schedule.yaml").write_text("daily_at: 17:30\nstages: {discovr: 3, crawl: weekly}\n"
                                     "budget: {month_usd: {x-api: 5}}\n")
    lines = D.check_config(d)
    assert find(lines, "config/weights.yaml does not parse")[0] == D.FAIL
    assert find(lines, "batch_9.yaml has no `people:` list")[0] == D.FAIL
    assert find(lines, "config/queries.yaml missing")[0] == D.FAIL
    s, text = find(lines, "config/schedule.yaml:")
    assert s == D.WARN and "the run starts at 10:50" in text and 'daily_at: "17:30"' in text   # 17:30 is YAML's 1050
    assert "'discovr'" in text and "stages.crawl" in text and "'x-api'" in text
    assert "contact_routes (5 files)" in find(lines, "config parses")[1]


def test_disk_is_writable_and_free_space_has_a_floor(tmp_path):
    assert statuses(D.check_disk(tmp_path)) == [D.OK, D.OK]
    assert D.check_disk(tmp_path, min_free_gb=1e12)[-1][0] == D.FAIL
    assert D.check_disk(tmp_path / "missing")[0][0] == D.FAIL


def test_launchd_reads_the_listing_and_the_agents_dir(tmp_path):
    listing = "PID\tStatus\tLabel\n812\t0\tcom.ring-wishlist.dashboard\n-\t0\tcom.apple.x\n"
    lines = D.check_launchd(listing, tmp_path, "Darwin")
    assert lines[0] == (D.WARN, "launchd com.ring-wishlist.wishlist: not installed (make schedule, then make schedule-on)")
    assert lines[1] == (D.OK, "launchd com.ring-wishlist.dashboard: loaded, running (pid 812)")
    (tmp_path / "com.ring-wishlist.wishlist.plist").write_text("<plist/>")
    assert "installed, not loaded" in D.check_launchd(listing, tmp_path, "Darwin")[0][1]
    lines = D.check_launchd(listing + "-\t3\tcom.ring-wishlist.wishlist\n", tmp_path, "Darwin")
    assert lines[0] == (D.WARN, "launchd com.ring-wishlist.wishlist: loaded, last exit 3 (see data/logs)")
    assert D.check_launchd(listing, tmp_path, "Linux") == []


def test_dashboard_port_is_a_plain_socket_connect():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert D.check_dashboard(port, host="127.0.0.1")[0][0] == D.OK
    finally:
        srv.close()
    assert D.check_dashboard(port, host="127.0.0.1")[0][0] == D.WARN


def test_python_version_and_imports(monkeypatch):
    assert D.check_python((3, 9, 1))[0][0] == D.FAIL
    assert D.check_python((3, 11, 4))[0][0] == D.WARN
    monkeypatch.setattr(D, "REQUIRED_MODULES", ("streamlit",))
    assert statuses(D.check_python((3, 12, 1))) == [D.OK, D.OK]
    monkeypatch.setattr(D, "REQUIRED_MODULES", ("ring_wishlist.no_such_module",))
    s, text = D.check_python((3, 12, 1))[1]
    assert s == D.FAIL and "ring_wishlist.no_such_module" in text


def test_main_exits_1_on_any_failure_and_a_crashing_check_is_a_failure(monkeypatch, capsys):
    monkeypatch.setattr(D, "CHECKS", (lambda: [(D.OK, "fine")], lambda: [(D.WARN, "look")]))
    assert D.main() == 0
    assert "✓ fine" in capsys.readouterr().out

    def boom():
        raise RuntimeError("kaput")
    monkeypatch.setattr(D, "CHECKS", (lambda: [(D.OK, "fine")], boom))
    assert D.main() == 1
    out = capsys.readouterr().out
    assert "✗ boom crashed: RuntimeError: kaput" in out and "1 ok, 0 to look at, 1 failing" in out
