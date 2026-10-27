"""
=============================================================================
数据集业务逻辑（上传 / 7 步校验 / 断档处置 / 示例数据 / 详情 / 预览 / 错误明细）
-----------------------------------------------------------------------------
版本     : V1.0（EP-4）
依据文档 : 《开发技术文档 V1.0》§5.5（`API-06` ~ `API-11`）、§4.3 `BE-41`（归属判定顺序）、
           §3.8（数据清理策略 / `JT-11` 错误明细 TTL 1 小时）
           《PRD V1.0》`FR-UPL-01` ~ `FR-UPL-15`、`FR-SYS-08`
           《数据库设计文档 V1.0》§3.2（`DB-T02`）、§2.6（`RESTRICT`）
           《UI/UX 设计文档 V1.0》§8.1 `UX-P05` 与 `UX-ST-05` 五类错误分支
           《项目开发实施方案 V1.0》§7.5（`WS-4-01` ~ `WS-4-08`）
-----------------------------------------------------------------------------
职责边界（`BE-04` / `BE-14`）
-----------------------------------------------------------------------------
  · 本模块**不 import fastapi**（沿用 `auth_service` 的约定）：HTTP 层的
    文件流（`UploadFile`）由 `api/v1/datasets.py` 消费，本模块只接受**路径**。
  · 只 import `dao` / `algo`（**禁止** `algo.train`）；`dao` 不得反向 import 本模块。
  · 所有「能不能做」的判断都在这里，路由层不得重复实现。

🔒 三条不许踩的线
-----------------------------------------------------------------------------
1. **校验顺序不可调整**（`FR-UPL-09` / PRD §3.2.2「确定值，不可调整」）
   扩展名 → 大小/行数 → 表头完整性 → 日期可解析性 → 销量非负 → 时间连续性 → 序列数上限
   任一步失败即终止，**不继续后续校验**。顺序取自 `algo.config.VALIDATE_ORDER`，
   本模块**不得**另抄一份字面量列表（`VALIDATE_ORDER` 是 re-export，不是副本）。

2. 🔒 **仓库列缺失即拒**（`FR-UPL-05`）。`warehouse_id` 与其余三列同为必需项，
   缺任一都返回 `40001`，且建议文案必须点明「需使用英文列名
   `sku_id` / `warehouse_id` / `date` / `sales`」（`JT-01`）。

3. 🔒 **落盘名必须是 UUID**，禁止使用用户原始文件名（`NFR-SEC-07`）。
   原始文件名只进 `datasets.file_name`（展示用），落盘名进 `stored_name`。
   目录按 `u{user_id}` 分片（`WS-4-01`），游客是 `u0`。

落库模型（`EP-4` 裁定 A —— ⚠️ 与技术文档 `API-06-2` 的字面表述存在偏离，已登记）
-----------------------------------------------------------------------------
技术文档 `API-06-2` 写「校验失败不落盘、不写库」，但**同一份文档**的 `API-11`
（`/datasets/{dataset_id}/errors`）与断档处置（`ResolveGapRequest{dataset_id, action}`）
都以「已存在 `dataset_id`」为前提；`UX-P05` 与原型 `upload.html` 又要求在
「日期异常 / 负值」失败卡里就能点「下载错误明细 CSV」。三者不可兼得，故收敛为：

    文件**完整落盘** → 一律写 `datasets` 行（三态）
        校验通过          status =  1，`gap_filled` 视断档处置而定
        断档待用户确认     status =  1，`gap_filled = 0`，`gap_count > 0`
        内容级校验失败     status = -1（作废，但保留行以便下载错误明细与审计留痕）
    文件**未完整落盘** → 不写行（无内容可校验，行数据也无从导出）
        ① 扩展名非 `.csv`（第 1 步，落盘前即拒）
        ② 文件大小超上限（第 2 步，流式中断并删除半成品）

    ⚠️ 因此 `ValidationErrorData.dataset_id` 可能是 `None`，前端必须判空。

🔒 归属判定顺序（`BE-41`，两个分支不可交换）
    dataset.user_id == 0        → 放行（游客数据匿名共享，任何调用者均可用）
    dataset.user_id != caller   → `40301`
    写成「两者任一为 0 即放行」会击穿真实用户之间的隔离（`NFR-SEC-02` 红线）；
    漏掉第一个分支则会让「游客上传 → 登录后继续」的链路断裂（`FR-USR-08`）。
=============================================================================
"""
from __future__ import annotations

