"""site_deploy：研报数据写线上改走 docs-site 的成品安装入口（site-publish 第 4 期）。

测试模式（RESEARCH_DEPLOY_DATA_DIR 由 conftest 设成临时目录）照旧复制；生产分支只用替身验证命令行与退出码，
绝不真起安装入口、不碰 /var/www。
"""
from __future__ import annotations

import ast
import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import site_deploy  # noqa: E402

SCRIPTS = sorted((ROOT / "scripts").glob("update_research_*.py"))


@pytest.fixture(autouse=True)
def _reset():
    site_deploy.FAILURES.clear()
    site_deploy._BUDGET["waited"] = 0.0
    yield
    site_deploy.FAILURES.clear()


def test_test_mode_copies_into_temp_dir(tmp_path):
    src = tmp_path / "a" / "300308-snapshot.json"
    src.parent.mkdir()
    src.write_text("{}")
    dst = Path(os.environ["RESEARCH_DEPLOY_DATA_DIR"])
    site_deploy.deploy(src, dst)
    assert (dst / src.name).read_text() == "{}"


def test_test_mode_refuses_live_dir(tmp_path, monkeypatch):
    src = tmp_path / "x.json"
    src.write_text("{}")
    with pytest.raises(site_deploy.DeployError):
        site_deploy.deploy(src, Path("/var/www/overview/data"))
    assert site_deploy.FAILURES


def _prod(monkeypatch, rc=0, record=None):
    monkeypatch.delenv("RESEARCH_DEPLOY_DATA_DIR", raising=False)
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="boom" if rc else "")
    monkeypatch.setattr(site_deploy.subprocess, "run", fake_run)
    return calls


def test_prod_mode_calls_artifact(tmp_path, monkeypatch):
    calls = _prod(monkeypatch)
    src = tmp_path / "02513-news.json"
    src.write_text("{}")
    site_deploy.deploy(src, site_deploy.PROD_DATA_DIR)
    cmd, kw = calls[0]
    assert cmd[:3] == ["/usr/bin/python3", "-I", str(site_deploy.PUBLISH_LIB)]
    assert cmd[3] == "artifact"
    assert ["--caller", "stock-screener"] == cmd[cmd.index("--caller"):cmd.index("--caller") + 2]
    assert f"{src}=data/02513-news.json" in cmd
    assert cmd[cmd.index("--lock-wait") + 1] == "30"
    assert kw.get("timeout") and kw["timeout"] >= 60
    assert not site_deploy.FAILURES


def test_prod_mode_refuses_other_dir(tmp_path, monkeypatch):
    calls = _prod(monkeypatch)
    src = tmp_path / "x.json"
    src.write_text("{}")
    with pytest.raises(site_deploy.DeployError):
        site_deploy.deploy(src, tmp_path)
    assert calls == []


@pytest.mark.parametrize("rc", [2, 4, 5, 6, 7, -15])
def test_prod_mode_nonzero_raises_and_records(tmp_path, monkeypatch, rc):
    _prod(monkeypatch, rc=rc)
    src = tmp_path / "300308-snapshot.json"
    src.write_text("{}")
    with pytest.raises(site_deploy.DeployError):
        site_deploy.deploy(src, site_deploy.PROD_DATA_DIR)
    assert site_deploy.FAILURES and site_deploy.final_rc(0) == 3 and site_deploy.final_rc(1) == 3


def test_prod_mode_timeout_counts_as_failure(tmp_path, monkeypatch):
    monkeypatch.delenv("RESEARCH_DEPLOY_DATA_DIR", raising=False)

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
    monkeypatch.setattr(site_deploy.subprocess, "run", slow)
    src = tmp_path / "300308-snapshot.json"
    src.write_text("{}")
    with pytest.raises(site_deploy.DeployError):
        site_deploy.deploy(src, site_deploy.PROD_DATA_DIR)
    assert site_deploy.final_rc(0) == 3


def test_budget_shrinks_lock_wait(tmp_path, monkeypatch):
    calls = _prod(monkeypatch)
    site_deploy._BUDGET["waited"] = site_deploy.TOTAL_LOCK_BUDGET + 1
    src = tmp_path / "300308-snapshot.json"
    src.write_text("{}")
    site_deploy.deploy(src, site_deploy.PROD_DATA_DIR)
    cmd = calls[0][0]
    assert cmd[cmd.index("--lock-wait") + 1] == "1"


def test_final_rc_without_failures_passes_through():
    assert site_deploy.final_rc(0) == 0 and site_deploy.final_rc(1) == 1 and site_deploy.final_rc(2) == 2


# ---------- 7 个脚本不再直写部署目录 ----------

def _deploy_names(tree) -> set[str]:
    return {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets
            if isinstance(t, ast.Name) and t.id.startswith("DEPLOY_DATA")}


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.stem)
def test_no_direct_write_to_deploy_dir(path):
    """不得再出现 copy*/write_text/open('w') 写到 DEPLOY_DATA* 或 deploy_dir 下（只能经 site_deploy.deploy）。"""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = _deploy_names(tree) | {"deploy_dir"}
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            fname = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
            text = ast.unparse(node)
            if fname in ("copy2", "copy", "copyfile", "move") and any(n in text for n in names):
                bad.append(text)
            if fname in ("write_text", "write_bytes") and isinstance(f, ast.Attribute):
                base = ast.unparse(f.value)
                if any(n in base for n in names) or base in ("dst", "dst_path"):
                    bad.append(text)
            if fname == "mkdir" and isinstance(f, ast.Attribute) and any(n == ast.unparse(f.value) for n in names):
                bad.append(text)
    assert bad == [], f"{path.name} 仍直写部署目录：{bad}"


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.stem)
def test_exit_goes_through_final_rc(path):
    src = path.read_text(encoding="utf-8")
    assert "sys.exit(site_deploy.final_rc(main()))" in src, path.name
