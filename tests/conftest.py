#!/usr/bin/env python3
"""共享测试辅助。

## 为什么会有这个文件（2026-08-09）

在此之前，快照类测试靠给 `stock` 字典塞一个 `consensus` 块来提供估值分母——
那走的是**注册表兜底**路径。用户当天拍板**删掉兜底**（宁可不显示，
也不显示一个用登记日旧预期算出的错 PE），于是那条路径没了，
七个测试文件的 fixture 同时失效。

正确做法是让测试走真实路径：把一致预期写成 `{key}-consensus.json`
落在被 patch 的 `DATA_DIR` 里，与生产完全一致。
"""

import atexit
import json
import os
import shutil
import tempfile
from pathlib import Path

# Before any test module imports a scripts/update_research_* module: send their
# live-site copies to a throwaway dir. See tests/test_no_live_site_writes.py.
# The temp ROOT is checked before anything is created in it, and cleanup is an
# atexit hook so it also runs when this file is imported as a plain module
# (pytest --noconftest + `from conftest import ...`).
_PROTECTED = tuple({Path("/var/www").resolve(), Path("/var/www/overview").resolve(),
                    (Path.home() / "docs-site").resolve()})


def _inside_protected(path: Path) -> bool:
    path = path.resolve()
    return any(p == path or p in path.parents for p in _PROTECTED)


if os.environ.get("_RESEARCH_DEPLOY_TMP_OWNER") != str(os.getpid()):
    _root = Path(tempfile.gettempdir())
    if _inside_protected(_root):
        raise RuntimeError(f"temp root {_root} is inside a protected tree; fix TMPDIR")
    _DEPLOY_TMP = Path(tempfile.mkdtemp(prefix="research-deploy-", dir=_root)).resolve()
    _PREVIOUS_DEPLOY_ENV = os.environ.get("RESEARCH_DEPLOY_DATA_DIR")
    os.environ["RESEARCH_DEPLOY_DATA_DIR"] = str(_DEPLOY_TMP)
    os.environ["_RESEARCH_DEPLOY_TMP_OWNER"] = str(os.getpid())

    def _cleanup(tmp=_DEPLOY_TMP, prev=_PREVIOUS_DEPLOY_ENV):
        shutil.rmtree(tmp, ignore_errors=True)
        if prev is None:
            os.environ.pop("RESEARCH_DEPLOY_DATA_DIR", None)
        else:
            os.environ["RESEARCH_DEPLOY_DATA_DIR"] = prev
        os.environ.pop("_RESEARCH_DEPLOY_TMP_OWNER", None)

    atexit.register(_cleanup)


def write_auto_consensus(data_dir, key: str, estimates: dict) -> None:
    """在测试用 DATA_DIR 里放一份自动源一致预期。

    `estimates` 形如 `{"2026E": {"profit_yuan": 8.6e10}}`——
    与 `{key}-consensus.json` 的 `estimates` 段同构。
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / f"{key}-consensus.json").write_text(
        json.dumps({"symbol": key, "estimates": estimates}, ensure_ascii=False),
        encoding="utf-8")
