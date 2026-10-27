"""`operation_logs` 表数据访问（`DB-T07`）。

🔒 纯 CRUD。写入函数**不抛异常**由调用方（`services`）决定；但按 `FR-SYS-03`
   审计要求，业务主流程失败也必须留下痕迹，故提供 `record()` 统一入口。

🔒 本模块不得 `import algo`。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.operation_log import (
    ACTION_ADMIN,
    ALL_ACTIONS,
    RESULT_FAIL,
    RESULT_SUCCESS,
    OperationLog,
)

__all__ = [
    "ACTION_ADMIN",
    "ALL_ACTIONS",
    "RESULT_SUCCESS",
    "RESULT_FAIL",
    "record",
    "list_paged",
    "count",
    "count_recent",
    "count_by_action",
]


def record(
    db: Session,
    *,
    action: str,
    result: str = RESULT_SUCCESS,
    user_id: int | None = None,
    target_id: str | int | None = None,
    detail: str | None = None,
    ip: str | None = None,
) -> OperationLog:
    """写入一条操作日志。

    :param user_id: **可为 0（游客）或 None（完全未登录）**。注意取值域是
                    「≥ 0 或 NULL」，不是「≥ 1 或 NULL」——游客操作同样要留痕。
    :param detail:  管理端子动作的承载字段（`action='admin'` 时），
                    形如 `sub=user_disable;target_user=3;by=1`。
    """
    row = OperationLog(
        user_id=user_id,
        action=action,
        target_id=None if target_id is None else str(target_id),
        result=result,
        detail=detail,
        ip=ip,
    )
    db.add(row)
    db.flush()
    return row


def list_paged(
    db: Session,
    *,
    page: int = 1,
    size: int = 20,
    action: str | None = None,
    result: str | None = None,
    user_id: int | None = None,
    start: str | None = None,
    end: str | None = None,
) -> list[OperationLog]:
    """操作日志查询（管理端 `API-30`）。"""
    stmt = select(OperationLog)
    if action:
        stmt = stmt.where(OperationLog.action == action)
    if result:
        stmt = stmt.where(OperationLog.result == result)
    if user_id is not None:
        stmt = stmt.where(OperationLog.user_id == user_id)
    if start:
        stmt = stmt.where(OperationLog.created_at >= start)
    if end:
        stmt = stmt.where(OperationLog.created_at <= end)
    stmt = (
        stmt.order_by(OperationLog.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    return list(db.execute(stmt).scalars().all())


def count(
    db: Session,
    *,
    action: str | None = None,
    result: str | None = None,
    user_id: int | None = None,
    start: str | None = None,
    end: str | None = None,
) -> int:
    """操作日志计数（管理端）。

    🔴 **筛选参数必须与 `list_paged` 完全一致**（2026-10-26 复评修复）：
    之前不接 `start`/`end`，于是「按时间窗筛选」时 `items` 已过滤、
    `total` 却是全库条数 ⇒ 前端页数虚高、翻页空白。
    """
    stmt = select(func.count()).select_from(OperationLog)
    if action:
        stmt = stmt.where(OperationLog.action == action)
    if result:
        stmt = stmt.where(OperationLog.result == result)
    if user_id is not None:
        stmt = stmt.where(OperationLog.user_id == user_id)
    if start:
        stmt = stmt.where(OperationLog.created_at >= start)
    if end:
        stmt = stmt.where(OperationLog.created_at <= end)
    return int(db.execute(stmt).scalar_one())


def count_recent(
    db: Session,
    *,
    action: str,
    ip: str | None,
    since: str,
    result: str | None = RESULT_SUCCESS,
) -> tuple[int, str | None]:
    """统计某 IP 在 `since` 之后的成功操作条数，并返回窗口内最早一条的时间。

    🔒 存在的理由（`R-07` 注册限流）：计数必须落**库**而不是内存。
       「注册配额」如果只存在进程内存里，重启即清零 ⇒ 攻击者只要等一次
       发布/重启就能重置配额；且未来上多 worker 时各进程各算一套。

    :param since: `YYYY-MM-DD HH:MM:SS`（与 DDL 的 `created_at` 同格式，
                  字符串比较即时间比较，见 `NFR-COMP-04` 单时区假设）。
    :return: `(条数, 窗口内最早一条的 created_at 或 None)`
    """
    stmt = select(func.count(), func.min(OperationLog.created_at)).where(
        OperationLog.action == action,
        OperationLog.created_at >= since,
    )
    if result:
        stmt = stmt.where(OperationLog.result == result)
    # 用 `is_(None)` 而不是 `== None` 的显式分支：`ip` 为 NULL 时也要归到同一个桶，
    # 否则「取不到客户端 IP」的链路（如内部调用）会完全绕过限流。
    stmt = stmt.where(OperationLog.ip.is_(None) if ip is None else OperationLog.ip == ip)
    n, oldest = db.execute(stmt).one()
    return int(n or 0), oldest


def count_by_action(db: Session, *, since: str | None = None) -> dict[str, int]:
    """按动作分组计数（看板）。"""
    stmt = select(OperationLog.action, func.count()).group_by(OperationLog.action)
    if since:
        stmt = stmt.where(OperationLog.created_at >= since)
    return {a: n for a, n in db.execute(stmt).all()}