import csv
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.algo import config as algo_config
from app.algo.classify import classify_series, count_levels
from app.algo.preprocess import interpolate_series
from app.core import config as app_config
from app.core import csv_safety
from app.core.db import EXPORT_TMP_DIR, UPLOAD_DIR
from app.core.errors import BizError, ErrorCode
from app.dao import dataset_dao, oplog_dao
from app.models.dataset import STATUS_ACTIVE, STATUS_INVALID, Dataset
from app.models.operation_log import ACTION_UPLOAD, RESULT_FAIL, RESULT_SUCCESS
from app.models.user import GUEST_USER_ID
from app.services import cost_model
from app.validators import (
    ALLOWED_SUFFIX,
    MAX_ERROR_ROWS_EXPORT,
    MAX_ERROR_ROWS_RETURNED,
    MAX_GAP_RANGES_RETURNED,
    PREVIEW_ROWS,
    Inspection,
    build_preview,
    check_extension,
    inspect_file,
    load_csv,
    parse_dates,
    fmt_num,
)

logger = logging.getLogger(__name__)

settings = app_config.settings

__all__ = [
    "VALIDATE_ORDER",
    "REQUIRED_COLUMNS",
    "ALLOWED_SUFFIX",
    "UPLOAD_CHUNK_BYTES",
    "MAX_GAP_RANGES_RETURNED",
    "MAX_ERROR_ROWS_RETURNED",
    "OVER_BUDGET_SERIES",
    "inspect_file",
    "check_extension",
    "allocate_upload_path",
    "user_upload_dir",
    "new_upload_id",
    "safe_unlink",
    "register_upload",
    "load_sample",
    "resolve_gap",
    "get_detail",
    "get_preview",
    "download_error_detail",
    "error_detail_path",
    "prune_error_details",
    "assert_dataset_readable",
    "readable_or_404",
    "estimate_ms",
    "build_upload_payload",
    "TEMPLATE_CSV_BODY",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
# 🔒 7 步顺序的唯一真源：直接 re-export `algo.config.VALIDATE_ORDER`。
#    刻意不写第二份字面量——本项目已经踩过「两套数字 / 两份定义」的坑，
#    顺序一旦分裂，PRD §3.2.2 的「不可调整」就无从保证。
VALIDATE_ORDER: tuple[str, ...] = algo_config.VALIDATE_ORDER
REQUIRED_COLUMNS: tuple[str, ...] = algo_config.REQUIRED_COLUMNS

# 校验相关阈值与提示文案的唯一真源在 `app/validators/constants.py`（`R-06` 拆包后），
# 这里**只 import 不重定义** —— 否则又会出现「两套数字」。
# 仍留在本模块的只有「非校验」的常量（上传分块、预算预警、模板行数）。
UPLOAD_CHUNK_BYTES = 1 << 20          # 1 MiB：流式落盘的块大小（FR-UPL-04）
OVER_BUDGET_SERIES = 800              # API-09：序列数 > 800 置 over_budget_risk
TEMPLATE_SAMPLE_ROWS = 2              # FR-UPL-08：模板 = 表头 + 2 行示例

# PRD §3.2.2 的英文列名提示（JT-01 要求「中文列名文件被拒」时明确指出这一点）
EN_COLUMN_HINT = (
    "本系统只接受英文列名 sku_id / warehouse_id / date / sales（区分大小写），"
    "不支持中文列名。请下载标准模板后按模板填写"
)

# API-08 模板内容（表头 + 2 行合法示例，用户直接提交应能通过校验）
TEMPLATE_CSV_BODY = (
    "sku_id,warehouse_id,date,sales\r\n"
    "item_0001,上海仓,2015-01-01,120\r\n"
    "item_0001,上海仓,2015-01-02,135\r\n"
)

# =============================================================================
# 路径与命名（WS-4-01 / NFR-SEC-07）
# =============================================================================
def user_upload_dir(user_id: int) -> Path:
    """按 `u{user_id}` 分片的上传目录（游客为 `u0`）。"""
    return UPLOAD_DIR / f"u{user_id}"


def new_upload_id() -> str:
    """本次上传请求的 UUID（落盘名 + 错误明细文件名的组成部分，`JT-11`）。"""
    return uuid.uuid4().hex


def allocate_upload_path(user_id: int, upload_id: str) -> Path:
    """🔒 生成落盘路径：目录 `u{user_id}`、文件名 UUID —— 绝不用用户原始文件名。"""
    return user_upload_dir(user_id) / f"{upload_id}{ALLOWED_SUFFIX}"


def _rel(p: Path) -> str:
    """转成相对工作区根目录的路径（数据库设计要求 `file_path` 存相对路径）。"""
    try:
        return p.resolve().relative_to(settings.base_dir.resolve()).as_posix()
    except (ValueError, OSError):
        return p.as_posix()


def _abs(dataset: Dataset) -> Path:
    """由 `datasets.file_path` 还原绝对路径（容忍历史数据里的绝对路径）。"""
    p = Path(dataset.file_path)
    return p if p.is_absolute() else settings.base_dir / p


def _safe_unlink(p: Path) -> bool:
    """尽力删除，失败只告警不抛。

    ⚠️ 刻意不抛异常：删除失败（文件被占用 / 权限）不应该让「放弃上传」这个
       用户操作变成 500。残留文件由 TTL 清理任务兜底（技术文档 §3.8）。
    """
    try:
        p.unlink(missing_ok=True)
        return True
    except OSError as exc:  # pragma: no cover - 平台相关
        logger.warning("临时文件删除失败，交由清理任务兜底：%s (%s)", p, exc)
        return False


# 公开别名：HTTP 层（`api/v1/datasets.py`）在流式中断时也要清理半成品，
# 但下划线名不应跨模块引用，故显式导出一个同语义的公开名。
safe_unlink = _safe_unlink


# =============================================================================
# 归属判定（🔒 BE-41，两个分支顺序不可交换）
# =============================================================================
def assert_dataset_readable(dataset: Dataset, caller_id: int) -> None:
    """🔒 数据集读权限判定。**分支顺序不可交换。**

    :param caller_id: 当前调用者身份；游客为 `GUEST_USER_ID (0)`。
    """
    if dataset.user_id == GUEST_USER_ID:
        return  # 游客数据匿名共享，任何调用者均可用（BE-41 第一分支）
    if dataset.user_id != caller_id:
        raise BizError(ErrorCode.FORBIDDEN, "无权访问该数据集")


def _load_readable(db: Session, dataset_id: int, caller_id: int) -> Dataset:
    dataset = dataset_dao.get_by_id(db, dataset_id)
    if dataset is None:
        raise BizError(ErrorCode.DATASET_NOT_FOUND, "数据集不存在或已被清理")
    assert_dataset_readable(dataset, caller_id)
    return dataset


def readable_or_404(db: Session, dataset_id: int, caller_id: int) -> Dataset:
    """对外暴露的「取可读数据集」入口（路由层用，避免重复实现归属判定）。"""
    return _load_readable(db, dataset_id, caller_id)


# =============================================================================
# 落库编排（WS-4-07）
# =============================================================================
def register_upload(
    db: Session,
    *,
    path: Path,
    size_bytes: int,
    original_name: str,
    upload_id: str,
    user_id: int,
    gap_action: str | None = None,
    ip: str | None = None,
) -> dict[str, Any]:
    """一次上传的完整落库流程，返回 `API-06` 的响应 `data`。

    失败时抛 `BizError`（已按裁定 A 建好 `status=-1` 行并写出错误明细）。
    """
    inspection = inspect_file(path, size_bytes=size_bytes)

    # ---- 建行（文件已完整落盘）----
    dataset = dataset_dao.create(
        db,
        user_id=user_id,
        file_name=original_name,
        stored_name=path.name,
        file_path=_rel(path),
        file_size=size_bytes,
        is_sample=0,
    )
    stats = dict(inspection.stats)
    if stats:
        dataset_dao.update_stats(db, dataset.id, **stats)
        db.refresh(dataset)

    # ---- 内容级校验失败：置 -1 + 写错误明细 + 审计留痕 ----
    if not inspection.ok:
        issue = inspection.issue
        assert issue is not None
        dataset_dao.set_status(db, dataset.id, STATUS_INVALID)
        db.refresh(dataset)
        detail_file = None
        if issue.error_rows:
            detail_file = _write_error_detail(
                dataset.id, upload_id, inspection.error_columns, issue.error_rows
            )
        oplog_dao.record(
            db,
            action=ACTION_UPLOAD,
            result=RESULT_FAIL,
            user_id=user_id,
            target_id=dataset.id,
            detail=(
                f"sub=validate_fail;code={issue.code};stage={issue.fail_stage};"
                f"detail={detail_file.name if detail_file else 'none'}"
            ),
            ip=ip,
        )
        data = dict(issue.data)
        data["dataset_id"] = dataset.id
        # 🔒 必须显式提交后再抛（`BE-17` 的事务边界说明）：
        #    `core.deps.get_db` 对任何异常——**包括 BizError**——都会走 `rollback`，
        #    这是 EP-2 刻意为之（避免「业务失败但数据已落库」）。但 EP-4 裁定 A
        #    要求失败记录与错误明细**必须留存**（否则 `API-11` 永远拿不到明细），
        #    故此处先把事务落定，随后 get_db 的 rollback 已是空操作。
        #    —— 漏掉这一行的后果是：5 类错误全部"复现得了但查不到"，实测踩到。
        db.commit()
        raise BizError(issue.code, issue.message, data)

    # ---- 断档：不自动继续（FR-UPL-13），除非调用方已显式传入 gap_action ----
    if inspection.gap_count > 0:
        dataset_dao.update_stats(db, dataset.id, gap_count=inspection.gap_count)
        db.refresh(dataset)

        if gap_action == "interpolate":
            return _apply_interpolation(
                db, dataset, upload_id=upload_id, user_id=user_id, ip=ip
            )
        if gap_action == "abort":
            dataset_dao.set_status(db, dataset.id, STATUS_INVALID)
            _safe_unlink(path)
            oplog_dao.record(
                db,
                action=ACTION_UPLOAD,
                result=RESULT_FAIL,
                user_id=user_id,
                target_id=dataset.id,
                detail=f"sub=gap_abort;dataset={dataset.id};gap={inspection.gap_count}",
                ip=ip,
            )
            db.commit()   # 同上：作废状态必须留存，否则 rollback 会让它「复活」
            raise BizError(
                ErrorCode.DATE_GAP,
                "已按你的选择放弃本次上传，数据未被采用",
                {
                    "fail_stage": VALIDATE_ORDER[5],
                    "gap_ranges": inspection.gap_ranges,
                    "series_count": inspection.stats.get("valid_series_count"),
                    "suggestion": "原文件未保留。修正数据后可随时重新上传",
                },
            )
        oplog_dao.record(
            db,
            action=ACTION_UPLOAD,
            result=RESULT_SUCCESS,
            user_id=user_id,
            target_id=dataset.id,
            detail=(
                f"sub=gap_pending;dataset={dataset.id};gap={inspection.gap_count};"
                f"days={inspection.gap_days_total}"
            ),
            ip=ip,
        )
        # 🔒 「待确认」行必须落定：前端紧接着要拿 dataset_id 调 POST /datasets/gap。
        #    不提前 commit 的话，get_db 会因这次 40005 把整条记录回滚掉，
        #    用户点「自动插值补齐」时得到 40402「数据集不存在」（实测踩到）。
        db.commit()
        raise BizError(
            ErrorCode.DATE_GAP,
            f"检测到 {inspection.gap_count} 处日期断档，需确认处理方式",
            {
                "fail_stage": VALIDATE_ORDER[5],
                "gap_ranges": inspection.gap_ranges,
                "series_count": inspection.stats.get("valid_series_count"),
                # 🔒 必须回带 dataset_id：前端要凭它调 POST /datasets/gap 处置断档。
                #    漏掉这一行会让「自动插值补齐」按钮点下去报 40012（参数 dataset_id 不合法）。
                "dataset_id": dataset.id,
                "suggestion": (
                    "请选择「自动插值补齐」或「放弃上传」。选择插值将用线性插值"
                    "填补缺失日期，未选择前系统不会继续"
                ),
            },
        )

    # ---- 通过 ----
    oplog_dao.record(
        db,
        action=ACTION_UPLOAD,
        result=RESULT_SUCCESS,
        user_id=user_id,
        target_id=dataset.id,
        detail=(
            f"sub=validated;dataset={dataset.id};rows={dataset.row_count};"
            f"series={dataset.valid_series_count}"
        ),
        ip=ip,
    )
    return build_upload_payload(dataset, inspection.preview, interpolated=False)


def build_upload_payload(
    dataset: Dataset, preview: dict[str, Any], *, interpolated: bool
) -> dict[str, Any]:
    """组装 `API-06` / `API-07` / 断档处置共用的响应 `data`。"""
    from app.schemas import DatasetSummary  # 延迟导入：service 不参与模型定义

    gap_count = int(dataset.gap_count or 0)
    return {
        "dataset": DatasetSummary.model_validate(dataset),
        "preview": preview,
        "series_count": int(dataset.valid_series_count or 0),
        "series_limit": settings.max_series,
        "gap_warning": {
            # 「待确认」= 有断档 且 尚未插值；插值后 has_gap 归 false
            "has_gap": bool(gap_count > 0 and not interpolated),
            "gap_count": gap_count,
            "ranges": [],
            "interpolated": bool(interpolated),
        },
    }


# =============================================================================
# 断档处置：插值补齐（FR-UPL-13 / WS-4-05）
# =============================================================================
def resolve_gap(
    db: Session,
    *,
    dataset_id: int,
    action: str,
    user_id: int,
    ip: str | None = None,
) -> dict[str, Any] | None:
    """`FR-UPL-13`：由用户显式选择断档处置方式。`abort` 返回 `None`。"""
    dataset = _load_readable(db, dataset_id, user_id)
    if int(dataset.gap_filled or 0) == 1 or int(dataset.gap_count or 0) <= 0:
        raise BizError(
            ErrorCode.BAD_PARAM,
            "该数据集没有待确认的日期断档",
            {
                "fail_stage": VALIDATE_ORDER[5],
                "suggestion": "可直接进入「确认预测参数」；若需重新校验，请重新上传文件",
            },
        )

    if action == "abort":
        dataset_dao.set_status(db, dataset.id, STATUS_INVALID)
        _safe_unlink(_abs(dataset))
        oplog_dao.record(
            db,
            action=ACTION_UPLOAD,
            result=RESULT_FAIL,
            user_id=user_id,
            target_id=dataset.id,
            detail=f"sub=gap_abort;dataset={dataset.id}",
            ip=ip,
        )
        return None

    return _apply_interpolation(db, dataset, upload_id=None, user_id=user_id, ip=ip)


def _apply_interpolation(
    db: Session,
    dataset: Dataset,
    *,
    upload_id: str | None,
    user_id: int,
    ip: str | None,
) -> dict[str, Any]:
    """对每条序列做**内部线性插值**补齐缺失日期，另存新文件并置 `gap_filled=1`。

    设计取舍：
      · **另存新文件**而不是就地覆盖 —— 原始 CSV 保留在 `data/uploads/u{uid}/`，
        满足 `FR-SYS-08`「原始 CSV 保留」，也让「放弃上传」不需要回滚。
      · 插值产物**只含 4 个必需列**：额外列在缺失日期上没有语义可循，
        强行前向填充等于凭空造数据。该行为已在实施方案「偏离登记」中披露。
    """
    src = _abs(dataset)
    df, enc = load_csv(src)
    if not src.exists():
        raise BizError(ErrorCode.IO_ERROR, "原始数据文件已丢失，请重新上传")

    parsed = parse_dates(df[algo_config.COL_DATE])
    sales = pd.to_numeric(df[algo_config.COL_SALES], errors="coerce")
    frame = df.assign(**{"_d": parsed, "_s": sales}).dropna(
        subset=["_d", "_s"]
    )
    frame = frame.sort_values(
        [algo_config.COL_SKU, algo_config.COL_WAREHOUSE, "_d"], kind="mergesort"
    )

    filled_rows: list[dict[str, str]] = []
    for (sku, wh), group in frame.groupby(
        [algo_config.COL_SKU, algo_config.COL_WAREHOUSE], observed=True, sort=False
    ):
        grid = pd.date_range(group["_d"].min(), group["_d"].max(), freq="D")
        series = group.set_index("_d")["_s"].reindex(grid)
        # 🔒 用算法层同一份插值实现，保证「上传时补齐」与「推理前补齐」口径一致
        values = interpolate_series(series.to_numpy(dtype=float))
        for day, value in zip(grid, values):
            filled_rows.append(
                {
                    algo_config.COL_SKU: str(sku),
                    algo_config.COL_WAREHOUSE: str(wh),
                    algo_config.COL_DATE: day.strftime("%Y-%m-%d"),
                    algo_config.COL_SALES: fmt_num(value),
                }
            )

    stem = Path(dataset.stored_name).stem or new_upload_id()
    filled_path = user_upload_dir(dataset.user_id) / f"{stem}_filled.csv"
    filled_path.parent.mkdir(parents=True, exist_ok=True)
    with filled_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                algo_config.COL_SKU,
                algo_config.COL_WAREHOUSE,
                algo_config.COL_DATE,
                algo_config.COL_SALES,
            ],
        )
        writer.writeheader()
        writer.writerows(filled_rows)

    # 用同一份校验实现复核插值产物（断档应为 0，且给出权威的统计与预览）
    recheck = inspect_file(filled_path, size_bytes=filled_path.stat().st_size)
    if not recheck.ok:
        _safe_unlink(filled_path)
        issue = recheck.issue
        assert issue is not None
        logger.error("插值产物未通过复检：code=%s stage=%s", issue.code, issue.fail_stage)
        raise BizError(
            ErrorCode.IO_ERROR,
            "插值结果自检未通过，已回滚本次操作，请修正原始数据后重传",
            {"fail_stage": issue.fail_stage, "suggestion": "请检查原始文件的日期与销量列"},
        )

    dataset_dao.update_stats(
        db,
        dataset.id,
        gap_filled=1,
        **recheck.stats,
    )
    # `stored_name` / `file_path` 不在 update_stats 的字段白名单里（它只回填统计），
    # 必须随文件一起改，否则会指向已弃用的原始文件。
    dataset.stored_name = filled_path.name
    dataset.file_path = _rel(filled_path)
    db.flush()
    db.refresh(dataset)

    oplog_dao.record(
        db,
        action=ACTION_UPLOAD,
        result=RESULT_SUCCESS,
        user_id=user_id,
        target_id=dataset.id,
        detail=(
            f"sub=gap_interpolate;dataset={dataset.id};from={src.name};"
            f"to={filled_path.name};rows={dataset.row_count}"
            + (f";upload={upload_id}" if upload_id else "")
        ),
        ip=ip,
    )
    return build_upload_payload(dataset, recheck.preview, interpolated=True)


