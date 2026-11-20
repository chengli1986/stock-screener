"""No test may write into the live docs site (/var/www/overview).

2026-11-20 ~20:48 and 20:57 UTC: two full test runs copied a test-built
300308-ah-history.json (fx 0.86, as_of 2026-11-21 -- the test's own fixture
values) over the live file. `_build` in test_snapshot_ah_dual_listing.py patched
DATA_DIR but not DEPLOY_DATA_DIR, and build_snapshot -> write_ah_history copies to
DEPLOY_DATA_DIR. Patching per test leaves the next test that forgets exposed, so
every update_research_* script reads its deploy dir from RESEARCH_DEPLOY_DATA_DIR
(default: the live path) and conftest points it at a temp dir before any test
module is imported.
"""
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LIVE = Path("/var/www/overview")
SCRIPTS = sorted((ROOT / "scripts").glob("update_research_*.py"))


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(f"_live_guard_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_there_are_scripts_to_check():
    assert len(SCRIPTS) >= 7


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.stem)
def test_deploy_dir_is_redirected_under_pytest(path):
    mod = _load(path)
    dirs = [v for k, v in vars(mod).items()
            if k.startswith("DEPLOY_DATA") and isinstance(v, Path)]
    assert dirs, f"{path.name}: no DEPLOY_DATA* constant found"
    for d in dirs:
        assert LIVE not in (d, *d.parents), f"{path.name} would write to {d}"


def test_no_script_hard_codes_the_live_path_outside_the_env_default():
    # A new `Path("/var/www/overview/...")` would bypass the redirect.
    pat = re.compile(r"""Path\(\s*["']/var/www/overview""")
    offenders = [p.name for p in (ROOT / "scripts").glob("*.py") if pat.search(p.read_text())]
    assert offenders == []
