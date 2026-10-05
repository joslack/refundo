"""The agent reaches the world only through the MCP server, never by importing it.

`world/` holds the scenarios, the oracle and the labels. If agent code could import it, it could read
the answers.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def imported_modules(path: Path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def test_agent_code_never_imports_the_world():
    files = list((ROOT / "agents").rglob("*.py"))
    assert files, "no agent code found, so this test would check nothing"
    offenders = [
        f"{path.relative_to(ROOT)} imports {module}"
        for path in files
        for module in imported_modules(path)
        if module == "world" or module.startswith("world.")
    ]
    assert not offenders, offenders
