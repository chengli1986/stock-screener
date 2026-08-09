#!/usr/bin/env python3
"""_horizon.py — 预测视界的单一定义（当前年 + 次年，按日历滚动）

用户判断（2026-08-05 首次，08-09 重申）：
> 「从现在看 27 年一整年，差不多，28 年太过于久远；而且现在世界格局变化极大，
>   A 股又不是一个强有效市场，看一年半已经足够了。」
> 「一致预期看到 2027 年底就够了，2028 年有点遥远。」

## 为什么抽成共享模块

2026-08-09 盘点发现视界**写死在 4 个地方**（买点告警、consensus 两处、
docs-site 的 consensus-quality.js），四份 `("2026E","2027E")` 各写各的。
（买点告警脚本已于同日按用户要求删除——「现在不会用，未来可能要重写」。）

- **2027 年一到全都错**：那时 2026 已是实际值，代码却仍死盯 2026E，
  等于拿一个已公布的年份当预测展示。
- 改一处漏三处。本管线已因「同一概念多处定义」栽过跟头
  （覆盖机构数 count vs orgs，2026-08-09）。

页面侧不再自己写死，改为读 `consensus.json` 的 `horizon` 字段——
数据是唯一真相源。
"""

from datetime import date as _date


def horizon_years(today: "_date | None" = None) -> tuple[str, str]:
    """预测视界：当前年 + 次年。

    2026-08 → ('2026E', '2027E')；2027-03 → ('2027E', '2028E')。
    视界长度在 1~2 年间浮动，符合「一年半左右」的本意，且不需要每年手工改。
    """
    y = (today or _date.today()).year
    return (f"{y}E", f"{y + 1}E")


def in_horizon(label: str, today: "_date | None" = None) -> bool:
    """该年份标签是否在视界内。实际值（`2025A`）永远不在——它不是预测。"""
    return str(label) in horizon_years(today)


def filter_horizon(labels, today: "_date | None" = None) -> list:
    """按视界过滤并**按年份升序**返回（调用方常直接用于展示，顺序要稳定）。"""
    keep = horizon_years(today)
    return [x for x in keep if x in set(map(str, labels))]
