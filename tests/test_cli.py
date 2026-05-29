"""CLI lifecycle: scaffold -> validate -> tdqs -> syndicate, and on the real pilots."""

from __future__ import annotations

from pathlib import Path

from cli.main import cmd_scaffold, validate_path
from cli.main import main as cli_main


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