# =============================================================================
# API-07 内置示例数据（幂等，JT-10 / API-07-1）
# =============================================================================
_DEMO_CACHE: dict[str, Any] = {}


def _demo_inspection(path: Path) -> Inspection:
    """示例数据的巡检结果（按文件 mtime 缓存，避免每次调用都全表解析）。"""
    stamp = f"{path}:{path.stat().st_mtime_ns if path.exists() else 0}"
    cached = _DEMO_CACHE.get("inspection")
    if _DEMO_CACHE.get("stamp") == stamp and isinstance(cached, Inspection):
        return cached
    inspection = inspect_file(path, size_bytes=path.stat().st_size)
    _DEMO_CACHE["stamp"] = stamp
    _DEMO_CACHE["inspection"] = inspection
    return inspection


def load_sample(
    db: Session, *, user_id: int, ip: str | None = None
) -> dict[str, Any]:
    """`API-07`：一键加载内置示例数据。**幂等键是 `(user_id, is_sample=1)`**。

    ⚠️ 幂等键**不是** `is_sample = 1`：否则游客先创建后，登录用户会复用游客的
       记录，导致历史列表归属错乱（`API-07-1`）。
    """
    existing = dataset_dao.get_sample_for_user(db, user_id)
    if existing is not None:
        preview_total = existing.row_count or 0
        return _sample_payload(existing, preview_total, interpolated=False)

    src = Path(settings.demo_csv)
    if not src.exists():
        raise BizError(
            ErrorCode.IO_ERROR,
            "内置示例数据文件缺失，请联系管理员",
            {"suggestion": f"期望文件位置：{_rel(src)}"},
        )
    inspection = _demo_inspection(src)
    if not inspection.ok:
        issue = inspection.issue
        assert issue is not None
        logger.error("内置示例数据未通过自检：%s", issue.message)
        raise BizError(
            ErrorCode.IO_ERROR,
            "内置示例数据自检未通过，请联系管理员",
            {"fail_stage": issue.fail_stage},
        )

    dataset = dataset_dao.create(
        db,
        user_id=user_id,
        file_name=src.name,
        stored_name=src.name,
        file_path=_rel(src),
        file_size=src.stat().st_size,
        is_sample=1,
    )
    dataset_dao.update_stats(db, dataset.id, **inspection.stats)
    db.refresh(dataset)
    oplog_dao.record(
        db,
        action=ACTION_UPLOAD,
        result=RESULT_SUCCESS,
        user_id=user_id,
        target_id=dataset.id,
        detail=(
            f"sub=sample;dataset={dataset.id};rows={dataset.row_count};"
            f"series={dataset.valid_series_count}"
        ),
        ip=ip,
    )
    return build_upload_payload(dataset, inspection.preview, interpolated=False)


