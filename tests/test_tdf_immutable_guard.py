"""Repo-wide guard: no Python file may open a Bruker tdf unsafely.

A read-write ``sqlite3.connect`` on an ``analysis.tdf`` checkpoints any stale
mid-acquisition WAL into the file and truncates it — the frame index is gone
and the run is unrecoverable. 350 ``.d`` on the cluster were destroyed this way
before it was caught. A plain ``mode=ro`` open does not truncate but reads
*through* the stale WAL and drops an shm inside the raw ``.d``. Only
``?mode=ro&immutable=1`` is correct. See ``stan/tdf.py``.

Code review does not catch this reliably — it looks like an ordinary database
open — so this walks the whole repository instead and fails on any tdf open
that is not routed through a recognised constructor in ``stan/tdf.py``.

**The exemption is per call site, not per file.** A fixture that fabricates a
synthetic tdf genuinely needs to write one, and declares that by building its
URI with ``synthetic_tdf_write_uri()``. A file-name allowlist would grow with
every new fixture until it exempted most of the test suite; a named
constructor cannot, because every exempted open has to say so in its own
argument list.

To prove the guard is live rather than merely present, this file contains a
planted offender — a real unsafe open the scanner must flag — and a test that
fails if the scanner stops flagging it.
"""

from __future__ import annotations

import ast
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from stan.tdf import connect_tdf, synthetic_tdf_write_uri, tdf_read_uri

REPO_ROOT = Path(__file__).resolve().parents[1]

# Directories that are not this repo's source: vendored code, build output,
# caches, and the agent worktrees under .claude, which hold whole stale copies
# of the tree and would otherwise be scanned as if they were live code.
SKIP_DIRS = {
    ".git",
    ".claude",
    ".eggs",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "stan_proteomics.egg-info",
    "venv",
}

# The only call-argument forms allowed to reach sqlite3.connect for a tdf.
SAFE_CONSTRUCTORS = {"tdf_read_uri", "synthetic_tdf_write_uri"}

# Marks the deliberately unsafe line below. The repo-wide test ignores lines
# carrying it; test_guard_catches_planted_offender asserts the scanner still
# reports it, and test_plant_marker_is_confined_to_this_file asserts nobody
# else can use it as an escape hatch.
PLANT_MARKER = "GUARD-TEST-PLANTED-OFFENDER"

_TDF_RE = re.compile(r"tdf", re.IGNORECASE)


@dataclass(frozen=True)
class Violation:
    path: Path
    lineno: int
    source: str
    reason: str

    def __str__(self) -> str:
        rel = self.path.relative_to(REPO_ROOT)
        return f"{rel}:{self.lineno}: {self.reason}\n      {self.source.strip()}"


# ──────────────────────────────────────────────────────────────────────
#  Scanner
# ──────────────────────────────────────────────────────────────────────

