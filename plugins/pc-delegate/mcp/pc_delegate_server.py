#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp", "grimp"]
# ///
"""pc-delegate MCP server — `export_subtree`, the "aller" leg's subtree computation.

Thin wrapper around `subtree.py`'s `compute_subtree` — no reasoning here. `grimp` needs a
third-party resolve, hence this is a native `mcp/` tool rather than a `skills/.../scripts/`
script (this repo's convention reserves third-party dependencies for `mcp/`, resolved by
`uv run --script` from the inline metadata above — no pip-install step for the plugin installer).

Declared via `.mcp.json` at the plugin root.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import subtree
from mcp.server import MCPServer

m = MCPServer("pc-delegate")


@m.tool()
def export_subtree(repo_path: str, touched_files: list[str]) -> str:
    """The files the "aller" leg should zip for `repo_path`: every entry in `touched_files`
    (repo-relative) plus the modules each one directly imports (depth 1, never transitive) —
    resolved on disk via `grimp` for a proper Python package, or a direct-import AST scan for a
    standalone script with no `__init__.py` in its own directory. Returns one repo-relative POSIX
    path per line, sorted, de-duplicated — never the file contents.
    """
    try:
        paths = subtree.compute_subtree(Path(repo_path), touched_files)
    except subtree.SubtreeError as e:
        return f"[export_subtree] BLOCKED — {e}"
    return "\n".join(paths)


if __name__ == "__main__":
    m.run()