def _sample_payload(
    dataset: Dataset, preview_total: int, *, interpolated: bool
) -> dict[str, Any]:
    """幂等命中时也要给出预览（复用落盘文件的前 20 行）。"""
    src = _abs(dataset)
    if src.exists():
        df, _enc = load_csv(src, None)
        preview = build_preview(df)
    else:  # pragma: no cover - 共享文件被人为删除时的降级
        preview = {
            "columns": list(REQUIRED_COLUMNS),
            "rows": [],
            "total": int(preview_total),
            "shown": 0,
        }
    return build_upload_payload(dataset, preview, interpolated=interpolated)


# =============================================================================
# API-09 / API-10 详情与预览
# =============================================================================
_EVAL_REPORT: dict[str, Any] | None = None


def _eval_report() -> dict[str, Any]:
    """读取算法侧产出的评估报告（`API-09` 的耗时预估基准）。"""
    global _EVAL_REPORT
    if _EVAL_REPORT is None:
        path = settings.base_dir / "outputs" / "eval_report.json"
        try:
            _EVAL_REPORT = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("评估报告不可读，耗时预估退化为经验公式：%s", exc)
            _EVAL_REPORT = {}
    return _EVAL_REPORT


def estimate_ms(series_count: int) -> int:
    """参数页耗时预估（`API-09`）。

    🔒 不写死实测数值：基准取自 `outputs/eval_report.json` 的
       `timing_ms.total_ms` 与 `coverage.total`，按序列数线性折算，
       避免出现「界面一套数字、算法另一套数字」（技术文档 `GIT-7`）。
    """
    report = _eval_report()
    try:
        base_ms = int(report["timing_ms"]["total_ms"])
        base_series = int(report["coverage"]["total"])
    except (KeyError, TypeError, ValueError):
        base_ms, base_series = 0, 0
    if base_ms > 0 and base_series > 0:
        return max(int(round(base_ms * max(series_count, 1) / base_series)), 200)
    # 报告缺失时的保底估算式（固定开销 + 每序列成本），非实测值
    return 1_500 + 30 * max(series_count, 1)


