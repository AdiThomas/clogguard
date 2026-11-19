"""Static guarantee: no code path to the printer outside ``clogguard/safety``.

Scans every ``clogguard/**/*.py`` with ``ast`` and fails if a module outside the allowed set
calls ``.call(...)`` on anything, or contains a string mentioning the printer G-code or print
control RPC methods.

Explicit exclusions (documented):

* ``clogguard/safety/`` - the gate itself; the only permitted caller.
* ``clogguard/sim/`` - implements the *server* side (mock Moonraker); it must name the methods.
* ``clogguard/moonraker/`` - the generic JSON-RPC client; it implements ``call`` for any method.
* ``clogguard/core/`` - shared interfaces (docstrings describe the protocol).

Transport adapters (``TRANSPORT_ADAPTERS``) implement ``PrinterLink.call`` by forwarding. In
those files ``.call(`` is allowed only inside a method named ``call`` whose positional
arguments are passed through unchanged; the string ban still applies to them.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "clogguard"
EXCLUDED_DIRS = ("safety", "sim", "moonraker", "core")
TRANSPORT_ADAPTERS = {"service/link.py"}
BANNED_STRINGS = ("printer.gcode.script", "printer.print.")


def _rel(path: Path) -> str:
    return path.relative_to(PKG).as_posix()


def _scanned_files() -> list[Path]:
    return [
        p for p in sorted(PKG.rglob("*.py"))
        if _rel(p).split("/")[0] not in EXCLUDED_DIRS
    ]


def _forwarding_calls(tree: ast.AST) -> set[int]:
    """ids of ``.call(...)`` nodes that only forward a ``call`` method's own arguments."""
    ok: set[int] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef) or fn.name != "call":
            continue
        params = [a.arg for a in fn.args.args[1:]]  # drop self
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "call"
                and not node.keywords
                and [a.id for a in node.args if isinstance(a, ast.Name)] == params
                and len(node.args) == len(params)
            ):
                ok.add(id(node))
    return ok


def violations_in(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    rel = _rel(path)
    allowed = _forwarding_calls(tree) if rel in TRANSPORT_ADAPTERS else set()
    out: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "call"
            and id(node) not in allowed
        ):
            out.append(f"{rel}:{node.lineno}: .call( outside clogguard/safety")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for banned in BANNED_STRINGS:
                if banned in node.value:
                    out.append(f"{rel}:{node.lineno}: string mentions {banned!r}")
    return out


def test_no_printer_path_outside_safety() -> None:
    files = _scanned_files()
    assert any(_rel(p).startswith("service/") for p in files)  # the scan sees the service
    problems = [v for p in files for v in violations_in(p)]
    assert problems == [], "\n".join(problems)


def test_scanner_catches_violations(tmp_path: Path, monkeypatch: object) -> None:
    import pytest

    mp: pytest.MonkeyPatch = monkeypatch  # type: ignore[assignment]
    fake_pkg = tmp_path / "clogguard"
    (fake_pkg / "service").mkdir(parents=True)
    (fake_pkg / "service" / "bad.py").write_text(
        "async def f(link):\n    await link.call('printer.gcode.script', {'script': 'M112'})\n"
    )
    (fake_pkg / "service" / "link.py").write_text(
        "class L:\n"
        "    async def call(self, method, params):\n"
        "        return await self._c.call(method, params)\n"
        "    async def sneaky(self, m):\n"
        "        return await self._c.call(m, {})\n"
    )
    (fake_pkg / "sim").mkdir()
    (fake_pkg / "sim" / "server.py").write_text("M = 'printer.print.pause'\n")
    mp.setattr(__import__(__name__, fromlist=["PKG"]), "PKG", fake_pkg)
    found = [v for p in _scanned_files() for v in violations_in(p)]
    assert any("bad.py:2: .call(" in v for v in found)
    assert any("bad.py:2: string" in v for v in found)
    assert any("link.py:5: .call(" in v for v in found)  # non-forwarding call flagged
    assert not any("link.py:3" in v for v in found)  # pure forwarding allowed
    assert not any("sim/" in v for v in found)
