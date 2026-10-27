"""第二轮待修项的回归守卫（2026-10-26 复评 → 2026-10-27 修复）。

覆盖体检报告 `outputs/产品优化体检报告_V1.0.md` §3.2 的 9 条待修项，
每条一个可机械验证的断言。**负向验证过**：把对应改动回退，测试会红。

C-06  60 秒硬约束真正生效（阶段边界检查 deadline）
C-07  游客共享桶只共享「系统示例数据」
C-08  `_build_prediction_rows` 用 groupby 索引，不再逐序列全表扫
C-09  历史窗口长度取所有序列的最大值（不能取首条）
C-10  export 动作写审计表
C-11  访问日志结构化（JSON）且带 task_id / user_id
C-12  `_UNKNOWN_FAIL` 字典有上界
C-13  「数据文件已丢失」统一为 4xxxx（不再 500）
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from app.algo import config as algo_config
from app.core import errors
from app.models.dataset import Dataset
from app.services import auth_service, dataset_service, predict_service, result_service


def _code_only(obj) -> str:
    """只取**可执行代码**（剥掉 docstring 与注释）。

    🔴 为什么需要它：判据经常是「某段代码里不该再出现 X」，
    而 X 可能只是留在**注释**里的历史说明 —— 直接搜源码会假失败。
    `ast.unparse` 会把注释全部丢掉，正是我们要的。
    """
    tree = ast.parse(inspect.getsource(obj))
    body = tree.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    tree.body = body
    return ast.unparse(tree)


# ---------------------------------------------------------------------------
# C-06：60 秒硬约束
# ---------------------------------------------------------------------------
class TestDeadlineGuard:
    def test_check_deadline_raises_when_exceeded(self) -> None:
        import time

        prog = MagicMock()
        prog.stage = "inference"
        with pytest.raises(predict_service._DeadlineExceeded):
            predict_service._check_deadline(time.perf_counter() - 1, prog)

    def test_check_deadline_passes_within_budget(self) -> None:
        import time

        prog = MagicMock()
        prog.stage = "inference"
        predict_service._check_deadline(time.perf_counter() + 60, prog)  # 不抛

    def test_pipeline_checks_deadline_between_two_passes(self) -> None:
        """两遍流水线之间必须查预算（否则等于把 60 秒架空一倍）。"""
        src = inspect.getsource(predict_service._run_pipeline)
        assert src.count("_check_deadline(") >= 2, (
            "_run_pipeline 里的 deadline 检查点少于 2 处 —— "
            "回测遍与预测遍之间、CPU 回退重跑前都要查"
        )

    def test_deadline_exception_becomes_soft_failure(self) -> None:
        """超预算必须走 `E_TIMEOUT` 软失败，而不是 HTTP 500。"""
        src = inspect.getsource(predict_service.create_and_run)
        assert "except _DeadlineExceeded" in src, "create_and_run 未捕获 _DeadlineExceeded"
        assert "E_TIMEOUT" in src, "超预算没有落到 E_TIMEOUT"


# ---------------------------------------------------------------------------
# C-07：游客共享桶
# ---------------------------------------------------------------------------
class TestGuestBucketIsolation:
    @staticmethod
    def _ds(user_id: int, is_sample: int) -> Dataset:
        ds = Dataset(
            user_id=user_id, file_name="x.csv", stored_name="x.csv", file_path="x.csv",
        )
        ds.is_sample = is_sample
        return ds

    def test_sample_data_shared_with_anyone(self) -> None:
        """系统示例数据仍是公共资源（`API-07` 共用一份）。"""
        dataset_service.assert_dataset_readable(self._ds(0, 1), 5)  # 不抛

    def test_guest_uploaded_data_blocked_for_logged_in_user(self) -> None:
        """🔴 核心：游客上传的真实数据，已登录用户**读不到**。"""
        with pytest.raises(errors.BizError) as exc:
            dataset_service.assert_dataset_readable(self._ds(0, 0), 5)
        assert exc.value.code == errors.ErrorCode.FORBIDDEN

    def test_guest_can_read_own_uploaded_data(self) -> None:
        """游客读自己上传的数据仍要能（否则游客功能直接瘫掉）。"""
        dataset_service.assert_dataset_readable(self._ds(0, 0), 0)  # 不抛

    def test_other_users_data_still_blocked(self) -> None:
        """普通隔离逻辑不受影响。"""
        with pytest.raises(errors.BizError):
            dataset_service.assert_dataset_readable(self._ds(5, 0), 6)


# ---------------------------------------------------------------------------
# C-08：全表扫优化
# ---------------------------------------------------------------------------
class TestSeriesIndexPerformance:
    @staticmethod
    def _df(n_series: int = 5, n_days: int = 10) -> pd.DataFrame:
        rows = []
        for i in range(n_series):
            for d in range(n_days):
                rows.append({
                    algo_config.COL_SKU: f"item_{i:04d}",
                    algo_config.COL_WAREHOUSE: "WH1",
                    algo_config.COL_DATE: f"2015-01-{d + 1:02d}",
                    algo_config.COL_SALES: float(d),
                })
        return pd.DataFrame(rows)

    def test_index_and_scan_agree(self) -> None:
        """新索引与旧全表扫的结果必须**逐条一致**。"""
        df = self._df()
        groups = predict_service._series_index(df)
        for (sku, wh) in groups:
            new = predict_service._series_values(df, sku, wh, groups=groups)
            old = predict_service._series_values(df, sku, wh)
            assert np.array_equal(new, old), f"{sku} 的历史序列不一致"

    def test_index_missing_series_returns_empty(self) -> None:
        df = self._df()
        groups = predict_service._series_index(df)
        assert predict_service._series_values(df, "nope", "WH1", groups=groups).size == 0

    def test_build_rows_uses_index(self) -> None:
        """`_build_prediction_rows` 必须建索引并传下去。"""
        src = inspect.getsource(predict_service._build_prediction_rows)
        assert "_series_index(" in src, "没有建索引"
        assert "groups=groups" in src, "索引没有传给 _series_values"


# ---------------------------------------------------------------------------
# C-09：历史窗口长度
# ---------------------------------------------------------------------------
class TestHistoryWindow:
    def test_window_uses_max_not_first(self) -> None:
        code = _code_only(result_service._series_curves)
        assert "next(iter(rows.values()))" not in code, (
            "历史窗口仍在取『第一条』序列的长度 —— L3 排首位时整图历史段会塌成 1 点"
        )
        assert "max(" in code, "历史窗口没有改用各序列最大值"


# ---------------------------------------------------------------------------
# C-10：export 审计
# ---------------------------------------------------------------------------
class TestExportAudit:
    def test_prepare_export_writes_audit(self) -> None:
        src = inspect.getsource(result_service.prepare_export)
        assert "ACTION_EXPORT" in src, "导出没有写审计（FR-SYS-03 的第 4 类操作）"
        assert "oplog_dao.record" in src, "导出没有调用 oplog_dao.record"

    def test_prepare_export_accepts_ip(self) -> None:
        params = inspect.signature(result_service.prepare_export).parameters
        assert "ip" in params, "prepare_export 没有 ip 形参，审计里定位不到发起者"


# ---------------------------------------------------------------------------
# C-11：访问日志
# ---------------------------------------------------------------------------
class TestAccessLog:
    def test_access_log_is_json(self) -> None:
        src = inspect.getsource(
            __import__("app.core.middleware", fromlist=["x"]).AccessLogMiddleware
        )
        assert "json.dumps" in src, "访问日志不是结构化 JSON"
        assert "task_id" in src and "user_id" in src, "访问日志没有 task_id / user_id"

    def test_middleware_has_no_fake_middleware_class(self) -> None:
        """代码里**不能真的存在** `TimeoutGuardMiddleware` 这个类。

        （docstring 里提到它「曾经被写过但不存在」是允许的 —— 那是说明，
        但要防的是有人照着文档把它补成一个假实现。）
        """
        import app.core.middleware as mw

        path = Path(inspect.getfile(mw))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        classes = [
            n.name for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef)
        ]
        assert "TimeoutGuardMiddleware" not in classes, (
            "middleware.py 里不该存在 TimeoutGuardMiddleware —— "
            "端点是同步阻塞执行的，ASGI 中间件没有能力中断它"
        )

    def test_middleware_docstring_explains_why(self) -> None:
        """docstring 必须**解释清楚**为什么没有这层中间件（否则下一个人又会补上）。"""
        import app.core.middleware as mw

        text = Path(inspect.getfile(mw)).read_text(encoding="utf-8")
        assert "TimeoutGuardMiddleware" in text, "docstring 没有说明该中间件为何不存在"
        assert "BE-16" in text, "docstring 没有指向 BE-16 的正确方案"


# ---------------------------------------------------------------------------
# C-12：未知邮箱限流字典上界
# ---------------------------------------------------------------------------
class TestUnknownFailBound:
    def test_bump_unknown_has_size_bound(self) -> None:
        src = inspect.getsource(auth_service._bump_unknown)
        assert "4096" in src, "_UNKNOWN_FAIL 没有条目数上界（可被随机邮箱撑爆内存）"

    def test_dict_bounded_under_flood(self) -> None:
        """打 5000 个不同邮箱，字典不能无限长。"""
        auth_service._UNKNOWN_FAIL.clear()
        for i in range(5000):
            auth_service._bump_unknown(f"probe_{i}@example.com", 3, 60)
        assert len(auth_service._UNKNOWN_FAIL) <= 4096
        auth_service._UNKNOWN_FAIL.clear()


# ---------------------------------------------------------------------------
# C-13：数据文件丢失的错误码
# ---------------------------------------------------------------------------
class TestFileMissingErrorCode:
    def test_file_missing_is_4xxxx_not_500(self) -> None:
        """「数据文件已丢失」是用户可恢复状态，不能返 HTTP 500。"""
        src = inspect.getsource(dataset_service.get_detail)
        assert "IO_ERROR" not in src, "get_detail 仍用 IO_ERROR(50002) ⇒ 会返 HTTP 500"
        assert "DATASET_NOT_FOUND" in src

    def test_preview_file_missing_is_4xxxx(self) -> None:
        src = inspect.getsource(dataset_service.get_preview)
        assert "IO_ERROR" not in src, "get_preview 仍用 IO_ERROR(50002)"
        assert "DATASET_NOT_FOUND" in src

    def test_server_side_issue_stays_500(self) -> None:
        """服务端真故障（示例文件缺失）**仍应是 500**，不能一刀切。"""
        src = inspect.getsource(dataset_service.load_sample)
        assert "IO_ERROR" in src, "示例数据缺失被误降级成 4xx（那是服务端故障）"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