def get_detail(db: Session, *, dataset_id: int, user_id: int) -> dict[str, Any]:
    """`API-09`：数据集概要 + 序列分级预判 + 耗时预估。

    ⚠️ 会执行一次完整读取与分级（`API-09-2`：50 万行约 2 秒）。
       参数页应在数据集加载后调用一次，**不要**在每次参数变更时重复调用。
    """
    from app.schemas import DatasetSummary

    dataset = _load_readable(db, dataset_id, user_id)
    path = _abs(dataset)
    if not path.exists():
        raise BizError(ErrorCode.IO_ERROR, "数据文件已丢失，请重新上传")

    df, _enc = load_csv(path, None)
    df = df.assign(**{"_s": pd.to_numeric(df[algo_config.COL_SALES], errors="coerce")})
    infos = classify_series(df)
    counts = count_levels(infos)
    return {
        "dataset": DatasetSummary.model_validate(dataset),
        "level_preview": counts.as_dict(),
        "estimated_ms": estimate_ms(counts.total),
        "over_budget_risk": counts.total > OVER_BUDGET_SERIES,
        # 🆕 EP-5：真实任务的端到端预估（随步长变化）。参数页展示的是这一项，
        #    而不是上面的 `estimated_ms`（单遍算法评测口径），
        #    否则会出现「预估 7.9 s、实际 16.6 s」的两套数字（GIT-7）。
        "estimate_by_horizon": cost_model.estimate_by_horizon(
            counts.total, algo_config.HORIZONS
        ),
    }


