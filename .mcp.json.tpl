{
  "mcpServers": {
    "cic-graph": {
      "command": "{{REPO_ROOT}}/.venv-host/bin/python",
      "args": [
        "{{REPO_ROOT}}/mcp-server/server.py"
      ],
      "env": {
        "KB_DATA_DIR": "{{REPO_ROOT}}/kb_data/pkl"
      }
    },
    "cic-session": {
      "command": "{{REPO_ROOT}}/.venv-host/bin/python",
      "args": [
        "{{REPO_ROOT}}/mcp-server/session_server.py"
      ],
      "env": {
        "PYTHONPATH": "{{REPO_ROOT}}"
      }
    }
  }
}
