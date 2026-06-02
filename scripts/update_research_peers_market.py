#!/usr/bin/env python3
"""
update_research_peers_market.py — 刷新个股研报 §6 同业横向对比表的「市值与股价表现」行。

数据源（均腾讯财经，避开 push2 偶发 502）：
  - qt.gtimg.cn 实时行情：现价 + 总市值(亿)。A 股 sh/sz 前缀，港股 hk 前缀。
  - K 线 fqkline：近 252 交易日计算 1 年涨幅。

读取 config/research_peers.json，对每个注册报告写出
  docs-site/data/{code}-peers-market.json 并部署到 /var/www/overview/data/。

任意 peer 抓取失败仅记 WARN（保留该 peer 静态值），整体非空即 exit 0；
全部失败 exit 1（由 cron-wrapper 告警）。
"""
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BJT = timezone(timedelta(hours=8))
REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config" / "research_peers.json"
DATA_DIR = REPO.parent / "docs-site" / "data"
DEPLOY_DATA_DIR = Path("/var/www/overview/data")

_QT_URL = "https://qt.gtimg.cn/q={code}"
_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def _get_with_retry(url: str, *, params=None, headers=None, timeout=15,
                    label="", attempts=3) -> requests.Response:
    last = None
    for i in range(1, attempts + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001 — 重试所有网络异常
            last = e
            if i < attempts:
                print(f"WARN: {label} {type(e).__name__}, retry ({i+1}/{attempts}) in {i}s...",
                      file=sys.stderr)
                time.sleep(i)
    raise last  # type: ignore[misc]


def fetch_quote(tencent_code: str) -> dict:
    """腾讯 qt 实时行情 → {price, market_cap_yi}。字段 [3]=现价, [45]=总市值(亿)。"""
    r = _get_with_retry(_QT_URL.format(code=tencent_code),
                        headers={"Referer": "https://gu.qq.com/"},
                        label=f"[{tencent_code}] qt")
    r.encoding = "gbk"
    txt = r.text.strip()
    if "=" not in txt:
        raise ValueError(f"qt malformed for {tencent_code}: {txt[:60]}")
    fields = txt.split("=", 1)[1].strip().strip('";').split("~")
    if len(fields) < 46 or not fields[3]:
        raise ValueError(f"qt too few fields for {tencent_code}: len={len(fields)}")
    return {
        "price": round(float(fields[3]), 2),
        "market_cap_yi": round(float(fields[45])) if fields[45] else None,
    }


def fetch_year_return(tencent_code: str) -> float | None:
    """腾讯 K 线 → 近 252 交易日 1 年涨幅%。数据不足返回 None。"""
    end_dt = datetime.now(BJT)
    start_dt = end_dt - timedelta(days=400)
    r = _get_with_retry(
        _KLINE_URL,
        params={"param": f"{tencent_code},day,{start_dt:%Y-%m-%d},{end_dt:%Y-%m-%d},320,qfq"},
        headers={"Referer": "https://gu.qq.com/"},
        label=f"[{tencent_code}] kline",
    )
    d = r.json()
    sd = d.get("data", {}).get(tencent_code, {})
    rows = sd.get("qfqday") or sd.get("day") or []
    closes = [float(row[2]) for row in rows if len(row) >= 3]
    if len(closes) < 60:
        return None
    window = closes[-252:] if len(closes) >= 252 else closes
    if window[0] <= 0:
        return None
    return round((window[-1] / window[0] - 1) * 100, 1)


def build_peers(code: str, cfg: dict) -> dict:
    out_peers = []
    ok = 0
    for p in cfg["peers"]:
        tc = p["tencent"]
        rec = {"code": p["code"], "name": p["name"], "market": p["market"],
               "currency": p["currency"], "price": None,
               "market_cap_yi": None, "year_return_pct": None}
        try:
            q = fetch_quote(tc)
            rec["price"] = q["price"]
            rec["market_cap_yi"] = q["market_cap_yi"]
            try:
                rec["year_return_pct"] = fetch_year_return(tc)
            except Exception as e:  # noqa: BLE001
                print(f"WARN: [{p['name']}] year_return failed: {e}", file=sys.stderr)
            ok += 1
            print(f"  [{p['name']:8s}] {p['currency']} {rec['price']}  "
                  f"市值 {rec['market_cap_yi']} 亿  1年 {rec['year_return_pct']}%")
        except Exception as e:  # noqa: BLE001
            print(f"WARN: [{p['name']}] quote failed, 保留静态值: {e}", file=sys.stderr)
        out_peers.append(rec)
    return {
        "as_of": datetime.now(BJT).strftime("%Y-%m-%d"),
        "table_label": cfg.get("table_label", ""),
        "peers": out_peers,
        "_ok": ok,
    }


def write_and_deploy(code: str, payload: dict) -> None:
    ok = payload.pop("_ok")
    if ok == 0:
        raise RuntimeError(f"[{code}] all peers failed; not writing")
    out = DATA_DIR / f"{code}-peers-market.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(f"  wrote {out}")
    if DEPLOY_DATA_DIR.is_dir():
        dst = DEPLOY_DATA_DIR / f"{code}-peers-market.json"
        dst.write_text(out.read_text())
        print(f"  deployed {dst}")


def main() -> int:
    cfg_all = json.loads(CONFIG.read_text())
    failures = []
    for code, cfg in cfg_all.items():
        if code.startswith("_"):
            continue
        print(f"[{code}] fetching {len(cfg['peers'])} peers...")
        try:
            payload = build_peers(code, cfg)
            write_and_deploy(code, payload)
        except Exception as e:  # noqa: BLE001
            print(f"ERROR [{code}]: {e}", file=sys.stderr)
            failures.append(code)
    if failures:
        print(f"FAILED: {failures}", file=sys.stderr)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