def get_preview(
    db: Session,
    *,
    dataset_id: int,
    user_id: int,
    limit: int = PREVIEW_ROWS,
    keyword: str | None = None,
) -> dict[str, Any]:
    """`API-10`：数据预览（`FR-UPL-07`），支持按商品 / 仓库搜索（`API-10-2`）。

    `total` 恒为**数据集总行数**（`API-10-1`），不是本次返回数、也不是命中数；
    `shown` 为本次实际返回行数。
    """
    dataset = _load_readable(db, dataset_id, user_id)
    path = _abs(dataset)
    if not path.exists():
        raise BizError(ErrorCode.IO_ERROR, "数据文件已丢失，请重新上传")

    limit = max(1, min(int(limit), 100))
    df, _enc = load_csv(path, None)
    total = int(dataset.row_count if dataset.row_count is not None else len(df))

    if keyword:
        needle = keyword.strip().lower()
        if needle:
            mask = (
                df[algo_config.COL_SKU].astype(str).str.lower().str.contains(needle, regex=False)
                | df[algo_config.COL_WAREHOUSE]
                .astype(str)
                .str.lower()
                .str.contains(needle, regex=False)
            )
            df = df.loc[mask]

    preview = build_preview(df, rows=limit)
    preview["total"] = total
    return preview


