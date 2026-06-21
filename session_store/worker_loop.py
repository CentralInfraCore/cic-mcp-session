"""
Bounded polling loop that repeatedly drives the two existing outbox-workers:
turn_projector.run_projection_batch() (job_type='project_envelope') and
chunk_indexer.run_indexing_batch() (job_type='index_turn').

Job: session-worker-scheduler-001

Source of truth for the batch logic this loop REUSES (not reimplemented
here):
  session_store/turn_projector.py:run_projection_batch()
  session_store/chunk_indexer.py:run_indexing_batch()

Scope: this module ONLY repeatedly calls the two existing batch functions,
in order (projection before indexing, since indexing reads session_core.turns
rows that projection produces), on a configurable interval, for a bounded or
unbounded number of iterations. It does NOT implement any outbox-row
selection, projection, chunking, or embedding logic itself — see
turn_projector.py / chunk_indexer.py for that. It does NOT implement
multi-worker locking/claiming beyond the single-worker-instance assumption
those two modules already document — see input.md "Nem cél".

This module's own CLI entry point (`python -m session_store.worker_loop`) and
this job's pytest suite (tests/test_session_store/test_worker_loop.py) are
the only callers as of this job — see report "Findings"/"Reachability" for
the explicit "exists/tested" vs. "actually scheduled in production"
distinction. No cron/supervisor/systemd timer is wired in by this job; a
documented (not deployed) systemd timer+service unit pair is provided
separately in the report, per input.md "3. Deployment artifact".
"""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass

from session_store.chunk_indexer import run_indexing_batch
from session_store.envelope_writer import SessionStoreConfig
from session_store.turn_projector import run_projection_batch

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True)
class IterationResult:
    """Outcome of a single worker_loop iteration.

    projection_count / indexing_count are the number of outbox rows each
    underlying batch function processed (len() of the list each batch
    function returns) — NOT a success/failure count; a row resolved to
    'failed'/'dead_letter' is still counted here, since it was processed
    (picked up and resolved) by that iteration. See turn_projector.
    ProjectionResult / chunk_indexer.IndexingResult for per-row outcome
    detail, which this dataclass intentionally does not duplicate.
    """

    iteration: int
    projection_count: int
    indexing_count: int


def run_one_iteration(config: SessionStoreConfig | None = None) -> IterationResult:
    """Run exactly one projection batch, then one indexing batch.

    Order matters: projection must run first because indexing reads
    session_core.turns rows that only exist after projection has run (see
    module docstring and input.md "2. Polling loop implementáció" — "előbb
    projekció, utána indexelés, mert az indexelésnek szüksége van a már
    projektált turn-ökre"). Calls the EXISTING run_projection_batch() /
    run_indexing_batch() — no projection/indexing/chunking/embedding logic
    is reimplemented here.

    An empty backlog for either or both batch functions is a normal,
    non-error outcome (both functions return an empty list, never raise for
    "nothing pending") — this is iteration_number-independent: this function
    never raises on an empty backlog.
    """
    cfg = config or SessionStoreConfig.from_env()

    projection_results = run_projection_batch(config=cfg)
    indexing_results = run_indexing_batch(config=cfg)

    return IterationResult(
        iteration=0,  # caller (run_loop) fills in the real 1-based iteration number
        projection_count=len(projection_results),
        indexing_count=len(indexing_results),
    )


def run_loop(
    max_iterations: int | None,
    interval_seconds: float,
    config: SessionStoreConfig | None = None,
) -> list[IterationResult]:
    """Run the bounded (or unbounded) polling loop.

    max_iterations: number of iterations to run; None means run forever
    (production usage, never used in this job's tests — see input.md "Nem
    cél": "a --interval-seconds valós production-értékének
    meghatározása/hangolása" and the bounded-loop requirement for testing).
    A positive int makes the loop testable: it runs exactly that many
    iterations and returns.

    interval_seconds: sleep duration between iterations. NOT slept after
    the LAST iteration (no point delaying the caller's return once the
    bounded loop is done) — only between iterations 1..N-1 inclusive.

    Returns the list of IterationResult, one per iteration actually run, in
    order — this is what callers/tests inspect to prove the loop drained a
    backlog across MULTIPLE iterations, not just in one combined call (see
    input.md "Forbidden Shortcuts": a single-iteration test does not prove
    looping).

    Never raises on an empty backlog in any iteration — see
    run_one_iteration docstring. Only a connection-level failure (e.g.
    Postgres unreachable) propagates out of run_projection_batch/
    run_indexing_batch and is NOT caught here, mirroring those functions'
    own contract (a per-row failure is absorbed internally; a connection
    failure is not, since there is nothing meaningful to retry without a
    DB).
    """
    cfg = config or SessionStoreConfig.from_env()
    results: list[IterationResult] = []

    iteration = 0
    while max_iterations is None or iteration < max_iterations:
        iteration += 1
        result = run_one_iteration(config=cfg)
        result = IterationResult(
            iteration=iteration,
            projection_count=result.projection_count,
            indexing_count=result.indexing_count,
        )
        results.append(result)
        logger.info(
            "worker_loop iteration=%s projection_count=%s indexing_count=%s",
            iteration,
            result.projection_count,
            result.indexing_count,
        )

        is_last_iteration = max_iterations is not None and iteration >= max_iterations
        if not is_last_iteration:
            time.sleep(interval_seconds)

    return results


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m session_store.worker_loop",
        description=(
            "Bounded polling loop driving turn_projector.run_projection_batch() "
            "and chunk_indexer.run_indexing_batch() on an interval."
        ),
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help=(
            "Number of iterations to run, then exit. Omit for an unbounded "
            "loop (production usage; NOT exercised by this job's tests, see "
            "input.md 'Nem cél')."
        ),
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help=(
            f"Sleep duration between iterations, in seconds (default: "
            f"{DEFAULT_INTERVAL_SECONDS}). Tests use a short value (e.g. "
            "0.1-1) — this default is NOT a tuned production value, see "
            "input.md 'Nem cél'."
        ),
    )
    return parser


def _main() -> int:
    """CLI entry point: `python -m session_store.worker_loop`.

    Runs the bounded (or, if --max-iterations is omitted, unbounded) polling
    loop against the Postgres instance configured via SESSION_STORE_PG_*/
    PG* env vars (see SessionStoreConfig.from_env), printing a one-line
    summary per iteration. This is the documented, runnable CLI entry point
    referenced in the report's reachability section; it does NOT by itself
    prove anything about whether something invokes it on a recurring
    schedule in production — see report "Findings"/"Reachability" and the
    separately documented (NOT deployed) systemd timer+service unit pair.
    """
    logging.basicConfig(level=logging.INFO)
    args = _build_arg_parser().parse_args()

    results = run_loop(
        max_iterations=args.max_iterations,
        interval_seconds=args.interval_seconds,
    )

    for r in results:
        print(
            f"iteration={r.iteration} "
            f"projection_count={r.projection_count} "
            f"indexing_count={r.indexing_count}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
