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


_YF_SUFFIX = {"jp": ".T", "kr": ".KS"}
_YF_TIMEOUT_S = 30


def yf_ticker_of(tencent_code: str) -> str | None:
    """海外股 → yfinance ticker；A 股/港股返回 None（腾讯供数充足，不必换源）。

    ★腾讯 fqkline 对海外股只返回 1–2 根 K 线（2026-08-06 实测：usAVGO 2 根、
    jp6503 1 根、kr005930 1 根；A 股/港股 261 根），所以年涨幅必须换源。
    """
    code = str(tencent_code)
    prefix, body = code[:2].lower(), code[2:]
    if prefix == "us":
        return body
    if prefix in _YF_SUFFIX:
        return body + _YF_SUFFIX[prefix]
    return None


def _yf_year_return(ticker: str) -> float | None:
    """yfinance 近一年涨幅%（带硬超时——其内部 requests 无 timeout）。"""
    import threading

    box: dict = {}

    def _work():
        try:
            import yfinance as yf
            hist = yf.Ticker(ticker).history(period="1y")
            if len(hist) >= 60:
                first, last = float(hist["Close"].iloc[0]), float(hist["Close"].iloc[-1])
                if first > 0:
                    box["v"] = round((last / first - 1) * 100, 1)
        except Exception as e:  # noqa: BLE001
            print(f"WARN: [{ticker}] yfinance year_return failed: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)

    t = threading.Thread(target=_work, daemon=True)
    t.start()
    t.join(_YF_TIMEOUT_S)
    if t.is_alive():
        print(f"WARN: [{ticker}] yfinance year_return 超时 >{_YF_TIMEOUT_S}s", file=sys.stderr)
    return box.get("v")


def fetch_year_return(tencent_code: str) -> float | None:
    """1 年涨幅%：A 股/港股走腾讯，海外股腾讯不供数时回落 yfinance。"""
    got = _tencent_year_return(tencent_code)
    if got is not None:
        return got
    ticker = yf_ticker_of(tencent_code)
    return _yf_year_return(ticker) if ticker else None


def _tencent_year_return(tencent_code: str) -> float | None:
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


# ── 跨市场币种换算 ─────────────────────────────────────────────────────────────
#
# 腾讯 qt 的 [45] 字段是**本币计价的「亿」**：茅台 16,358 亿人民币、Broadcom
# 19,900 亿美元、三星 15,135,928 亿韩元、三菱 119,552 亿日元。直接并列比较是错的。
# （2026-08-06 实测：美股/港股/A股/韩股/日股均有数据，**台股 tw2330 无数据**。）
#
# 统一换算到 CNY —— 观察池以 A 股为主，读者的锚点是人民币。

_PREFIX_CCY = {"sh": "CNY", "sz": "CNY", "hk": "HKD", "us": "USD",
               "jp": "JPY", "kr": "KRW"}
_FX_PAIRS = {"USD": "USDCNY=X", "HKD": "HKDCNY=X",
             "JPY": "JPYCNY=X", "KRW": "KRWCNY=X"}
_FX_TIMEOUT_S = 30


def currency_of(tencent_code: str) -> str | None:
    """从腾讯代码前缀推币种；未知前缀返回 None（**不默认按人民币**）。"""
    return _PREFIX_CCY.get(str(tencent_code)[:2].lower())


def convert_cap(cap_native: float | None, currency: str, fx: dict) -> float | None:
    """本币「亿」→ 人民币「亿」。

    ★缺汇率时**抛错而不是原样返回** —— 原样返回正是把 19,900 亿美元
    印成 19,900 亿人民币的那条路径（与 8-04 港股 PS 换算的处置口径一致）。
    """
    if cap_native is None:
        return None
    if currency == "CNY":
        return cap_native
    rate = fx.get(currency)
    if not rate:
        raise ValueError(f"缺少 {currency}→CNY 汇率，拒绝按 1:1 处理")
    return round(cap_native * rate)   # 亿元级，小数无意义（原始值另存 native）


def fetch_fx_rates(currencies: set[str]) -> dict:
    """取各币种对 CNY 汇率（yfinance，带硬超时——它内部 requests 无 timeout）。"""
    import threading

    need = {c for c in currencies if c != "CNY" and c in _FX_PAIRS}
    out: dict[str, float] = {}
    if not need:
        return out

    def _work():
        import yfinance as yf
        for ccy in need:
            try:
                hist = yf.Ticker(_FX_PAIRS[ccy]).history(period="5d")
                if not hist.empty:
                    out[ccy] = float(hist["Close"].iloc[-1])
            except Exception as e:  # noqa: BLE001
                print(f"WARN: {ccy} 汇率抓取失败: {type(e).__name__}: {e}", file=sys.stderr)

    t = threading.Thread(target=_work, daemon=True)
    t.start()
    t.join(_FX_TIMEOUT_S)
    if t.is_alive():
        print(f"WARN: 汇率抓取超时 >{_FX_TIMEOUT_S}s", file=sys.stderr)
    for ccy, rate in out.items():
        print(f"  汇率 {ccy}→CNY = {rate:.6f}")
    return out


def build_peer_record(p: dict, price, cap_native, year_return_pct, fx: dict) -> dict:
    """组装单个 peer。换算后同时保留原币值与所用汇率，页面才能说清这是换算值。"""
    ccy = p.get("currency") or currency_of(p["tencent"]) or "CNY"
    rec = {
        "code": p["code"], "name": p["name"], "market": p["market"],
        # 页面就地水合按腾讯代码匹配单元格（data-pm-cap="sz000858"）——
        # 展示用的 code 是「000858.SZ」这类可读格式，两者不是一回事，必须都落盘。
        "tencent": p["tencent"],
        "currency": ccy, "price": price,
        "market_cap_yi_native": cap_native,
        "market_cap_yi": None,
        "fx_to_cny": 1.0 if ccy == "CNY" else None,
        "year_return_pct": year_return_pct,
    }
    try:
        rec["market_cap_yi"] = convert_cap(cap_native, ccy, fx)
        if ccy != "CNY":
            rec["fx_to_cny"] = fx.get(ccy)
    except ValueError as e:
        rec["note"] = str(e)
    return rec


def build_peers(code: str, cfg: dict, fx: dict) -> dict:
    out_peers = []
    ok = 0
    for p in cfg["peers"]:
        tc = p["tencent"]
        price = cap_native = yr = None
        try:
            q = fetch_quote(tc)
            price, cap_native = q["price"], q["market_cap_yi"]
            try:
                yr = fetch_year_return(tc)
            except Exception as e:  # noqa: BLE001
                print(f"WARN: [{p['name']}] year_return failed: {e}", file=sys.stderr)
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"WARN: [{p['name']}] quote failed, 保留静态值: {e}", file=sys.stderr)
        rec = build_peer_record(p, price, cap_native, yr, fx)
        if price is not None:
            conv = "" if rec["currency"] == "CNY" else f" → ¥{rec['market_cap_yi']} 亿" \
                if rec["market_cap_yi"] is not None else " → 换算失败"
            print(f"  [{p['name']:8s}] {rec['currency']} {price}  "
                  f"市值 {cap_native} 亿{conv}  1年 {yr}%")
        out_peers.append(rec)
    return {
        "as_of": datetime.now(BJT).strftime("%Y-%m-%d"),
        "table_label": cfg.get("table_label", ""),
        "cap_unit": "亿元人民币（非人民币标的按当日汇率换算，原值见 market_cap_yi_native）",
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
    wanted = {p.get("currency") or currency_of(p["tencent"])
              for code, cfg in cfg_all.items() if not code.startswith("_")
              for p in cfg["peers"]}
    fx = fetch_fx_rates({c for c in wanted if c})

    failures = []
    for code, cfg in cfg_all.items():
        if code.startswith("_"):
            continue
        print(f"[{code}] fetching {len(cfg['peers'])} peers...")
        try:
            payload = build_peers(code, cfg, fx)
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
