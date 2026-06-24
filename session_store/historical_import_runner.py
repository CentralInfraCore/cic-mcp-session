"""
Batch runner: walk a sharded ChatGPT export bundle end-to-end and persist it.

Job: historical-import-runner-001

Source of truth for the export bundle shard layout (`conversations-NNN.json`,
zero-padded 3 digits, top-level `list` of conversation-objects, each with a
`mapping` dict of `{id, message, parent, children}` nodes):
  jobs/historical-chatgpt-export-importer-001/output/historical-chatgpt-importer-design.md
  (cic-mcp-factory repo), section "Export Bundle Structure".

Source of truth for the open question this module closes (the `mapping`
tree traversal order, design report line 329: "A `mapping` fa-bejarasi
sorrendje NYITOTT KERDES marad"):
  see "Traversal order decision" below.

Scope: this module ONLY walks already-on-disk shard files and, for every
mapping-node it visits, calls the EXISTING
session_store.chatgpt_import.chatgpt_message_to_envelope() and
session_store.envelope_writer.insert_envelope(). It does NOT:
  - reimplement the envelope conversion logic (occurred_at normalization,
    raw_payload_hash, idempotency_key) -- that lives in chatgpt_import.py
    and is called verbatim
  - reimplement the ON CONFLICT DO NOTHING dedupe -- that lives in
    envelope_writer.insert_envelope() and is called verbatim
  - touch any real, personal export bundle -- see this job's
    output/historical-import-runner.md "Findings" for the explicit
    security boundary (real-bundle runs require a separate security
    review that has NOT happened as part of this job)

Traversal order decision
-------------------------
DFS preorder from the tree's root node(s), following the `children` array
in on-disk order, with `parent is None` (or `parent` not present) used to
identify root node(s).

Why DFS preorder (not BFS, not "walk back from current_node via parent"):
  - The design report explicitly left this open (line 329) and only noted
    two directions as conceivable ("gyokertol `children`-en előre, vagy
    `current_node`-tol visszafele `parent`-en") -- walking backward from
    `current_node` is REJECTED here because it only recovers the single
    active branch of a (possibly branching, due to regenerate/edit)
    conversation tree, silently dropping sibling branches -- exactly the
    failure mode the design report's own "Risks" section warns about
    ("elveszítve elagazo branch-eket").
  - DFS preorder from the root(s), shape `node -> children[0] (recursively)
    -> children[1] (recursively) -> ...`, visits EVERY node exactly once
    and never depends on `current_node` (which may be absent or stale),
    so it is robust to a conversation tree that branched (edits/
    regenerations) and was never linearized by the export itself.
  - It is fully deterministic given the on-disk `children` array order,
    which is what the Definition Of Done requires ("a sorszámoknak
    PONTOSAN egyezniük kell" across independent runs) -- no dict-iteration-
    order dependency, since traversal is driven by the `children` list
    (an ordered array), not by iterating `mapping.keys()`.
  - Root identification: a node is a root if `node.get("parent") is None`.
    A conversation's `mapping` MAY have more than one such node (e.g. if
    the export's tree has detached branches); this runner visits ALL of
    them, in the order they appear in `mapping` (dict insertion order,
    which is on-disk JSON key order -- the export's own canonical order
    for the conversation's node collection), so multi-root conversations
    are not silently dropped.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

from session_store.chatgpt_import import chatgpt_message_to_envelope
from session_store.envelope_writer import SessionStoreConfig, insert_envelope

logger = logging.getLogger(__name__)

SHARD_GLOB = "conversations-*.json"


class ImportInterrupted(Exception):
    """Raised by a caller-supplied `fail_after` hook to simulate a crash.

    Used ONLY by this job's own tests (kill-mid-run/resume proof) to force
    an interruption at a deterministic point mid-shard, without needing an
    actual external `kill -9` -- the resulting partially-applied DB state
    (some rows committed via insert_envelope(), then the exception
    propagates and run() aborts) is the same observable scenario a real
    process kill would produce, since insert_envelope() commits per-row
    (see envelope_writer.py:226-230, one `with psycopg.connect(...)` per
    call -- there is no multi-row transaction to roll back).
    """


@dataclass
class ShardResult:
    """Outcome of importing ONE shard file."""

    shard_path: Path
    conversations_seen: int = 0
    nodes_visited: int = 0
    rows_inserted: int = 0
    rows_deduped: int = 0
    completed: bool = False


@dataclass
class ImportRunResult:
    """Outcome of one run() call across all shards in a bundle directory."""

    shard_results: list[ShardResult] = field(default_factory=list)

    @property
    def total_rows_inserted(self) -> int:
        return sum(r.rows_inserted for r in self.shard_results)

    @property
    def total_rows_deduped(self) -> int:
        return sum(r.rows_deduped for r in self.shard_results)

    @property
    def total_nodes_visited(self) -> int:
        return sum(r.nodes_visited for r in self.shard_results)


def discover_shards(bundle_dir: Path) -> list[Path]:
    """Return shard files in `bundle_dir`, sorted by filename.

    Filename sort is correct ordering here ONLY because the real export
    names shards `conversations-NNN.json` with a FIXED zero-padded width
    (design report "Export Bundle Structure": "Pontosan 20 darab,
    conversations-000.json ... conversations-019.json nevmintaval
    (zero-padded, 3 szamjegy)") -- lexicographic sort on a fixed-width
    zero-padded numeric suffix is equivalent to numeric sort.
    """
    return sorted(Path(bundle_dir).glob(SHARD_GLOB))


def iter_mapping_nodes_dfs_preorder(
    mapping: Mapping[str, Any],
) -> Iterator[Mapping[str, Any]]:
    """Yield every node in `mapping` in deterministic DFS preorder.

    See module docstring "Traversal order decision" for the full rationale.
    Roots are nodes with `parent is None` (or missing `parent`), visited in
    `mapping` dict order (on-disk JSON key order). From each root, descends
    via `children` (an ordered array of node-id strings), recursively,
    preorder (the node itself is yielded before any of its children).

    A `children` entry whose id is not present in `mapping` is skipped
    (defensive -- malformed/partial export data should not crash the
    traversal; this is a structural-integrity concern, not a content
    decision, so it does not violate the "do not interpret content"
    boundary).
    """
    visited: set[str] = set()

    def _walk(node_id: str) -> Iterator[Mapping[str, Any]]:
        if node_id in visited:
            return
        node = mapping.get(node_id)
        if node is None:
            return
        visited.add(node_id)
        yield node
        for child_id in node.get("children") or []:
            yield from _walk(child_id)

    roots = [
        node_id
        for node_id, node in mapping.items()
        if node.get("parent") is None
    ]
    for root_id in roots:
        yield from _walk(root_id)

    # Defensive: any node not reachable from a declared root (detached
    # subtree -- should not happen in a well-formed export, but the runner
    # must not silently drop nodes if it does) is still visited, in
    # `mapping` dict order, after all root-reachable nodes.
    for node_id, node in mapping.items():
        if node_id not in visited:
            yield from _walk(node_id)


def _shard_progress_path(bundle_dir: Path) -> Path:
    """Path to the progress-marker file for a given bundle directory.

    Format: a plain-text file, one fully-completed shard FILENAME per
    line (not a path, not an index -- so it stays valid if shards are
    moved between directories). Chosen over a DB-side marker table
    because:
      - this runner has NO migration of its own (Nem cel: it must not
        require a schema change just to track progress) -- a flat file
        next to the bundle needs no DB access to read/write
      - it only needs to skip ALREADY-FULLY-DONE shards on a cold resume
        before even opening a DB connection -- the DB-side
        idempotency_key UNIQUE constraint (envelope_writer.py:199,
        "ON CONFLICT (idempotency_key) DO NOTHING") is what actually
        GUARANTEES no duplication, even if this marker file were stale,
        missing, or wrong (see module docstring 3rd bullet under
        "Runner-implementacio" in input.md) -- this file is purely a
        performance/skip-already-done optimization, never a correctness
        mechanism
    """
    return Path(bundle_dir) / ".historical_import_progress"


def _read_completed_shards(bundle_dir: Path) -> set[str]:
    progress_path = _shard_progress_path(bundle_dir)
    if not progress_path.exists():
        return set()
    return {
        line.strip()
        for line in progress_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def _mark_shard_completed(bundle_dir: Path, shard_name: str) -> None:
    progress_path = _shard_progress_path(bundle_dir)
    with progress_path.open("a", encoding="utf-8") as fh:
        fh.write(shard_name + "\n")


def import_shard(
    shard_path: Path,
    *,
    config: SessionStoreConfig | None = None,
    fail_after: int | None = None,
) -> ShardResult:
    """Import ONE shard file: every conversation, every mapping-node.

    For every node, calls chatgpt_message_to_envelope() (unmodified,
    session_store/chatgpt_import.py:144) then insert_envelope()
    (unmodified, session_store/envelope_writer.py:165) -- this function
    does not duplicate either function's logic, it only sequences calls
    to them per the traversal order decided above.

    `fail_after`: if set, raises ImportInterrupted after exactly this many
    nodes have been visited IN THIS SHARD (across the whole shard, not
    per-conversation) -- used only by tests to simulate a kill mid-shard,
    AFTER some rows have already been committed by insert_envelope() but
    before the shard finishes.
    """
    result = ShardResult(shard_path=shard_path)

    conversations = json.loads(Path(shard_path).read_text(encoding="utf-8"))
    for conversation in conversations:
        result.conversations_seen += 1
        mapping = conversation.get("mapping") or {}

        for node in iter_mapping_nodes_dfs_preorder(mapping):
            if node.get("message") is None:
                # A mapping-node with message: null (e.g. the synthetic
                # root container some exports use) carries nothing
                # chatgpt_message_to_envelope() can convert -- skipping it
                # is a structural/shape decision (no content read), not a
                # semantic interpretation of conversation content.
                continue

            result.nodes_visited += 1

            envelope = chatgpt_message_to_envelope(conversation, node)
            new_id = insert_envelope(envelope, config=config)
            if new_id is not None:
                result.rows_inserted += 1
            else:
                result.rows_deduped += 1

            if fail_after is not None and result.nodes_visited >= fail_after:
                raise ImportInterrupted(
                    f"simulated interruption after {result.nodes_visited} "
                    f"node(s) in {shard_path.name} (test-only fail_after hook)"
                )

    result.completed = True
    return result


def run(
    bundle_dir: str | Path,
    *,
    config: SessionStoreConfig | None = None,
    fail_after_shard: str | None = None,
    fail_after_node: int | None = None,
    use_progress_marker: bool = True,
) -> ImportRunResult:
    """Walk every `conversations-NNN.json` shard in `bundle_dir` and import it.

    Shards already recorded as fully completed in the progress marker file
    (see `_shard_progress_path`) are skipped without being re-read --
    purely a performance optimization on resume, NOT what makes resume
    safe (see `_shard_progress_path` docstring and module docstring 3rd
    bullet under "Runner-implementacio").

    `fail_after_shard` / `fail_after_node`: test-only hooks. When the
    shard whose filename equals `fail_after_shard` is reached, that
    shard's import is run with `fail_after=fail_after_node`, so the
    caller can deterministically simulate "the process was killed midway
    through processing the Nth node of shard X" and then call `run()`
    again to prove resume correctness.
    """
    bundle_dir = Path(bundle_dir)
    shards = discover_shards(bundle_dir)
    completed_shards = (
        _read_completed_shards(bundle_dir) if use_progress_marker else set()
    )

    run_result = ImportRunResult()

    for shard_path in shards:
        if shard_path.name in completed_shards:
            logger.info(
                "skipping already-completed shard %s (progress marker)",
                shard_path.name,
            )
            continue

        fail_after = (
            fail_after_node if shard_path.name == fail_after_shard else None
        )
        shard_result = import_shard(
            shard_path, config=config, fail_after=fail_after
        )
        run_result.shard_results.append(shard_result)

        if use_progress_marker:
            _mark_shard_completed(bundle_dir, shard_path.name)

    return run_result


def _main(argv: list[str]) -> int:  # pragma: no cover - thin CLI wrapper
    """Minimal CLI entry point: `python -m session_store.historical_import_runner <bundle_dir>`.

    NOT wired into any MCP tool, hook, or other production caller -- this
    job's "Nem cel" scope excludes shared/gateway wiring; this is a manual
    operator entry point only, mirroring chatgpt_import.py's own "no
    production caller in this job" framing.
    """
    if len(argv) != 1:
        print(
            "usage: python -m session_store.historical_import_runner <bundle_dir>",
            file=sys.stderr,
        )
        return 2
    result = run(argv[0])
    print(
        f"shards processed: {len(result.shard_results)}, "
        f"nodes visited: {result.total_nodes_visited}, "
        f"rows inserted: {result.total_rows_inserted}, "
        f"rows deduped: {result.total_rows_deduped}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main(sys.argv[1:]))
