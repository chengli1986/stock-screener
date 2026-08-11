#!/usr/bin/env python3
"""test_broker_forecasts.py — 逐家机构预测落盘（为「谁更准」留出可回测的记录）

## 2026-08-10 起因

用户指出 ETNet 对港股不可替代：**yfinance 只给营收 + EPS，净利只有 ETNet 有**
（EPS 因各家股本假设不同已弃用）。顺着查发现三处：

1. **港股逐家明细算完就扔** —— `robust_stats(et["brokers"])` 之后，那 13 家
   带机构名与发布日期的净利预测再无去处。
2. **A 股存了但口径不一致**（⚠ 已知，**刻意未修**）—— `record["brokers"]` 是位置式年份
   （`profit_y2/y3`）**且 limit=20 硬截断**：茅台存 20 条却按 **31 家**算统计量，
   宁德存 20 条却按 27 家算。用户 2026-08-10 明确「ETNet/yfinance 只服务港股，别动 A 股」，
   故本次只做港股；**这份回测记录因此只覆盖港股**（A 股的扁平 list 形状会被拒收）。
3. **两边都没人显示** —— 页面唯一读 `brokers` 的那段 JS 当天早些时候被删掉了。

## 为什么要单独一份 append-only 记录

`consensus.json` 是覆盖写的。摩根大通这周说 −36 亿、下周改口 −30 亿，上一版就没了。
而「谁的预测更准」只能在**实际值揭晓后回头比**——到那时记录已经不在。

不塞进 `consensus-history.jsonl`：那是每周一行的聚合观测；逐家预测**不需要按周快照**，
每条自带 `published`，券商不改口就不会变。按 (org, year, published) 去重，
重跑不重复，改口自然产生新行。

## ⚠ 这份记录不回答「谁更乐观」，它是为了回答「谁更准」

2026-08-10 实测智谱：外资 7 家三年均值都比中资 6 家乐观（2028E 差 18 亿、方向都不同），
但**「更乐观」不等于「更可靠」**。在 2026 年报出来之前，准确度问题无法回答——
存下来，才有机会回答。
"""

import importlib.util
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location(
    "update_research_consensus", _ROOT / "scripts" / "update_research_consensus.py")
urc = importlib.util.module_from_spec(_spec)
sys.modules["update_research_consensus"] = urc
_spec.loader.exec_module(urc)


def _rec(brokers, fetched="2026-08-10T08:45:00+08:00", source="etnet"):
    return {"symbol": "02513", "fetched_at": fetched,
            "broker_source": source, "brokers": brokers}


def _read(p):
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


# ── 基本落盘 ─────────────────────────────────────────────────────────────────


class TestAppend:
    def test_writes_one_row_per_firm_year(self, tmp_path):
        rec = _rec({"2026E": [{"org": "摩根大通", "published": "2026-07-21", "value": -3.605e9},
                              {"org": "瑞银", "published": "2026-07-13", "value": -5.504e9}],
                    "2027E": [{"org": "汇丰", "published": "2026-07-28", "value": 2.49e8}]})

        p = urc.append_broker_forecasts("02513", rec, tmp_path)

        assert len(_read(p)) == 3

    def test_carries_org_year_published_value(self, tmp_path):
        rec = _rec({"2026E": [{"org": "高盛", "published": "2026-07-10", "value": -4.059e9}]})

        rows = _read(urc.append_broker_forecasts("02513", rec, tmp_path))

        assert rows[0]["org"] == "高盛" and rows[0]["year"] == "2026E"
        assert rows[0]["published"] == "2026-07-10" and rows[0]["value"] == -4.059e9

    def test_records_source_and_first_seen(self, tmp_path):
        """来源要留：A 股是东财 F10、港股是经济通，将来比准确度时不能混为一谈。"""
        rec = _rec({"2026E": [{"org": "高盛", "published": "2026-07-10", "value": -1.0}]})

        rows = _read(urc.append_broker_forecasts("02513", rec, tmp_path))

        assert rows[0]["source"] == "etnet" and rows[0]["first_seen"] == "2026-08-10"


# ── 去重与改口 ───────────────────────────────────────────────────────────────


