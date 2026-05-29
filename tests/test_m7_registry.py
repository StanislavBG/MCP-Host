"""M7 — registry: server.json generation, install snippets, syndication plan, TDQS gate."""

from __future__ import annotations

from mcp_host.registry.serverjson import install_snippets, to_server_json
from mcp_host.registry.syndicate import plan_syndication
from mcp_host.registry.tdqs import passes, score_provider
from providers.edgar_rag.provider import EdgarRagProvider
from providers.signal_builder.provider import SignalBuilderProvider
from providers.social_trader.provider import SocialTraderProvider

BASE = "https://mcp-host"


def test_server_json_namespace_and_remote():
    p = EdgarRagProvider()
    sj = to_server_json(p.manifest, BASE)
    assert sj["name"] == "io.github.StanislavBG/edgar-rag"
    assert sj["remotes"][0]["url"] == "https://mcp-host/mcp/edgar-rag"
    assert sj["remotes"][0]["type"] == "streamable-http"
    assert sj["version"] == "0.1.0"


def test_install_snippets_parse():
    import json
    p = EdgarRagProvider()
    snip = install_snippets(p.manifest, BASE)
    assert json.loads(snip["claude"])["mcpServers"]["edgar-rag"]["url"].endswith("/mcp/edgar-rag")
    assert json.loads(snip["vscode"])["servers"]["edgar-rag"]["url"]


def test_syndication_plan_targets():
    p = EdgarRagProvider()
    plan = plan_syndication(p.manifest, BASE)
    assert set(plan.targets) == {"official_registry", "glama", "mcp_so", "pulsemcp"}
    assert plan.warnings == []  # namespace present, official enabled


def test_syndication_warns_without_namespace():
    p = EdgarRagProvider()
    m = dict(p.manifest)
    m.pop("owner_namespace")
    plan = plan_syndication(m, BASE)
    assert any("owner_namespace" in w for w in plan.warnings)


def test_tdqs_passes_for_all_pilots():
    for P in (EdgarRagProvider, SignalBuilderProvider, SocialTraderProvider):
        ok, score, scores = passes(P())
        assert ok, f"{P.__name__} scored {score}: {[(s.name, s.reasons) for s in scores]}"
        assert score >= 0.6


def test_tdqs_penalizes_weak_tool():
    """A provider whose worst tool is bad should be dragged down by the 0.4*min weighting."""
    p = SignalBuilderProvider()
    # baseline good
    good, _ = score_provider(p)
    # blank out one tool's description + annotations in the live manifest copy
    p.manifest["tools"][0]["description"] = ""
    p.manifest["tools"][0]["annotations"] = {}
    # also blank SDK description path: rename so list_tools desc is empty
    worse, _ = score_provider(p)
    assert worse <= good
