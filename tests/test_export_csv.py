"""导出链路的纯函数 / 分块行为单测。

覆盖 2026-10-26 修掉的两个缺陷（评审报告 R-01 / R-02）：

  R-01 流式：`iter_export_chunks()` 必须**逐批产出**，首块是 UTF-8 BOM，
       且总字节数与「一次性拼好」完全一致（流式不能改变产物）。
  R-02 注入：`sku_id` / `warehouse_id` 来自用户上传，以 `= + - @ Tab CR`
       开头时必须加 `'` 前缀，否则 Excel 打开导出文件会执行公式（DDE）。

这些用例**不碰数据库**（另见 `tests/test_error_codes.py` 的说明），
所以可以在 CI 干净 runner 上跑。
"""
from __future__ import annotations

import csv
import io
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import result_service as rs

BOM = b"\xef\xbb\xbf"


def _row(**over: Any) -> SimpleNamespace:
    """构造一个只带 `_export_cells` 所需属性的行对象（鸭子类型，无需 ORM）。"""
    base: dict[str, Any] = {
        "sku_id": "SKU_0001",
        "warehouse_id": "上海仓",
        "forecast_date": "2026-01-01",
        "predicted_value": 120.0,
        "granularity_level": "L1",
        "actual_value": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


# =============================================================================
# R-02：CSV 公式注入
# =============================================================================
class TestCsvInjection:
    @pytest.mark.parametrize(
        "payload",
        ["=cmd|'/c calc'!A1", "+1+1", "-1", "@SUM(A1:A9)", "\tfoo", "\rfoo"],
    )
    def test_text_columns_are_defused(self, payload: str) -> None:
        cells = rs._export_cells(_row(sku_id=payload, warehouse_id=payload))
        assert cells[0] == "'" + payload
        assert cells[1] == "'" + payload

    def test_normal_text_is_untouched(self) -> None:
        cells = rs._export_cells(_row(sku_id="SKU_0001", warehouse_id="广州仓"))
        assert cells[0] == "SKU_0001"
        assert cells[1] == "广州仓"

    def test_numeric_columns_never_prefixed(self) -> None:
        # 关键边界：负数是**合法数值**，加 ' 会把它变成文本、破坏 Excel 求和
        cells = rs._export_cells(_row(predicted_value=-3.5, actual_value=-2.0))
        assert cells[3] == "-3.50"
        assert cells[5] == "-2.00"
        assert cells[6] == "1.50"

    def test_empty_and_none_are_safe(self) -> None:
        assert rs._csv_text("") == ""
        assert rs._csv_text(None) == ""


# =============================================================================
# 空值语义：无真值写空串，不写 0
# =============================================================================
class TestEmptyNotZero:
    def test_missing_actual_stays_empty(self) -> None:
        cells = rs._export_cells(_row(actual_value=None))
        assert cells[5] == ""       # actual_sales
        assert cells[6] == ""       # error

    def test_error_is_absolute_difference(self) -> None:
        cells = rs._export_cells(_row(predicted_value=100.0, actual_value=87.5))
        assert cells[6] == "12.50"

    def test_header_order_is_frozen(self) -> None:
        # 列序是交付契约（UX-P08-08），改它会破坏业务方已有的 Excel 模板
        assert rs.EXPORT_HEADER == (
            "sku_id",
            "warehouse_id",
            "date",
            "forecast_sales",
            "granularity_level",
            "actual_sales",
            "error",
        )


# =============================================================================
# R-01：分块流式
# =============================================================================
class _FakeSession:
    """替身会话：生成器会在 `with SessionLocal() as db` 里取数。"""

    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def close(self) -> None:  # pragma: no cover - 兼容性兜底
        pass


@pytest.fixture()
def fake_source(monkeypatch: pytest.MonkeyPatch):
    """把 DAO 与会话工厂换成可控替身，返回「设置行数」的函数。"""

    def _install(n: int) -> None:
        rows = [_row(sku_id=f"SKU_{i:05d}", forecast_date=f"2026-01-{(i % 28) + 1:02d}")
                for i in range(n)]
        monkeypatch.setattr(
            rs.prediction_dao,
            "iter_all_for_export",
            lambda db, task_id, chunk=2000: iter(rows),
        )
        monkeypatch.setattr(rs, "SessionLocal", _FakeSession)

    return _install


def _csv_spec(**over: Any) -> rs.ExportSpec:
    base = dict(fmt="csv", name="t.csv", mime="text/csv; charset=utf-8",
                task_id=1, dump=False)
    base.update(over)
    return rs.ExportSpec(**base)


class TestChunkedCsv:
    def test_first_chunk_is_bom(self, fake_source) -> None:
        fake_source(3)
        chunks = list(rs.iter_export_chunks(_csv_spec()))
        assert chunks[0] == BOM, "首块必须是 BOM，否则 Excel 打开中文乱码"

    def test_chunking_is_bounded(self, fake_source) -> None:
        """行数远大于一批时，必须被切成多块（这是「内存不随数据量增长」的前提）。"""
        n = rs.EXPORT_CHUNK_ROWS * 2 + 7
        fake_source(n)
        chunks = list(rs.iter_export_chunks(_csv_spec()))
        # 1(BOM) + 2(整批) + 1(尾批) = 4
        assert len(chunks) == 4
        assert all(len(c) > 0 for c in chunks)

    def test_stream_equals_materialized(self, fake_source) -> None:
        """流式产物必须与「一次性拼好」逐字节一致 —— 否则下载下来的文件就变了。"""
        n = 25
        fake_source(n)
        streamed = b"".join(rs.iter_export_chunks(_csv_spec()))

        # 用同一批行按旧方式一次性拼
        rows = [_row(sku_id=f"SKU_{i:05d}", forecast_date=f"2026-01-{(i % 28) + 1:02d}")
                for i in range(n)]
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(rs.EXPORT_HEADER)
        for r in rows:
            w.writerow(rs._export_cells(r))
        expected = buf.getvalue().encode("utf-8-sig")

        assert streamed == expected

    def test_header_and_row_count(self, fake_source) -> None:
        fake_source(5)
        body = b"".join(rs.iter_export_chunks(_csv_spec())).decode("utf-8-sig")
        lines = body.splitlines()
        assert len(lines) == 6                      # 表头 + 5 行
        assert lines[0].startswith("sku_id,warehouse_id,date")

    def test_dump_false_writes_nothing(self, fake_source, tmp_path) -> None:
        """`dump=False` 时不得产生留档文件（阈值以下不留盘）。"""
        fake_source(3)
        before = set(rs.settings.export_dir.glob("*")) if rs.settings.export_dir.exists() else set()
        list(rs.iter_export_chunks(_csv_spec(dump=False)))
        after = set(rs.settings.export_dir.glob("*")) if rs.settings.export_dir.exists() else set()
        assert after - before == set()
