"""
Multi-consumer smoke test for the shared session.env runtime-env loader.

Job: session-runtime-env-unification-001, "Feladat" 4 ("Valós,
multi-consumer smoke teszt").

Proves — against a REAL Postgres instance, with REAL separate OS
processes, NOT mocked, NOT in-process function calls pretending to be
separate consumers — that:

  1. session_store/worker_loop.py's CLI entry point
     (`python -m session_store.worker_loop`), and
  2. mcp-server/session_server.py's MCP tool dispatch (via a REAL
     mcp.client.stdio subprocess + ClientSession, the SAME evidence bar as
     cic-mcp-gateway/gateway_core/compile_context.py's own test),

resolve to the SAME Postgres instance when BOTH are launched as
subprocesses that only inherit a repo-root-relative session.env file (via
SESSION_ENV_FILE pointing at a tmp_path copy — see fixture below) — NOT
via any shared os.environ the test process injects into both at once.
If session_store/worker_loop.py and mcp-server/session_server.py did NOT
both call session_store.runtime_env.load_session_env() before resolving
SessionStoreConfig.from_env(), this test would fail: each subprocess
would fall back to from_env()'s OWN hardcoded defaults
(localhost:5432/postgres) instead of the test Postgres instance pointed
to by the env file, and the marker row written by consumer 1 would never
become visible to consumer 2 (different DB / connection refused).

The unique marker (see MARKER below) is written by consumer 1 (a real
envelope inserted via session_store.envelope_writer.insert_envelope(),
using config=None so it ALSO resolves via SessionStoreConfig.from_env() /
the same env-file convention — same call shape production code uses) and
then driven through the full real worker_loop CLI subprocess (projection +
indexing), and is then read back by consumer 2 (the session MCP server's
search_session_context_fts tool, via FTS) in a SEPARATE subprocess that
never received the marker directly — only the env file in common.

Requires a reachable Postgres instance with ALL SIX schema/migration SQL
files already applied, in the same order as
tests/test_session_store/test_session_api.py's own module docstring:
    output/session-postgres-schema.sql
    output/session-chunk-indexer-migration.sql
    output/session-retrieval-quality-migration.sql
    output/session-vector-search-api-migration.sql
    output/session-hybrid-search-api-migration.sql
    output/session-source-refs-api-migration.sql
addressed via the SESSION_STORE_PG_* env vars (this test's own pg_config
fixture connects directly, OUTSIDE the env-file mechanism under test, to
set up/verify fixture data and to fail loudly if unreachable — same
"do not silently skip the real-evidence test" stance as
test_session_api.py's own pg_config fixture).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest

from session_store.envelope_writer import SessionStoreConfig, insert_envelope

REPO_ROOT = Path(__file__).resolve().parents[2]

MARKER = f"session-runtime-env-unification-smoke-{uuid.uuid4().hex[:12]}"


@pytest.fixture(scope="module")
def pg_config() -> SessionStoreConfig:
    cfg = SessionStoreConfig(
        host=os.environ.get("SESSION_STORE_PG_HOST", "localhost"),
        port=int(os.environ.get("SESSION_STORE_PG_PORT", "55460")),
        dbname=os.environ.get("SESSION_STORE_PG_DB", "testdb"),
        user=os.environ.get("SESSION_STORE_PG_USER", "postgres"),
        password=os.environ.get("SESSION_STORE_PG_PASSWORD", "test"),
    )
    try:
        with psycopg.connect(cfg.conninfo(), connect_timeout=5):
            pass
    except psycopg.OperationalError as exc:
        pytest.fail(
            "Cannot reach a real Postgres instance for the multi-consumer "
            f"runtime-env smoke test. Original error: {exc}"
        )
    return cfg


@pytest.fixture()
def shared_session_env_file(tmp_path: Path, pg_config: SessionStoreConfig) -> Path:
    """Write ONE session.env-shaped file (the artifact under test) that
    BOTH subprocesses below will be pointed at via SESSION_ENV_FILE — this
    is the actual "common config source" the job closes the drift on, not
    a shared os.environ the test process manufactures.
    """
    env_path = tmp_path / "session.env"
    env_path.write_text(
        "\n".join(
            [
                f"SESSION_STORE_PG_HOST={pg_config.host}",
                f"SESSION_STORE_PG_PORT={pg_config.port}",
                f"SESSION_STORE_PG_DB={pg_config.dbname}",
                f"SESSION_STORE_PG_USER={pg_config.user}",
                f"SESSION_STORE_PG_PASSWORD={pg_config.password}",
                "",
            ]
        )
    )
    return env_path


def _subprocess_env(shared_session_env_file: Path) -> dict:
    """Minimal env for a subprocess: PYTHONPATH (to import session_store/
    mcp-server packages) + SESSION_ENV_FILE (the ONLY DB-config pointer —
    deliberately NOT also setting SESSION_STORE_PG_* directly, so a
    failure to call load_session_env() inside the subprocess would surface
    as a connection to the wrong (default) DB, not as a passthrough from
    this test's own os.environ).
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(REPO_ROOT),
        "SESSION_ENV_FILE": str(shared_session_env_file),
    }


