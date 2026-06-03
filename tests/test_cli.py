"""CLI lifecycle: scaffold -> validate -> tdqs -> syndicate, and on the real pilots."""

from __future__ import annotations

import json
from pathlib import Path

from cli.main import build_ingest_request, cmd_scaffold, validate_path
from cli.main import main as cli_main
from mcp_host.auth.principal import verify_token


def test_validate_pilot_passes():
    report = validate_path(Path("providers/edgar_rag/provider.json"))
    assert report["schema"] == "ok"
    assert report["tdqs_pass"] is True
    assert report["reconciled"] == "ok"


def test_scaffold_then_validate(tmp_path):
    class A:
        id = "my-thing"
        dir = str(tmp_path / "mything")
    assert cmd_scaffold(A()) == 0
    report = validate_path(Path(A.dir) / "provider.json")
    assert report["id"] == "my-thing"
    # scaffold ships a valid, deployable skeleton
    assert report["tdqs_pass"] is True


def test_cli_validate_exit_code():
    assert cli_main(["validate", "providers/signal_builder/provider.json"]) == 0


def test_cli_syndicate_runs():
    assert cli_main(["syndicate", "providers/social_trader"]) == 0


def test_cli_tdqs_runs():
    assert cli_main(["tdqs", "providers/edgar_rag"]) == 0


# ---- enhancement 001: owner ingest seam -------------------------------------
def test_cli_token_mints_verifiable_bearer(capsys):
    rc = cli_main(["token", "--provider", "social-trader", "--sub", "StanislavBG",
                   "--scopes", "trader:admin", "--signing-key", "k"])
    assert rc == 0
    token = capsys.readouterr().out.strip()
    principal = verify_token("k", token, "https://mcp-host/mcp/social-trader")
    assert principal.id == "StanislavBG" and "trader:admin" in principal.scopes


def test_cli_ingest_dry_run_builds_tools_call(tmp_path, capsys):
    f = tmp_path / "signals.json"
    f.write_text(json.dumps([{"ticker": "HPE", "side": "short"}]))
    rc = cli_main(["ingest", "social-trader", "signals", str(f), "--mode", "append",
                   "--sub", "StanislavBG", "--signing-key", "k", "--dry-run"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["url"] == "https://mcp-host/mcp/social-trader"
    assert out["headers"]["Authorization"] == "Bearer <redacted>"  # secret not printed
    assert out["body"]["method"] == "tools/call"
    assert out["body"]["params"]["name"] == "signals.ingest"
    assert out["body"]["params"]["arguments"]["mode"] == "append"
    assert out["body"]["params"]["arguments"]["rows"] == [{"ticker": "HPE", "side": "short"}]


def test_cli_ingest_accepts_rows_object(tmp_path):
    f = tmp_path / "signals.json"
    f.write_text(json.dumps({"mode": "replace", "rows": [{"ticker": "AMD", "side": "buy"}]}))
    rc = cli_main(["ingest", "social-trader", "signals", str(f),
                   "--sub", "StanislavBG", "--signing-key", "k", "--dry-run"])
    assert rc == 0


def test_cli_ingest_rejects_bad_file(tmp_path, capsys):
    f = tmp_path / "bad.json"
    f.write_text(json.dumps({"no_rows_here": 1}))
    rc = cli_main(["ingest", "social-trader", "signals", str(f),
                   "--sub", "StanislavBG", "--signing-key", "k", "--dry-run"])
    assert rc == 1


def test_build_ingest_request_shape():
    url, headers, body = build_ingest_request(
        "social-trader", "positions", [{"ticker": "HPE"}], "replace", "https://h", "TOK")
    assert url == "https://h/mcp/social-trader"
    assert headers["Authorization"] == "Bearer TOK"
    assert body["params"]["arguments"]["dataset"] == "positions"
