"""7 步校验的行为测试（`R-06` 拆包后的直接收益）。

拆包之前，`inspect_file()` 与 `dataset_service` 绑在一起，想测一个分支
得先起库、造用户、走上传接口；现在校验层不知道 `datasets` 表的存在，
所以这里只用一个临时 CSV 文件就能覆盖到每一条错误分支。

⚠️ 本文件**刻意不经过 HTTP 层**：`verify_ep4_api.py`（119 项）已经覆盖了
端到端契约；这里守的是「每一步的判定边界」，两者互补不重复。
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from app.core import config
from app.core.errors import BizError, ErrorCode
from app.validators import check_extension, inspect_file

HEADER = "sku_id,warehouse_id,date,sales\n"


def _write(tmp_path: Path, body: str, name: str = "d.csv") -> Path:
    p = tmp_path / name
    p.write_text(HEADER + body, encoding="utf-8")
    return p


def _inspect(p: Path, original_rows: int | None = None):
    return inspect_file(p, size_bytes=p.stat().st_size, original_rows=original_rows)


def _settings(monkeypatch: pytest.MonkeyPatch, **overrides) -> None:
    """临时替换配置（`Settings` 是 frozen dataclass，只能整体替换）。"""
    monkeypatch.setattr(
        config, "settings", dataclasses.replace(config.settings, **overrides)
    )


class TestCheckExtension:
    """第 1 步：落盘前即拒。"""

    @pytest.mark.parametrize("name", ["a.xlsx", "a.txt", "noext", ""])
    def test_rejects_non_csv(self, name: str) -> None:
        with pytest.raises(BizError) as ei:
            check_extension(name)
        assert ei.value.code == ErrorCode.BAD_EXTENSION
        # 前端要按 fail_stage 展示「第 1 步失败」，所以这个键不能缺
        assert ei.value.data.get("fail_stage")

    @pytest.mark.parametrize("name", ["a.csv", "A.CSV", "  data.Csv  "])
    def test_accepts_csv_case_insensitive(self, name: str) -> None:
        check_extension(name)  # 不应抛


class TestHappyPath:
    def test_valid_file_yields_stats_and_preview(self, tmp_path: Path) -> None:
        p = _write(
            tmp_path,
            "i1,sh,2024-01-01,10\ni1,sh,2024-01-02,12\ni2,sh,2024-01-01,3\n",
        )
        r = _inspect(p)
        assert r.ok, r.issue
        assert r.stats["row_count"] == 3
        assert r.stats["valid_series_count"] == 2
        assert r.stats["date_start"] == "2024-01-01"
        assert r.gap_count == 0

    def test_preview_hides_internal_helpers(self, tmp_path: Path) -> None:
        """预览列必须与源文件一致 —— 内部辅助列 `_d`/`_s` 泄漏会直接违反 FR-UPL-07。"""
        p = _write(tmp_path, "i1,sh,2024-01-01,10\n")
        r = _inspect(p)
        assert r.preview["columns"] == ["sku_id", "warehouse_id", "date", "sales"]

    def test_sales_is_numeric_in_preview_others_are_strings(self, tmp_path: Path) -> None:
        p = _write(tmp_path, "i1,sh,2024-01-01,10\n")
        row = _inspect(p).preview["rows"][0]
        assert row["sales"] == 10 and isinstance(row["sales"], int)
        assert row["date"] == "2024-01-01" and isinstance(row["date"], str)

    def test_loose_date_formats_are_accepted(self, tmp_path: Path) -> None:
        """PRD 的口径是「可解析」而不是「必须 YYYY-MM-DD」。"""
        p = _write(tmp_path, "i1,sh,2024/1/7,10\ni1,sh,20240108,12\n")
        assert _inspect(p).ok


class TestStepFailures:
    """每一步的失败必须落在**自己的** fail_stage 上（顺序不可串位）。"""

    def test_missing_warehouse_column(self, tmp_path: Path) -> None:
        p = tmp_path / "d.csv"
        p.write_text("sku_id,date,sales\ni1,2024-01-01,10\n", encoding="utf-8")
        r = _inspect(p)
        assert r.issue and r.issue.code == ErrorCode.MISSING_COLUMN
        assert "warehouse_id" in r.issue.message
        # JT-01：中文列名文件被拒时必须点明「只接受英文列名」
        assert "sku_id" in r.issue.data["suggestion"]

    def test_unparsable_date(self, tmp_path: Path) -> None:
        p = _write(tmp_path, "i1,sh,2015/13/45,10\n")
        r = _inspect(p)
        assert r.issue and r.issue.code == ErrorCode.DATE_UNPARSABLE
        assert r.issue.data["unparsable_rows"][0]["row_no"] == 2   # 表头占第 1 行
        assert r.issue.error_rows, "错误明细必须可导出"

    def test_negative_sales(self, tmp_path: Path) -> None:
        p = _write(tmp_path, "i1,sh,2024-01-01,-5\n")
        r = _inspect(p)
        assert r.issue and r.issue.code == ErrorCode.NEGATIVE_VALUE
        assert r.issue.data["negative_rows"][0]["value"] == -5.0

    def test_non_numeric_sales_shares_the_same_code(self, tmp_path: Path) -> None:
        """「非数值」与「负值」共用 40003：修复动作相同，不拆码。"""
        p = _write(tmp_path, "i1,sh,2024-01-01,abc\n")
        r = _inspect(p)
        assert r.issue and r.issue.code == ErrorCode.NEGATIVE_VALUE
        assert r.issue.data["unparsable_rows"][0]["raw_value"] == "abc"

    def test_empty_file(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.csv"
        p.write_text("", encoding="utf-8")
        r = _inspect(p)
        assert r.issue and r.issue.code == ErrorCode.EMPTY_FILE

    def test_header_only(self, tmp_path: Path) -> None:
        p = tmp_path / "h.csv"
        p.write_text(HEADER, encoding="utf-8")
        r = _inspect(p)
        assert r.issue and r.issue.code == ErrorCode.EMPTY_FILE

    def test_size_limit(self, tmp_path: Path, monkeypatch) -> None:
        _settings(monkeypatch, max_file_size_mb=0)
        p = _write(tmp_path, "i1,sh,2024-01-01,10\n")
        r = inspect_file(p, size_bytes=2 * 1024 * 1024)
        assert r.issue and r.issue.code == ErrorCode.FILE_TOO_LARGE

    def test_row_limit_before_parse(self, tmp_path: Path, monkeypatch) -> None:
        _settings(monkeypatch, max_rows=10)
        p = _write(tmp_path, "i1,sh,2024-01-01,10\n")
        r = inspect_file(p, size_bytes=p.stat().st_size, original_rows=999)
        assert r.issue and r.issue.code == ErrorCode.ROW_OVER_LIMIT

    def test_series_limit_refuses_without_gaps(self, tmp_path: Path, monkeypatch) -> None:
        _settings(monkeypatch, max_series=1)
        p = _write(tmp_path, "i1,sh,2024-01-01,1\ni2,sh,2024-01-01,2\n")
        r = _inspect(p)
        assert r.issue and r.issue.code == ErrorCode.SERIES_OVER_LIMIT
        assert r.issue.data["series_count"] == 2


class TestGapHandling:
    def test_gap_detected_but_not_rejected(self, tmp_path: Path) -> None:
        """断档要交给用户选「插值 / 跳过」（FR-UPL-13），**不能**直接判失败。"""
        p = _write(
            tmp_path,
            "i1,sh,2024-01-01,1\ni1,sh,2024-01-02,2\ni1,sh,2024-01-06,3\n",
        )
        r = _inspect(p)
        assert r.ok, r.issue
        assert r.gap_count == 1
        assert r.gap_days_total == 3            # 1-03 / 1-04 / 1-05
        assert r.gap_ranges[0]["gap_start"] == "2024-01-03"
        assert r.gap_ranges[0]["gap_end"] == "2024-01-05"

    def test_gap_suppresses_series_limit(self, tmp_path: Path, monkeypatch) -> None:
        """有断档时**先不判**序列数上限，否则用户拿不到断档处置入口。"""
        _settings(monkeypatch, max_series=1)
        p = _write(
            tmp_path,
            "i1,sh,2024-01-01,1\ni1,sh,2024-01-06,1\ni2,sh,2024-01-01,2\n",
        )
        r = _inspect(p)
        assert r.ok, "有断档时不该因序列数超限而失败"
        assert r.gap_count == 1