def test_worker_loop_and_mcp_server_share_marker_via_session_env(
    pg_config: SessionStoreConfig,
    shared_session_env_file: Path,
):
    """Consumer 1 (insert + real worker_loop CLI subprocess) writes the
    MARKER; consumer 2 (real session MCP server subprocess, via FTS) reads
    it back — both resolving DB config ONLY through shared_session_env_file
    via SESSION_ENV_FILE, never directly from this test's own os.environ.
    """
    # --- Consumer 1, part A: insert the marker envelope. -----------------
    # config=None deliberately, mirroring production call sites
    # (mcp-server/session_server.py never passes an explicit config
    # either) — this resolves via SessionStoreConfig.from_env(), which in
    # THIS process has already seen pg_config's own os.environ writes (see
    # test_session_api.py-style direct-connect fixtures); the point under
    # test is the TWO SEPARATE SUBPROCESSES below, not this call.
    envelope = {
        "apiVersion": "cic.session/v1",
        "kind": "SessionIngressEnvelope",
        "event_id": str(uuid.uuid4()),
        "provider": "claude-code",
        "provider_session_id": f"sess-runtime-env-smoke-{uuid.uuid4().hex[:8]}",
        "provider_event_name": "Stop",
        "source": {"kind": "hook", "collector": "log-event.py"},
        "occurred_at": "2026-06-25T12:00:00Z",
        "ingested_at": "2026-06-25T12:00:01Z",
        "payload": {"raw_text": MARKER},
        "payload_encoding": "json",
        "raw_payload_hash": "sha256:" + ("b" * 64),
        "trust": "session_local",
        "canonical": False,
        "interpreted": False,
        "idempotency_key": "sha256:" + uuid.uuid4().hex + uuid.uuid4().hex[:32],
        "workstream": None,
        "schema_notes": None,
    }
    row_id = insert_envelope(envelope, config=pg_config)
    assert row_id is not None, "marker envelope insert was a no-op (unexpected idempotency collision)"

    # --- Consumer 1, part B: drive the REAL worker_loop CLI as a SEPARATE
    # subprocess, resolving its DB config ONLY via SESSION_ENV_FILE. This
    # is what projects the marker envelope into session_core.turns and
    # indexes it into session_idx.chunk_fts (so consumer 2's FTS lookup
    # below has something to find) — input.md "Feladat" 4: "a worker loop
    # egy egyszeri futása".
    worker_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "session_store.worker_loop",
            "--max-iterations",
            "1",
            "--interval-seconds",
            "0",
        ],
        cwd=str(REPO_ROOT),
        env=_subprocess_env(shared_session_env_file),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert worker_proc.returncode == 0, (
        f"worker_loop subprocess failed (rc={worker_proc.returncode}):\n"
        f"stdout={worker_proc.stdout}\nstderr={worker_proc.stderr}"
    )
    assert "iteration=1" in worker_proc.stdout, (
        f"worker_loop subprocess did not report running iteration 1: {worker_proc.stdout!r}"
    )

    # --- Consumer 2: the REAL session MCP server, as a REAL, independent
    # subprocess, talked to via REAL mcp.client.stdio (same evidence bar
    # as cic-mcp-gateway/gateway_core/compile_context.py) — resolving its
    # OWN DB config ONLY via SESSION_ENV_FILE (the same file, never told
    # to this subprocess any other way). input.md "Feladat" 4: "a session
    # MCP szerver egy lekérdezése".
    import asyncio

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def _query_via_mcp_server() -> list[dict]:
        server_params = StdioServerParameters(
            command=sys.executable,
            args=[str(REPO_ROOT / "mcp-server" / "session_server.py")],
            env=_subprocess_env(shared_session_env_file),
        )
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(
                    "search_session_context_fts",
                    {
                        "session_id": str(_get_session_id(pg_config, envelope["provider_session_id"])),
                        "query": MARKER,
                        "limit": 5,
                    },
                )
                # FastMCP (mcp SDK 1.28.0, verified empirically against
                # THIS server) populates .structuredContent for a
                # list[dict]-returning tool as {"result": [...]} (the MCP
                # spec wraps non-object return types under a "result" key
                # for structured content) — unwrap that, falling back to
                # content[0].text (also "{\"result\": [...]}"-shaped in
                # this SDK version) only if structuredContent is absent.
                structured = getattr(result, "structuredContent", None)
                if structured is not None:
                    payload = (
                        structured["result"]
                        if isinstance(structured, dict) and set(structured.keys()) == {"result"}
                        else structured
                    )
                else:
                    assert result.content, f"empty MCP tool result: {result!r}"
                    decoded = json.loads(result.content[0].text)
                    payload = (
                        decoded["result"]
                        if isinstance(decoded, dict) and set(decoded.keys()) == {"result"}
                        else decoded
                    )
                return payload

    rows = asyncio.run(_query_via_mcp_server())

    assert any(MARKER in row.get("text", "") for row in rows), (
        f"consumer 2 (session MCP server subprocess) did not find the marker "
        f"written by consumer 1 (worker_loop subprocess) — rows={rows!r}. "
        "This means the two subprocesses, both pointed ONLY at "
        "SESSION_ENV_FILE, resolved to DIFFERENT Postgres instances."
    )


def _get_session_id(pg_config: SessionStoreConfig, provider_session_id: str):
    with psycopg.connect(pg_config.conninfo()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT session_id FROM session_core.sessions "
                "WHERE provider_session_id = %s",
                (provider_session_id,),
            )
            row = cur.fetchone()
    assert row is not None, (
        f"no session_core.sessions row for provider_session_id={provider_session_id!r} "
        "— the worker_loop subprocess did not project the marker envelope"
    )
    return row[0]