class TestDedup:
    _R = _rec({"2026E": [{"org": "高盛", "published": "2026-07-10", "value": -4.059e9}]})

    def test_rerun_does_not_duplicate(self, tmp_path):
        """★周更 cron 每周都会看到同一批未改口的预测，不去重会迅速堆成垃圾。"""
        urc.append_broker_forecasts("02513", self._R, tmp_path)
        p = urc.append_broker_forecasts("02513", self._R, tmp_path)

        assert len(_read(p)) == 1

    def test_revision_creates_a_new_row(self, tmp_path):
        """★券商改口必须留痕——这正是回测要看的东西，不能覆盖掉旧值。"""
        urc.append_broker_forecasts("02513", self._R, tmp_path)
        newer = _rec({"2026E": [{"org": "高盛", "published": "2026-08-09", "value": -3.0e9}]})

        rows = _read(urc.append_broker_forecasts("02513", newer, tmp_path))

        assert len(rows) == 2
        assert {r["published"] for r in rows} == {"2026-07-10", "2026-08-09"}

    def test_same_firm_different_year_kept_separately(self, tmp_path):
        rec = _rec({"2026E": [{"org": "高盛", "published": "2026-07-10", "value": -1.0}],
                    "2027E": [{"org": "高盛", "published": "2026-07-10", "value": -2.0}]})

        assert len(_read(urc.append_broker_forecasts("02513", rec, tmp_path))) == 2

    def test_rows_without_org_are_dropped(self, tmp_path):
        """没有机构名就无法归因，留着只是噪音。"""
        rec = _rec({"2026E": [{"org": None, "published": "2026-07-10", "value": -1.0},
                              {"org": "高盛", "published": "2026-07-10", "value": -2.0}]})

        assert len(_read(urc.append_broker_forecasts("02513", rec, tmp_path))) == 1


# ── 不该写的时候不写 ─────────────────────────────────────────────────────────


class TestNoOp:
    def test_missing_brokers_returns_none(self, tmp_path):
        assert urc.append_broker_forecasts("02513", {"fetched_at": "2026-08-10"}, tmp_path) is None

    def test_empty_brokers_returns_none(self, tmp_path):
        assert urc.append_broker_forecasts("02513", _rec({}), tmp_path) is None

    def test_list_shaped_brokers_rejected(self, tmp_path):
        """★A 股旧格式是扁平 list（位置式年份 profit_y2/y3）。误传进来会静默写出
        没有年份的行，污染整份记录——宁可不写。"""
        rec = _rec([{"org": "国联民生", "profit_y2_yuan": 1}])

        assert urc.append_broker_forecasts("02513", rec, tmp_path) is None


# ── 接在落盘入口（两条路径自动都覆盖）─────────────────────────────────────────


def test_wired_into_write_and_deploy(tmp_path, monkeypatch):
    """算了没接＝白算——这套管线已经犯过多次（preferred_stat / momentum / 腾讯码）。"""
    monkeypatch.setattr(urc, "DOCS_DATA", tmp_path)
    monkeypatch.setattr(urc, "DEPLOY_DATA", tmp_path / "nope")
    rec = {"symbol": "02513", "name": "智谱", "fetched_at": "2026-08-10T08:45:00+08:00",
           "estimates": {"2026E": {"revenue_yuan": 3.84e9}},
           "broker_stats": {"2026E": {"count": 13}},
           "broker_source": "etnet",
           "brokers": {"2026E": [{"org": "高盛", "published": "2026-07-10", "value": -4.06e9}]}}

    urc.write_and_deploy("02513", rec)

    assert (tmp_path / "02513-broker-forecasts.jsonl").exists()


# ── A 股与港股形状必须一致 ───────────────────────────────────────────────────


def test_hk_persists_per_firm_detail():
    """港股逐家明细必须落盘——它是本次改动的全部目的。"""
    src = (_ROOT / "scripts" / "update_research_consensus.py").read_text(encoding="utf-8")

    assert 'record["brokers"] = et["brokers"]' in src, "港股未落盘逐家明细"


def test_a_share_path_untouched():
    """★用户 2026-08-10：「ETNet 和 yfinance 只服务港股，别动 A 股」。

    我在收到这条之前已经把 A 股的 `brokers` 从 `parse_em_brokers` 改成了
    `parse_em_broker_records`（顺带修截断），收到后已还原。本测试把「不动 A 股」
    钉住，免得以后又被顺手改掉。

    ⚠ A 股那侧的已知问题**保留未修**（有意为之，非遗漏）：
    `parse_em_brokers` 位置式年份 + limit=20 硬截断，导致存下来的逐家明细
    与统计量所用样本对不上（茅台 20 vs 31、宁德 20 vs 27）。
    要不要修是独立决定，需用户另行拍板。
    """
    src = (_ROOT / "scripts" / "update_research_consensus.py").read_text(encoding="utf-8")

    assert '"brokers": parse_em_brokers(ycmx)' in src, "A 股路径被改动了"
    assert "def parse_em_brokers(" in src, "A 股旧解析器不该被删"
