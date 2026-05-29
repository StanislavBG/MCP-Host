"""M2 — artifact store: HMAC upload auth, chunked assembly, atomic commit, read view."""

from __future__ import annotations

import tempfile

from mcp_host.artifacts.store import ArtifactStore, verify_upload_auth


def test_upload_auth_constant_time():
    assert verify_upload_auth("Bearer s3cret", "s3cret")
    assert not verify_upload_auth("Bearer wrong", "s3cret")
    assert not verify_upload_auth(None, "s3cret")
    assert not verify_upload_auth("Bearer x", "")  # no secret configured -> deny


def test_single_shot_put_and_read():
    with tempfile.TemporaryDirectory() as d:
        s = ArtifactStore(d)
        n = s.put("edgar-rag", "vectors.bin", b"hello-vectors")
        assert n == len("hello-vectors")
        assert s.view("edgar-rag").read_bytes("vectors.bin") == b"hello-vectors"
        assert s.sha256("edgar-rag", "vectors.bin")


def test_chunked_assembly_and_atomic_commit():
    with tempfile.TemporaryDirectory() as d:
        s = ArtifactStore(d)
        s.begin("signal-builder", "panel_history.db")
        s.append_chunk("signal-builder", "panel_history.db", b"AAAA")
        s.append_chunk("signal-builder", "panel_history.db", b"BBBB")
        n = s.commit("signal-builder", "panel_history.db")
        assert n == 8
        assert s.view("signal-builder").read_bytes("panel_history.db") == b"AAAABBBB"


def test_atomic_swap_replaces_existing():
    with tempfile.TemporaryDirectory() as d:
        s = ArtifactStore(d)
        s.put("edgar-rag", "v.bin", b"old")
        s.put("edgar-rag", "v.bin", b"newer-data")
        assert s.view("edgar-rag").read_bytes("v.bin") == b"newer-data"


def test_isolated_per_provider():
    with tempfile.TemporaryDirectory() as d:
        s = ArtifactStore(d)
        s.put("edgar-rag", "v.bin", b"E")
        assert s.view("signal-builder").read_bytes("v.bin") is None
