"""
Single, shared runtime-env loader for ALL cic-mcp-session DB-config
consumers (worker loop, session MCP server) — and, via the same env-file
convention, the cic-mcp-gateway session-adapter subprocess launch path.

Job: session-runtime-env-unification-001

Problem this closes (input.md "Kontextus"): session_store/worker_loop.py,
mcp-server/session_server.py, and cic-mcp-gateway/gateway_core/
compile_context.py each end up reading SESSION_STORE_PG_*/PG* purely from
whatever happens to be in os.environ at process-start time, with NO shared
mechanism to point all of them at the same env FILE. A config change that
is only exported in one shell (or only present in one process's env) then
silently does not apply to the others — worker, MCP server, and gateway
subprocess can each resolve to a different Postgres instance without any
of them raising an error.

What this module does NOT do (input.md "Nem cél" — no business-logic
change): it does NOT touch session_store.envelope_writer.SessionStoreConfig
itself (dataclass shape, conninfo() format, the SESSION_STORE_PG_*/PG*
env-var NAMES and their precedence/fallback order are all UNCHANGED — see
envelope_writer.py:88-96). This module only adds ONE missing layer BEFORE
SessionStoreConfig.from_env() is called: loading a single, repo-root-
relative .env-style FILE into os.environ (without overriding any var the
calling process's shell/orchestrator already exported), so multiple
independent processes started from the same checkout — or pointed at the
same file via SESSION_ENV_FILE — observe the SAME variable values, not
whatever subset their own launcher happened to export.

Env-file format decision (input.md "2. Közös env-fájl formátum"): plain
`.env`-style `KEY=value` lines, reusing the SAME five keys
SessionStoreConfig.from_env() already reads (SESSION_STORE_PG_HOST/_PORT/
_DB/_USER/_PASSWORD) — NOT a single DSN string. Rationale: a single
SESSION_STORE_PG_DSN value would require a SECOND parser (or a change to
SessionStoreConfig itself, which input.md "Nem cél" forbids) to split it
back into host/port/dbname/user/password for callers that need the parts
individually (e.g. a future health-check); the five-key form is a no-op
change for every existing caller, since envelope_writer.py already defines
and reads exactly these five names.

Resolution order for WHICH file gets loaded (first match wins; this
mirrors the "explicit override > repo convention > nothing to load"
pattern, not a new precedence concept):
  1. SESSION_ENV_FILE env var, if set (explicit override — e.g. a test
     pointing every consumer at one shared tmp_path file).
  2. <repo_root>/session.env, if it exists (the repo-root convention file;
     .gitignore'd, see .gitignore "Local runtime env file" entry added by
     this job).
  3. Nothing — load_session_env() is a no-op, and SessionStoreConfig.
     from_env() falls back to ITS OWN existing defaults exactly as before
     this job (localhost/5432/postgres/postgres/'' or PG*), so a checkout
     with no env file at all is byte-for-byte the pre-this-job behavior.

Values already present in os.environ are NEVER overwritten (override=False
in dotenv_values application below) — an explicit shell export still wins
over the file, same "environment wins over file" convention python-dotenv
itself defaults to.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_ENV_FILENAME = "session.env"


def session_env_file_path() -> Path | None:
    """Resolve which env file load_session_env() would load, without
    loading it. Returns None if neither SESSION_ENV_FILE nor
    <repo_root>/session.env resolve to an existing file (the "nothing to
    load" case in the module docstring's resolution order).
    """
    override = os.environ.get("SESSION_ENV_FILE")
    if override:
        path = Path(override)
        return path if path.is_file() else None

    default_path = REPO_ROOT / DEFAULT_ENV_FILENAME
    return default_path if default_path.is_file() else None


def load_session_env() -> Path | None:
    """Load the resolved session env file (see module docstring,
    "Resolution order") into os.environ, WITHOUT overwriting any variable
    that is already set. Returns the Path that was loaded, or None if no
    env file was found (a no-op in that case — callers fall back to
    SessionStoreConfig.from_env()'s own existing defaults).

    Idempotent and safe to call from multiple entry points (worker_loop
    CLI, session_server.py module import) — calling it twice in the same
    process is a no-op the second time for any var the first call already
    set (the existing os.environ value is never replaced).
    """
    path = session_env_file_path()
    if path is None:
        return None

    values = dotenv_values(path)
    for key, value in values.items():
        if value is None:
            continue
        os.environ.setdefault(key, value)
    return path
