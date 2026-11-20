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
import subprocess
import sys
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


def test_cron_default_is_still_the_live_dir():
    # A child with the variable removed must see the live path, as cron does.
    code = (
        "import importlib.util, sys, pathlib\n"
        "out = []\n"
        "for p in sys.argv[1:]:\n"
        "    s = importlib.util.spec_from_file_location('m', p)\n"
        "    m = importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
        "    out += [str(v) for k, v in vars(m).items()\n"
        "            if k.startswith('DEPLOY_DATA') and isinstance(v, pathlib.Path)]\n"
        "print('\\n'.join(out))\n"
    )
    env = {k: v for k, v in os.environ.items() if k != ENV}
    r = subprocess.run([sys.executable, "-c", code, *map(str, SCRIPTS)], env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.split()
    assert len(lines) == len(SCRIPTS)
    assert set(lines) == {LIVE_DEFAULT}


def _docstring_nodes(tree):
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _env_default_nodes(tree):
    ids = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and len(node.args) == 2
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == ENV):
            ids.add(id(node.args[1]))
    return ids


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