def _sqlite_connect_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Local names that refer to sqlite3 / to sqlite3.connect in this module.

    Tracks aliases, because ``import sqlite3 as _sqlite3`` is real in this repo
    (``stan/community/scripts/run_one_v1.py``) and a guard that only looked for
    the literal text ``sqlite3.connect`` would sail straight past it.
    """
    modules: set[str] = set()
    bare: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqlite3":
                    modules.add(alias.asname or "sqlite3")
        elif isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            for alias in node.names:
                if alias.name == "connect":
                    bare.add(alias.asname or "connect")
    return modules, bare


def _is_connect_call(node: ast.Call, modules: set[str], bare: set[str]) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "connect":
        return isinstance(func.value, ast.Name) and func.value.id in modules
    return isinstance(func, ast.Name) and func.id in bare


def _database_arg(node: ast.Call) -> ast.expr | None:
    if node.args:
        return node.args[0]
    for kw in node.keywords:
        if kw.arg == "database":
            return kw.value
    return None


def _has_uri_true(node: ast.Call) -> bool:
    for kw in node.keywords:
        if kw.arg == "uri":
            return isinstance(kw.value, ast.Constant) and kw.value.value is True
    return False


def _assigns_a_tdf(scope: ast.AST, name: str) -> bool:
    """True if ``name`` is assigned from something naming analysis.tdf here.

    Catches the indirect form ``p = d / "analysis.tdf"`` followed later by
    ``sqlite3.connect(p)``, where the connect call's own argument gives away
    nothing.
    """
    for node in ast.walk(scope):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        if node.value is not None and _mentions_tdf(ast.dump(node.value)):
            return True
    return False


def _mentions_tdf(text: str) -> bool:
    return bool(_TDF_RE.search(text))


def _targets_a_tdf(
    call: ast.Call,
    arg: ast.expr,
    source: str,
    enclosing: ast.AST | None,
    enclosing_name: str,
) -> bool:
    """Decide whether this connect call's target is a Bruker tdf.

    Deliberately errs toward flagging: a false positive costs one call to
    ``connect_tdf``; a false negative costs somebody's raw data.
    """
    arg_src = ast.get_source_segment(source, arg) or ""
    if _mentions_tdf(arg_src):
        return True
    # A helper named for what it builds — _make_valid_tdf(path) — hides the
    # tdf in the function name while the argument is just `path`.
    if _mentions_tdf(enclosing_name):
        return True
    if isinstance(arg, ast.Name) and enclosing is not None:
        return _assigns_a_tdf(enclosing, arg.id)
    return False


def _verdict(call: ast.Call, arg: ast.expr) -> str | None:
    """Reason this open is unsafe, or None if it is fine."""
    if isinstance(arg, ast.Call):
        func = arg.func
        ctor = (
            func.attr if isinstance(func, ast.Attribute)
            else func.id if isinstance(func, ast.Name)
            else ""
        )
        if ctor in SAFE_CONSTRUCTORS:
            if not _has_uri_true(call):
                return f"{ctor}() builds a URI but uri=True was not passed"
            return None
    if not _has_uri_true(call):
        return (
            "read-write open of a Bruker tdf — this checkpoints any stale WAL "
            "and truncates the frame index; use stan.tdf.connect_tdf()"
        )
    return (
        "tdf URI not built by stan.tdf — hand-built URIs miss immutable=1 and "
        "break on a .d path containing ? # or %; use stan.tdf.connect_tdf()"
    )


def scan_source(source: str, path: Path) -> list[Violation]:
    """Report every unsafe tdf open in one Python source string."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    modules, bare = _sqlite_connect_names(tree)
    if not modules and not bare:
        return []

    lines = source.splitlines()
    scopes: list[tuple[ast.AST, str]] = []
    violations: list[Violation] = []

    def visit(node: ast.AST) -> None:
        pushed = False
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scopes.append((node, node.name))
            pushed = True
        if isinstance(node, ast.Call) and _is_connect_call(node, modules, bare):
            arg = _database_arg(node)
            if arg is not None:
                enclosing, enclosing_name = scopes[-1] if scopes else (tree, "")
                if _targets_a_tdf(node, arg, source, enclosing, enclosing_name):
                    reason = _verdict(node, arg)
                    if reason is not None:
                        line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
                        violations.append(Violation(path, node.lineno, line, reason))
        for child in ast.iter_child_nodes(node):
            visit(child)
        if pushed:
            scopes.pop()

    visit(tree)
    return violations


def scan_file(path: Path) -> list[Violation]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return scan_source(source, path)


def _python_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for path in root.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        found.append(path)
    return found


def scan_repo(root: Path = REPO_ROOT) -> list[Violation]:
    violations: list[Violation] = []
    for path in _python_files(root):
        violations.extend(scan_file(path))
    return violations


def _is_planted(v: Violation) -> bool:
    return PLANT_MARKER in v.source


# ──────────────────────────────────────────────────────────────────────
#  The planted offender
# ──────────────────────────────────────────────────────────────────────

def _planted_offender_never_called(tdf):  # pragma: no cover - never executed
    """A genuinely unsafe tdf open, kept here so the guard has to catch it.

    If the scanner ever stops reporting the line below — a regex loosened, an
    AST walk that misses a nested scope, a skip-list that swallows tests/ — the
    repo-wide check would start passing vacuously. This makes that failure
    loud instead.
    """
    return sqlite3.connect(str(tdf))  # GUARD-TEST-PLANTED-OFFENDER


# ──────────────────────────────────────────────────────────────────────
#  Tests
# ──────────────────────────────────────────────────────────────────────

def test_no_unsafe_tdf_opens_in_repo() -> None:
    """Every tdf open in the repo goes through stan/tdf.py."""
    real = [v for v in scan_repo() if not _is_planted(v)]
    assert not real, (
        f"{len(real)} unsafe Bruker tdf open(s) — a read-write or plain "
        "mode=ro open can destroy a .d. Route each through "
        "stan.tdf.connect_tdf():\n\n  " + "\n  ".join(str(v) for v in real)
    )


