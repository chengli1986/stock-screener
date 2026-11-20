"""No test may write into the live docs site (/var/www/overview).

2026-11-20 ~20:48 and 20:57 UTC: two full test runs copied a test-built
300308-ah-history.json (fx 0.86, as_of 2026-11-21 -- the test's own fixture
values) over the live file. `_build` in test_snapshot_ah_dual_listing.py patched
DATA_DIR but not DEPLOY_DATA_DIR (and fetched real closes), and build_snapshot ->
write_ah_history copies to DEPLOY_DATA_DIR. Patching per test leaves the next test
that forgets exposed, so every update_research_* script reads its deploy dir from
RESEARCH_DEPLOY_DATA_DIR (default: the live path, unchanged for cron) and
conftest points it at a verified temp dir before any test module is imported.
"""
import ast
import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LIVE_DEFAULT = "/var/www/overview/data"
ENV = "RESEARCH_DEPLOY_DATA_DIR"
SCRIPTS = sorted((ROOT / "scripts").glob("update_research_*.py"))


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(f"_live_guard_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _deploy_dirs(mod):
    return [v for k, v in vars(mod).items() if k.startswith("DEPLOY_DATA") and isinstance(v, Path)]


def test_there_are_scripts_to_check():
    assert len(SCRIPTS) >= 7


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.stem)
def test_deploy_dir_is_the_session_temp_dir(path):
    root = Path(os.environ[ENV]).resolve()
    assert not str(root).startswith("/var/www")
    dirs = _deploy_dirs(_load(path))
    assert dirs, f"{path.name}: no DEPLOY_DATA* constant found"
    for d in dirs:
        assert d.resolve() == root, f"{path.name} would write to {d}"


def _is_env_get(call) -> bool:
    """True for exactly os.environ.get(ENV, <default>)."""
    if not isinstance(call, ast.Call):
        return False
    f = call.func
    return (isinstance(f, ast.Attribute) and f.attr == "get"
            and isinstance(f.value, ast.Attribute) and f.value.attr == "environ"
            and isinstance(f.value.value, ast.Name) and f.value.value.id == "os"
            and len(call.args) == 2 and not call.keywords
            and isinstance(call.args[0], ast.Constant) and call.args[0].value == ENV)


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.stem)
def test_cron_default_is_still_the_live_dir(path):
    # Read, not run: each DEPLOY_DATA* must be exactly
    # Path(os.environ.get(ENV, "/var/www/overview/data")) at module level.
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id.startswith("DEPLOY_DATA")):
            v = node.value
            ok = (isinstance(v, ast.Call) and len(v.args) == 1 and not v.keywords
                  and _is_env_get(v.args[0]) and v.args[0].args[1].value == LIVE_DEFAULT)
            found.append((node.targets[0].id, ok))
    assert found and all(ok for _, ok in found), found


def _docstring_nodes(tree):
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _env_default_nodes(tree):
    return {id(n.args[1]) for n in ast.walk(tree) if isinstance(n, ast.Call) and _is_env_get(n)}


def test_no_script_names_the_live_site_outside_the_env_default():
    # Catches Path("/var/www/overview/..."), Path("/var/www") / "overview", open("/var/www/..."), ...
    offenders = []
    for p in sorted((ROOT / "scripts").glob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        allowed = _docstring_nodes(tree) | _env_default_nodes(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and "/var/www" in node.value and id(node) not in allowed):
                offenders.append(f"{p.name}:{node.lineno}")
    assert offenders == []