# =============================================================================
# API-11 错误明细（JT-11：data/exports/tmp/，TTL 1 小时，与正式导出分目录）
# =============================================================================
def error_detail_path(dataset_id: int, upload_id: str) -> Path:
    """🔒 临时错误明细的路径（`JT-11` 的命名原样落地）。"""
    return EXPORT_TMP_DIR / f"dataset_{dataset_id}_{upload_id}.csv"


def _write_error_detail(
    dataset_id: int, upload_id: str, columns: list[str], rows: list[dict[str, Any]]
) -> Path:
    """把不合格行导出为 CSV（`FR-UPL-06`：仅含不合格行 + 保留原始列 + 追加 `error_reason`）。

    ⚠️ 必须落盘而不是塞进 `operation_logs.detail`（`API-11-3`）：最坏情况
       500,000 行全部非法，约数十 MB，入库会污染审计表。
    """
    EXPORT_TMP_DIR.mkdir(parents=True, exist_ok=True)
    target = error_detail_path(dataset_id, upload_id)
    fieldnames = list(columns)
    for extra in ("row_no", "error_reason"):
        if extra not in fieldnames:
            fieldnames.append(extra)
    # BOM（utf-8-sig）：Excel 双击打开不乱码（NFR-COMP-05）
    with target.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows[:MAX_ERROR_ROWS_EXPORT]:
            # 🔴 CSV 注入防护：这里下载的是**用户上传的原始行**（含原始
            #    `sku_id`），威胁模型与结果导出（API-16）完全相同 ——
            #    之前只有 API-16 做了防护，这里漏了。统一走 `core.csv_safety`。
            #    数值列（`sales`）**不转义**，否则 `-12` 会变成 `'-12`。
            writer.writerow(
                csv_safety.escape_row(
                    row, fieldnames, numeric_columns=(algo_config.COL_SALES,)
                )
            )
    prune_error_details()
    return target


