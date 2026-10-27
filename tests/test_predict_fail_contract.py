"""两条回归守卫（2026-10-26 复评发现的两处真实缺陷）。

**G-1 预测软失败契约**（`predict_service._fail`）
-----------------------------------------------------------------------------
`_fail()` 的调用点传了 `ip=ip`，而 `_fail()` 自己的签名里**没有** `ip` 形参
⇒ Python 在**调用瞬间**就抛
`TypeError: _fail() got an unexpected keyword argument 'ip'`。

这个 TypeError 发生在 `except Exception` 处理器**内部**，于是：
1. 逃逸出 `create_and_run` ⇒ 线上返回 **HTTP 500**，
   而不是契约承诺的 `code=0 / status="failed" / error_code="E_INFER_FAIL"`；
2. `task_dao.mark_failed()` **从未被调用** ⇒ 任务行永远停在 `running`
   （僵尸任务），历史列表里堆积，且「失败率」分母被污染。

🔒 守卫方式：**直接调 `_fail` 并传 `ip`**。不需要起模型、不需要建库，
毫秒级。`inspect.signature` 那条断言是第二道防线 ——
万一有人改了形参名（`ip` → `client_ip`），签名断言会先报出来。

**G-2 错误明细 CSV 注入**（`dataset_service._write_error_detail`）
-----------------------------------------------------------------------------
`API-11` 错误明细 CSV 下载的是**用户上传的原始行**（含原始 `sku_id`），
威胁模型与结果导出（`API-16`）完全相同 —— 但此前只有 `API-16` 做了
CSV 注入转义，这条路漏了。用户上传一份 `sku_id = =cmd|'/c calc'!A0`
的文件并触发校验失败，下载错误明细用 Excel 打开即触发 DDE 公式执行。

🔒 守卫方式：走**真实写入路径** `_write_error_detail`，读回文件断言
危险前缀已被转义、而**数值列没有被转义**（`-12` 必须还是 `-12`，
不能变成 `'-12`）。
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from app.core import csv_safety
from app.services import dataset_service, predict_service


class TestPredictFailContract:
    def test_fail_accepts_ip_kwarg(self) -> None:
        """G-1 第一道防线：签名里必须真的有 `ip` 形参。"""
        params = inspect.signature(predict_service._fail).parameters
        assert "ip" in params, (
            "_fail() 缺少 ip 形参 —— 调用点传了 ip=ip，会在调用瞬间抛 TypeError，"
            "把「软失败」变成 HTTP 500 并留下 running 僵尸任务"
        )

    def test_fail_with_ip_does_not_raise(self) -> None:
        """G-1 第二道防线：真调一次，传 ip，断言不抛 TypeError。"""
        from unittest.mock import MagicMock

        db = MagicMock()
        task = MagicMock()
        task.id = 1
        task.dataset_id = 1
        task.model_version = "lstm_v1"
        prog = MagicMock()
        prog.device = "cpu"
        prog.device_fallback = 0
        prog.series_count = 3

        payload = predict_service._fail(
            db,
            task=task,
            prog=prog,
            error_code="E_INFER_FAIL",
            error_msg="推理过程异常：RuntimeError",
            completed_stage="predict",
            duration_ms=1234,
            user_id=7,
            ip="203.0.113.9",
        )
        assert payload["status"] == "failed"
        assert payload["error_code"] == "E_INFER_FAIL"


class TestErrorDetailCsvInjection:
    def test_error_detail_escapes_text_columns(self, tmp_path: Path) -> None:
        """G-2：文本列的危险前缀被转义。"""
        rows = [
            {
                "sku_id": "=cmd|'/c calc'!A0",
                "warehouse_id": "@SUM(1+1)",
                "date": "2020-01-01",
                "sales": "-12",
                "row_no": 2,
                "error_reason": "日期无法解析",
            }
        ]
        path = dataset_service._write_error_detail(
            1, "probe", ["sku_id", "warehouse_id", "date", "sales"], rows
        )
        text = path.read_text(encoding="utf-8-sig")
        assert "'=cmd|'/c calc'!A0" in text, "sku_id 的公式前缀未被转义"
        assert "'@SUM(1+1)" in text, "warehouse_id 的公式前缀未被转义"
        # 🔴 数值列绝不能被转义：`-12` 变成 `'-12` 会破坏 Excel 的类型与聚合
        assert ",-12," in text, "数值列被误转义，Excel 会当文本处理"
        assert "'-12" not in text, "数值列被误加前缀"

    def test_safe_text_rules(self) -> None:
        """转义规则的单元断言（前缀集合与空值处理）。"""
        assert csv_safety.safe_text("=1+1") == "'=1+1"
        assert csv_safety.safe_text("+1") == "'+1"
        assert csv_safety.safe_text("@x") == "'@x"
        assert csv_safety.safe_text("\tx") == "'\tx"
        assert csv_safety.safe_text("normal") == "normal"
        assert csv_safety.safe_text(None) == ""
        assert csv_safety.safe_text("") == ""


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
