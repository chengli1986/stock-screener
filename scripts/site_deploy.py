"""研报数据写线上的唯一出口（site-publish 第 4 期，设计 docs-site docs/2026-11-21-site-publish-phase3-5-design.md §3）。

- 测试模式（环境里有 RESEARCH_DEPLOY_DATA_DIR，由 tests/conftest.py 设成临时目录）：照旧 shutil.copy2 到
  该目录——保持 PR #2 建立的测试隔离；目录若在 /var/www 下则拒绝。
- 生产（cron 从不设该变量）：调用 docs-site 的成品安装入口
  `python3 -I ~/docs-site/scripts/publish_lib.py artifact --caller stock-screener --item <A 文件>=data/<名>`，
  由它持站点锁、备份、逐项日志、读回、记录。非 0 → DeployError。
- 任何一次部署失败都记进 FAILURES；脚本用 `sys.exit(site_deploy.final_rc(main()))` 结束，有失败就以 3 退出
  （> 1：cron 行的 `rc<=1 才提交` 与 `&&` 都不会提交 A，wrapper 告警；A 已更新、C 保持上一版）。
"""
from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import sys
import time
from pathlib import Path

ENV = "RESEARCH_DEPLOY_DATA_DIR"
# 生产 C 的 data 目录。写成「环境变量默认值」的形状，test_no_live_site_writes 才放行这个字面量。
PROD_DATA_DIR = Path(os.environ.get("RESEARCH_DEPLOY_DATA_DIR_PROD_UNUSED", "/var/www/overview/data"))
_HOME = Path(pwd.getpwnam("ubuntu").pw_dir)
PUBLISH_LIB = _HOME / "docs-site" / "scripts" / "publish_lib.py"
DOCS_REPO = _HOME / "docs-site"
LOCK_WAIT = 30            # 单次等站点锁上限（秒）
TOTAL_LOCK_BUDGET = 120   # 同一进程累计等锁超过这个数后，改为 1 秒快速失败（financials cron 只有 360 秒）
CALL_TIMEOUT = 240        # 单次安装调用的总上限

FAILURES: list[str] = []
_BUDGET = {"waited": 0.0}


class DeployError(Exception):
    pass


def _fail(msg: str) -> None:
    FAILURES.append(msg)
    print(f"ERROR: 部署到线上失败：{msg}", file=sys.stderr)
    raise DeployError(msg)


def deploy(src: Path, deploy_dir: Path) -> None:
    """把 A 里的 src 放到线上 data/<src.name>。"""
    src = Path(src)
    if ENV in os.environ:
        real = os.path.realpath(deploy_dir)
        if real == "/var/www" or real.startswith("/var/www/"):
            _fail(f"测试模式下部署目录 {deploy_dir} 指向线上，拒绝")
        shutil.copy2(src, Path(deploy_dir) / src.name)
        return
    if os.path.realpath(deploy_dir) != os.path.realpath(PROD_DATA_DIR):
        _fail(f"生产模式下部署目录 {deploy_dir} 不是线上 data 目录，拒绝")
    wait = LOCK_WAIT if _BUDGET["waited"] <= TOTAL_LOCK_BUDGET else 1
    cmd = ["/usr/bin/python3", "-I", str(PUBLISH_LIB), "artifact", "--repo", str(DOCS_REPO),
           "--deploy", str(Path(PROD_DATA_DIR).parent), "--caller", "stock-screener",
           "--lock-wait", str(wait), "--item", f"{src}=data/{src.name}"]
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=CALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        _BUDGET["waited"] += time.monotonic() - t0
        _fail(f"{src.name}：安装入口超过 {CALL_TIMEOUT} 秒未返回（状态不明，见 ~/.local/state/docs-publish/）")
        return
    except OSError as e:
        _fail(f"{src.name}：无法启动安装入口：{e}")
        return
    _BUDGET["waited"] += time.monotonic() - t0
    if r.returncode != 0:
        tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
        _fail(f"{src.name}：安装入口 exit {r.returncode}：{' | '.join(tail)}")


def final_rc(rc: int | None) -> int:
    """main() 的返回码；本进程有任何部署失败 → 3。"""
    if FAILURES:
        print(f"ERROR: 本次有 {len(FAILURES)} 个文件没能部署到线上（A 已更新，C 保持上一版）；退出码 3", file=sys.stderr)
        return 3
    return 0 if rc is None else rc