def test_guard_catches_planted_offender() -> None:
    """The scanner reports the deliberately unsafe open in this file."""
    planted = [v for v in scan_file(Path(__file__)) if _is_planted(v)]
    assert len(planted) == 1, (
        "the planted offender was not caught — this guard is no longer "
        f"guarding anything. Found: {[str(v) for v in planted]}"
    )
    assert "read-write" in planted[0].reason


def test_plant_marker_is_confined_to_this_file() -> None:
    """Nobody else may use the plant marker to silence the guard."""
    users = [
        p for p in _python_files(REPO_ROOT)
        if PLANT_MARKER in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert users == [Path(__file__)], (
        f"the plant marker escaped this file and is now an exemption "
        f"backdoor: {[str(p) for p in users]}"
    )


@pytest.mark.parametrize(
    "snippet, expect_flagged",
    [
        ("import sqlite3\ncon = sqlite3.connect(str(tdf))\n", True),
        ('import sqlite3\ncon = sqlite3.connect(f"file:{tdf}?mode=ro", uri=True)\n', True),
        (
            'import sqlite3\ncon = sqlite3.connect(f"file:{tdf}?mode=ro&immutable=1", uri=True)\n',
            True,  # right parameters, wrong construction: breaks on ? # % in the path
        ),
        ("import sqlite3 as _s\ncon = _s.connect(str(tdf))\n", True),
        ("from sqlite3 import connect\ncon = connect(str(tdf))\n", True),
        ('import sqlite3\np = d / "analysis.tdf"\ncon = sqlite3.connect(p)\n', True),
        (
            "import sqlite3\nfrom stan.tdf import tdf_read_uri\n"
            "con = sqlite3.connect(tdf_read_uri(tdf), uri=True)\n",
            False,
        ),
        (
            "import sqlite3\nfrom stan.tdf import synthetic_tdf_write_uri\n"
            "con = sqlite3.connect(synthetic_tdf_write_uri(tdf), uri=True)\n",
            False,
        ),
        # Not a tdf: STAN's own database must stay read-write.
        ("import sqlite3\ncon = sqlite3.connect(str(db_path))\n", False),
    ],
)
def test_scanner_verdicts(snippet: str, expect_flagged: bool) -> None:
    """The scanner's rules, stated as cases rather than left implicit."""
    flagged = bool(scan_source(snippet, Path("snippet.py")))
    assert flagged is expect_flagged, (
        f"expected flagged={expect_flagged} for:\n{snippet}"
    )


def test_safe_constructor_without_uri_true_is_flagged() -> None:
    """tdf_read_uri() passed as a plain path is still wrong, and caught."""
    snippet = (
        "import sqlite3\nfrom stan.tdf import tdf_read_uri\n"
        "con = sqlite3.connect(tdf_read_uri(tdf))\n"
    )
    violations = scan_source(snippet, Path("snippet.py"))
    assert len(violations) == 1
    assert "uri=True" in violations[0].reason


def test_exempted_constructor_round_trips(tmp_path: Path) -> None:
    """The exemption is a working call site, not a name the guard humours.

    Builds a synthetic tdf through the exempted writer, reads it back through
    the immutable reader, and checks the immutable open left no WAL or shm
    behind in the .d.
    """
    d_dir = tmp_path / "round trip #1 100%.d"  # URI metacharacters, on purpose
    d_dir.mkdir()
    tdf = d_dir / "analysis.tdf"

    con = sqlite3.connect(synthetic_tdf_write_uri(tdf), uri=True)
    con.execute("CREATE TABLE Frames (Id INTEGER, MsmsType INTEGER)")
    con.execute("INSERT INTO Frames VALUES (1, 9)")
    con.commit()
    con.close()

    assert "%23" in tdf_read_uri(tdf), "the # in the path must be escaped"

    with connect_tdf(tdf) as reader:
        assert reader.execute("SELECT MsmsType FROM Frames").fetchone()[0] == 9

    leftovers = sorted(p.name for p in d_dir.iterdir() if p.name != "analysis.tdf")
    assert leftovers == [], f"the immutable read wrote into the .d: {leftovers}"
