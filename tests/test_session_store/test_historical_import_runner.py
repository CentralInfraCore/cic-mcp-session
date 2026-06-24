"""
Tests for session_store.historical_import_runner against a REAL Postgres
instance, including a kill-mid-run/resume proof.

Job: historical-import-runner-001

SECURITY BOUNDARY (same pattern as tests/test_session_store/
test_chatgpt_import.py, originally from historical-dedupe-idempotency-001
"KRITIKUS BIZTONSAGI HATAR"): every shard file written by this test module
is ENTIRELY FABRICATED, SYNTHETIC content, generated in-process by the
fixtures below. None of it originates from, nor is derived from, any real
personal ChatGPT export bundle -- only the STRUCTURE (sharded
`conversations-NNN.json` filenames, top-level `list` of conversation-
objects, `mapping` dict of `{id, message, parent, children}` nodes,
`author.role` / `content.content_type` field names) is taken from the
structural design report
(jobs/historical-chatgpt-export-importer-001/output/historical-chatgpt-importer-design.md,
cic-mcp-factory repo) and the existing
tests/test_session_store/test_chatgpt_import.py fixture style. All
conversation ids, node ids, and message text below (e.g.
"synthetic-conv-000-001", "synthetic message body, node 3 of shard 1") are
obviously-fabricated placeholder values, not real data. A real, personal
export-bundle run is explicitly NOT exercised here and requires a separate
security review that has NOT happened as part of this job (see
output/historical-import-runner.md "Findings").

These tests do NOT mock the database connection -- same real-Postgres
pattern as test_envelope_writer.py / test_chatgpt_import.py (pg_config /
_clean_envelopes_table / _count_rows fixtures reused verbatim, NOT
reinvented here).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from session_store.historical_import_runner import (
    ImportInterrupted,
    discover_shards,
    iter_mapping_nodes_dfs_preorder,
    run,
)
from tests.test_session_store.test_envelope_writer import (
    _clean_envelopes_table,  # noqa: F401 (autouse fixture, re-exported on purpose)
    _count_rows,
    pg_config,  # noqa: F401 (session-scoped fixture, re-exported on purpose)
)

# ---------------------------------------------------------------------------
# Synthetic multi-shard bundle builder -- FABRICATED, see module docstring.
# ---------------------------------------------------------------------------


def _synthetic_message_node(
    node_id: str,
    parent_id: str | None,
    children: list[str],
    role: str,
    create_time: float,
    text: str,
) -> dict:
    """One fabricated `mapping`-node, shape per design report:
    {id, message, parent, children} with message = {author, content, ...}.
    """
    return {
        "id": node_id,
        "parent": parent_id,
        "children": children,
        "message": {
            "id": node_id,
            "author": {"role": role, "name": None, "metadata": {}},
            "create_time": create_time,
            "update_time": create_time,
            "content": {"content_type": "text", "parts": [text]},
            "status": "finished_successfully",
            "end_turn": True,
            "weight": 1.0,
            "metadata": {
                "can_save": True,
                "is_visually_hidden_from_conversation": False,
            },
            "recipient": "all",
            "channel": None,
        },
    }


def _synthetic_conversation(conv_id: str, base_time: float) -> dict:
    """One fabricated conversation: a 3-node linear thread,
    root(system, message=None) -> user -> assistant, DFS-preorder-shaped.

    The root node has message=None (mirrors a real export's synthetic
    root-container node, design report "mapping-node kulcsai" -- system/
    tool nodes with a None message exist and must not crash the runner,
    see historical_import_runner.import_shard()'s explicit None-message
    skip).
    """
    root_id = f"{conv_id}-root"
    user_id = f"{conv_id}-user"
    assistant_id = f"{conv_id}-assistant"

    root_node = {
        "id": root_id,
        "parent": None,
        "children": [user_id],
        "message": None,
    }
    user_node = _synthetic_message_node(
        user_id,
        root_id,
        [assistant_id],
        "user",
        base_time,
        f"synthetic user message body for {conv_id}",
    )
    assistant_node = _synthetic_message_node(
        assistant_id,
        user_id,
        [],
        "assistant",
        base_time + 5,
        f"synthetic assistant reply body for {conv_id}",
    )

    return {
        "conversation_id": conv_id,
        "id": conv_id,
        "title": f"synthetic test conversation {conv_id} - not real data",
        "current_node": assistant_id,
        "mapping": {
            root_id: root_node,
            user_id: user_node,
            assistant_id: assistant_node,
        },
    }


def _write_synthetic_bundle(tmp_path: Path, conversations_per_shard: int = 2) -> Path:
    """Write 3 fabricated `conversations-NNN.json` shard files into tmp_path.

    Each shard holds `conversations_per_shard` fabricated conversations,
    each with 2 importable nodes (user + assistant; the root has
    message=None and is skipped) -- 3 shards * 2 conversations * 2 nodes
    = 12 importable nodes total across the whole synthetic bundle.
    """
    bundle_dir = tmp_path / "synthetic_export_bundle"
    bundle_dir.mkdir()

    for shard_idx in range(3):
        conversations = [
            _synthetic_conversation(
                f"synthetic-conv-{shard_idx:03d}-{conv_idx:03d}",
                base_time=1700000000.0 + (shard_idx * 100) + (conv_idx * 10),
            )
            for conv_idx in range(conversations_per_shard)
        ]
        shard_path = bundle_dir / f"conversations-{shard_idx:03d}.json"
        shard_path.write_text(json.dumps(conversations), encoding="utf-8")

    return bundle_dir


# ---------------------------------------------------------------------------
# 1. Traversal order unit test (no DB) -- proves DFS preorder is what's
#    actually implemented, independent of the DB-backed tests below.
# ---------------------------------------------------------------------------
def test_dfs_preorder_visits_root_then_children_in_order():
    mapping = {
        "root": {"id": "root", "parent": None, "children": ["a", "b"], "message": None},
        "a": {"id": "a", "parent": "root", "children": ["a1"], "message": "msg-a"},
        "a1": {"id": "a1", "parent": "a", "children": [], "message": "msg-a1"},
        "b": {"id": "b", "parent": "root", "children": [], "message": "msg-b"},
    }

    visited_ids = [node["id"] for node in iter_mapping_nodes_dfs_preorder(mapping)]

    assert visited_ids == ["root", "a", "a1", "b"]


def test_dfs_preorder_visits_detached_subtree_without_dropping_it():
    # "x" has no declared root pointing to it and is not reachable from any
    # parent==None node -- the defensive fallback in
    # iter_mapping_nodes_dfs_preorder must still visit it, not silently drop it.
    mapping = {
        "root": {"id": "root", "parent": None, "children": [], "message": "msg-root"},
        "x": {"id": "x", "parent": "missing-parent", "children": [], "message": "msg-x"},
    }

    visited_ids = [node["id"] for node in iter_mapping_nodes_dfs_preorder(mapping)]

    assert set(visited_ids) == {"root", "x"}


# ---------------------------------------------------------------------------
# 2. Shard discovery is sorted, deterministic.
# ---------------------------------------------------------------------------
def test_discover_shards_returns_sorted_filenames(tmp_path):
    bundle_dir = _write_synthetic_bundle(tmp_path)

    shards = discover_shards(bundle_dir)

    assert [s.name for s in shards] == [
        "conversations-000.json",
        "conversations-001.json",
        "conversations-002.json",
    ]


# ---------------------------------------------------------------------------
# 3. Real Postgres: full run over the synthetic 3-shard bundle inserts
#    every importable node exactly once.
# ---------------------------------------------------------------------------
def test_full_run_inserts_every_node_once(tmp_path, pg_config):
    bundle_dir = _write_synthetic_bundle(tmp_path)

    result = run(bundle_dir, config=pg_config)

    # 3 shards * 2 conversations * 2 importable nodes (user + assistant;
    # the root node has message=None and is skipped) = 12.
    assert result.total_nodes_visited == 12
    assert result.total_rows_inserted == 12
    assert result.total_rows_deduped == 0
    assert _count_rows(pg_config) == 12


# ---------------------------------------------------------------------------
# 4. Kill-mid-run / resume proof -- the Definition Of Done's core claim.
# ---------------------------------------------------------------------------
def test_kill_mid_run_then_resume_matches_full_run_row_count(tmp_path, pg_config):
    bundle_dir = _write_synthetic_bundle(tmp_path)

    # --- baseline: full run, count rows, then truncate for a clean retry ---
    baseline_result = run(bundle_dir, config=pg_config)
    baseline_rows = _count_rows(pg_config)
    assert baseline_rows == 12
    assert baseline_result.total_rows_inserted == 12

    with pg_config_connect(pg_config) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE session_raw.envelopes CASCADE")
        conn.commit()
    assert _count_rows(pg_config) == 0

    # A FRESH bundle_dir copy for the interrupted run, since the progress
    # marker file from the baseline run above already lives next to the
    # original shards and would cause every shard to be skipped.
    interrupted_bundle_dir = tmp_path / "synthetic_export_bundle_run2"
    interrupted_bundle_dir.mkdir()
    for shard_path in discover_shards(bundle_dir):
        (interrupted_bundle_dir / shard_path.name).write_text(
            shard_path.read_text(encoding="utf-8"), encoding="utf-8"
        )

    # --- step 1: intentionally interrupt mid-way through shard 1 (the
    #     2nd shard, conversations-001.json), after exactly 1 of its 4
    #     importable nodes has been inserted ---
    with pytest.raises(ImportInterrupted):
        run(
            interrupted_bundle_dir,
            config=pg_config,
            fail_after_shard="conversations-001.json",
            fail_after_node=1,
        )

    rows_after_kill = _count_rows(pg_config)
    # shard 000 fully completed (4 nodes) + shard 001 partially completed
    # (1 node, before the simulated kill) = 5 rows committed so far.
    assert rows_after_kill == 5

    # --- step 2: resume -- run() again over the SAME bundle dir, no
    #     fail_after hooks this time ---
    resume_result = run(interrupted_bundle_dir, config=pg_config)

    rows_after_resume = _count_rows(pg_config)

    # The core Definition Of Done claim: row count after kill+resume
    # EXACTLY matches the original full-run baseline, no duplication, no
    # missing node.
    assert rows_after_resume == baseline_rows == 12

    # shard 000 was skipped on resume (progress marker: already complete),
    # so resume only re-ran shard 001 (re-inserting its 1 already-committed
    # node as a dedupe no-op, then inserting its remaining 3 new nodes) and
    # shard 002 (4 new nodes).
    assert resume_result.total_rows_inserted == 7  # 3 new in shard001 + 4 in shard002
    assert resume_result.total_rows_deduped == 1  # the 1 node re-converted from shard001


def pg_config_connect(pg_config):
    import psycopg

    return psycopg.connect(pg_config.conninfo())