def download_error_detail(db: Session, *, dataset_id: int, user_id: int) -> tuple[Path, str]:
    """`API-11`：返回 `(文件路径, 下载文件名)`。文件不存在或已过期都报 40402。"""
    dataset = _load_readable(db, dataset_id, user_id)
    if not EXPORT_TMP_DIR.exists():
        raise BizError(ErrorCode.DATASET_NOT_FOUND, "错误明细不存在或已过期")
    matches = sorted(
        EXPORT_TMP_DIR.glob(f"dataset_{dataset.id}_*.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise BizError(
            ErrorCode.DATASET_NOT_FOUND,
            "错误明细不存在或已过期（临时文件保留 1 小时）",
            {
                "fail_stage": "错误明细下载",
                "suggestion": "校验通过的数据集不会产生错误明细；若确实存在失败行，请重新上传后再下载",
            },
        )
    target = matches[0]
    if not _is_fresh(target):
        _safe_unlink(target)
        raise BizError(
            ErrorCode.DATASET_NOT_FOUND,
            "错误明细已超过 1 小时的保留期，请重新上传后下载",
            {"fail_stage": "错误明细下载", "suggestion": "重新上传同一文件即可再次生成明细"},
        )
    return target, f"错误明细_{dataset.id}.csv"


def _is_fresh(path: Path) -> bool:
    try:
        age = datetime.now().timestamp() - path.stat().st_mtime
    except OSError:  # pragma: no cover
        return False
    return age <= settings.export_tmp_ttl_seconds


def prune_error_details() -> int:
    """清理超过 TTL 的临时错误明细（`JT-11`：1 小时）。

    ⚠️ 只在写入时顺手清理，不引入定时器：本项目的部署形态是单进程 uvicorn，
       为一个低频临时目录引入后台线程得不偿失；`scripts/cleanup_tmp.py`
       提供了手动 / 运维入口。
    """
    if not EXPORT_TMP_DIR.exists():
        return 0
    removed = 0
    for item in EXPORT_TMP_DIR.glob("dataset_*.csv"):
        if not _is_fresh(item):
            if _safe_unlink(item):
                removed += 1
    return removed


# 显式引用，避免 linter 因「ACTION_UPLOAD 仅用于 re-export」而报未使用
_ = (STATUS_ACTIVE, ACTION_UPLOAD, np)
