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

import json


def write_auto_consensus(data_dir, key: str, estimates: dict) -> None:
    """在测试用 DATA_DIR 里放一份自动源一致预期。

    `estimates` 形如 `{"2026E": {"profit_yuan": 8.6e10}}`——
    与 `{key}-consensus.json` 的 `estimates` 段同构。
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / f"{key}-consensus.json").write_text(
        json.dumps({"symbol": key, "estimates": estimates}, ensure_ascii=False),
        encoding="utf-8")
