"""`tasks` 表数据访问（`DB-T03`）。

🔒 纯 CRUD。所有用户侧查询必须携带 `user_id`（`NFR-SEC-02`）；
   带 `_all` 后缀的函数是管理端（`CH-01`）专用例外。

🔒 本模块不得 `import algo`。
"""
from __future__ import annotations

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models.task import (
    STATUS_CREATED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    Task,
)

__all__ = [
    "STATUS_CREATED",
    "STATUS_RUNNING",
    "STATUS_SUCCEEDED",
    "STATUS_FAILED",
    "get_by_id",
    "get_owned",
    "create",
    "mark_running",
    "mark_succeeded",
    "mark_failed",
    "update_counts",
    "soft_delete",
    "hard_delete",
    "list_by_user",
    "count_by_user",
    "list_history",
    "count_history",
    "list_all",
    "count_all",
    "list_active_versions",
    "success_rate",
]


def get_by_id(db: Session, task_id: int) -> Task | None:
    return db.get(Task, task_id)


def get_owned(db: Session, task_id: int, user_id: int) -> Task | None:
    """取「属于该用户且未被软删除」的任务。**隔离红线落实点。**"""
    stmt = select(Task).where(
        Task.id == task_id, Task.user_id == user_id, Task.is_deleted == 0
    )
    return db.execute(stmt).scalars().first()


def create(
    db: Session,
    *,
    user_id: int,
    dataset_id: int,
    horizon: int,
    model_version: str,
    task_name: str | None = None,
    use_holiday_feature: int = 1,
) -> Task:
    """创建任务（初始状态 `created`）。"""
    row = Task(
        user_id=user_id,
        dataset_id=dataset_id,
        horizon=horizon,
        model_version=model_version,
        task_name=task_name,
        use_holiday_feature=use_holiday_feature,
        status=STATUS_CREATED,
    )
    db.add(row)
    db.flush()
    return row


def mark_running(db: Session, task_id: int, *, started_at: str, device: str,
                 device_fallback: int, series_total: int | None) -> int:
    return (
        db.query(Task)
        .filter(Task.id == task_id)
        .update(
            {
                Task.status: STATUS_RUNNING,
                Task.started_at: started_at,
                Task.device: device,
                Task.device_fallback: device_fallback,
                Task.series_total: series_total,
            },
            synchronize_session=False,
        )
    )


def mark_succeeded(
    db: Session,
    task_id: int,
    *,
    finished_at: str,
    duration_ms: int,
    l1_count: int | None = None,
    l2_count: int | None = None,
    l3_count: int | None = None,
    device: str | None = None,
    device_fallback: int | None = None,
) -> int:
    """成功终态。

    🔒 `device` / `device_fallback` 必须在这里**再写一次**（`EP-9` 修复的真实缺陷）。
       `mark_running` 落的是**开工时**的设备，而 `NFR-REL-03` 的显存自愈恰恰发生
       在开工之后：GPU OOM → 清缓存 → 模型搬 CPU → 重跑成功。
       原实现只在内存态 `_Progress` 上记账，于是：

       · 当次响应里的 `device_fallback` 是对的（`_merge_task` 会合并内存态）；
       · 但 `tasks` 行永远停在 `device='cuda', device_fallback=0`，
         服务重启或用户从历史列表重新打开该任务时，**降级事实就丢了** ——
         运维再也统计不出"GPU 到底 OOM 过几次"，而这正是该字段存在的唯一理由。

       📌 只有 `is not None` 才覆盖，避免调用方不传时把 `mark_running` 写的值抹成 NULL。
    """
    values: dict[object, object] = {
        Task.status: STATUS_SUCCEEDED,
        Task.finished_at: finished_at,
        Task.duration_ms: duration_ms,
        Task.l1_count: l1_count,
        Task.l2_count: l2_count,
        Task.l3_count: l3_count,
        Task.error_code: None,
        Task.error_msg: None,
    }
    if device is not None:
        values[Task.device] = device
    if device_fallback is not None:
        values[Task.device_fallback] = int(device_fallback)

    return db.query(Task).filter(Task.id == task_id).update(
        values, synchronize_session=False
    )


def mark_failed(
    db: Session,
    task_id: int,
    *,
    finished_at: str,
    error_code: str,
    error_msg: str,
    completed_stage: str | None = None,
    duration_ms: int | None = None,
    device: str | None = None,
    device_fallback: int | None = None,
) -> int:
    """软失败：任务标记 failed，但已完成阶段的信息保留（`FR-SYS-02`）。

    `device` / `device_fallback` 的理由同 `mark_succeeded` ——
    「OOM 回退到 CPU 之后仍然超预算」这类组合恰恰是最需要被看见的情况。
    """
    values: dict[object, object] = {
        Task.status: STATUS_FAILED,
        Task.finished_at: finished_at,
        Task.error_code: error_code,
        Task.error_msg: error_msg,
        Task.completed_stage: completed_stage,
        Task.duration_ms: duration_ms,
    }
    if device is not None:
        values[Task.device] = device
    if device_fallback is not None:
        values[Task.device_fallback] = int(device_fallback)

    return db.query(Task).filter(Task.id == task_id).update(
        values, synchronize_session=False
    )


def update_counts(
    db: Session, task_id: int, *, l1_count: int, l2_count: int, l3_count: int
) -> int:
    """更新覆盖率计数（`FR-VIS-07`）。"""
    return (
        db.query(Task)
        .filter(Task.id == task_id)
        .update(
            {Task.l1_count: l1_count, Task.l2_count: l2_count, Task.l3_count: l3_count},
            synchronize_session=False,
        )
    )


def soft_delete(db: Session, task_id: int) -> int:
    """用户侧删除：仅置 `is_deleted = 1`（`FR-HIS-05`，可查回）。"""
    return (
        db.query(Task)
        .filter(Task.id == task_id)
        .update({"is_deleted": 1}, synchronize_session=False)
    )


def hard_delete(db: Session, task_id: int) -> int:
    """管理端强制删除：物理删除，`predictions` / `metrics` 由外键 CASCADE 连带删除
    （`API-27` / `CH-01`）。`datasets` 不受影响（外键为 RESTRICT）。"""
    return db.query(Task).filter(Task.id == task_id).delete(synchronize_session=False)


def list_by_user(
    db: Session, user_id: int, *, limit: int = 20, offset: int = 0
) -> list[Task]:
    """历史列表首屏（命中 `IX-03`）。"""
    stmt = (
        select(Task)
        .where(Task.user_id == user_id, Task.is_deleted == 0)
        .order_by(Task.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(db.execute(stmt).scalars().all())


def count_by_user(db: Session, user_id: int) -> int:
    stmt = (
        select(func.count())
        .select_from(Task)
        .where(Task.user_id == user_id, Task.is_deleted == 0)
    )
    return int(db.execute(stmt).scalar_one())


def list_all(
    db: Session,
    *,
    page: int = 1,
    size: int = 20,
    user_id: int | None = None,
    status: str | None = None,
    keyword: str | None = None,
) -> list[Task]:
    """全量任务列表（管理端 `API-26`）。**隔离红线例外，仅 admin.py 可调用。**"""
    stmt = select(Task)
    if user_id is not None:
        stmt = stmt.where(Task.user_id == user_id)
    if status is not None:
        stmt = stmt.where(Task.status == status)
    if keyword:
        stmt = stmt.where(Task.task_name.like(f"%{keyword}%"))
    stmt = stmt.order_by(Task.created_at.desc()).offset((page - 1) * size).limit(size)
    return list(db.execute(stmt).scalars().all())


def count_all(
    db: Session,
    *,
    user_id: int | None = None,
    status: str | None = None,
    keyword: str | None = None,
) -> int:
    """全量任务计数（管理端）。**隔离红线例外。**

    🔴 **筛选参数必须与 `list_all` 完全一致**（2026-10-26 复评修复）：
    之前 `count_all` 不接 `keyword`，于是「按关键字搜索」时 `items` 已过滤、
    `total` 却是全库条数 ⇒ 前端算出的页数虚高，用户翻页看到连续空白页。
    """
    stmt = select(func.count()).select_from(Task)
    if user_id is not None:
        stmt = stmt.where(Task.user_id == user_id)
    if status is not None:
        stmt = stmt.where(Task.status == status)
    if keyword:
        stmt = stmt.where(Task.task_name.like(f"%{keyword}%"))
    return int(db.execute(stmt).scalar_one())


# =============================================================================
# 历史记录（`API-17`）
# =============================================================================
# ⚠️ 为什么历史列表单独走 `v_task_summary` 视图而不复用上面的 `list_by_user`：
#    列表要展示 `dataset_name` + 6 项指标（`weighted_mape` / `mae_improve_pct` …），
#    走 `tasks` 就要再 JOIN `datasets` 与 `metrics` 三张表、在 ORM 层拼字段，
#    既与数据库设计文档的视图定义脱节，又容易把「指标缺失」和「任务缺失」混成 NULL。
#    视图已内置 `WHERE t.is_deleted = 0`（`API-17-1` 的红线），这里不再重复过滤。

# 🔒 排序固定 `created_at DESC`（`API-17-2`）—— 索引 `ix_tasks_user_list` 的
#    第三个分段就是它，改成别的会让 `NFR-PERF-08` 的 1 s 预算失效。
_HISTORY_SORT = "created_at DESC"


def _history_filters(
    user_id: int,
    *,
    keyword: str | None,
    date_from: str | None,
    date_to: str | None,
    status: str | None,
) -> list[str]:
    """构造历史列表的 WHERE 片段（参数化，不拼字符串）。

    🔒 `user_id` 永远第一个条件：即使漏传也只会查空集，不会退化成全表扫描。
    """
    where = ["user_id = :user_id"]
    if keyword:
        # 任务名 / 数据集名模糊搜索（`FR-HIS-01`）
        where.append("(task_name LIKE :kw OR dataset_name LIKE :kw)")
    if date_from:
        where.append("date(created_at) >= date(:date_from)")
    if date_to:
        where.append("date(created_at) <= date(:date_to)")
    if status:
        where.append("status = :status")
    return where


def list_history(
    db: Session,
    user_id: int,
    *,
    page: int = 1,
    size: int = 20,
    keyword: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """历史列表分页（`API-17`），走 `v_task_summary` 视图。

    🔒 返回 `dict`（视图行）而非 ORM 实体：这一层不做业务判断，
       口径归 `history_service` 管，DAO 只负责「取哪几行」。
    """
    where = _history_filters(
        user_id,
        keyword=keyword,
        date_from=date_from,
        date_to=date_to,
        status=status,
    )
    params: dict = {"user_id": user_id}
    if keyword:
        params["kw"] = f"%{keyword}%"
    if date_from:
        params["date_from"] = date_from
    if date_to:
        params["date_to"] = date_to
    if status:
        params["status"] = status

    sql = (
        "SELECT * FROM v_task_summary WHERE "
        + " AND ".join(where)
        + f" ORDER BY {_HISTORY_SORT} LIMIT :limit OFFSET :offset"
    )
    params["limit"] = size
    params["offset"] = (page - 1) * size
    rows = db.execute(text(sql), params).mappings().all()
    return [dict(r) for r in rows]


def count_history(
    db: Session,
    user_id: int,
    *,
    keyword: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
) -> int:
    """历史列表总条数。**筛选条件必须与 `list_history` 完全一致**，
    否则会出现「第 2 页空白但 total 说有 5 页」。"""
    where = _history_filters(
        user_id,
        keyword=keyword,
        date_from=date_from,
        date_to=date_to,
        status=status,
    )
    params: dict = {"user_id": user_id}
    if keyword:
        params["kw"] = f"%{keyword}%"
    if date_from:
        params["date_from"] = date_from
    if date_to:
        params["date_to"] = date_to
    if status:
        params["status"] = status

    sql = "SELECT COUNT(*) FROM v_task_summary WHERE " + " AND ".join(where)
    return int(db.execute(text(sql), params).scalar_one())


def list_active_versions(db: Session, limit: int = 20) -> list[Task]:
    """按创建时间列出最近任务（供看板 MAPE 走势，`API-22`）。"""
    stmt = select(Task).order_by(Task.created_at.desc()).limit(limit)
    return list(db.execute(stmt).scalars().all())


def success_rate(db: Session) -> float | None:
    """任务成功率 = succeeded / (succeeded + failed)。无已完成任务时返回 None。"""
    row = db.execute(
        text(
            "SELECT "
            "  SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END), "
            "  SUM(CASE WHEN status IN ('succeeded', 'failed') THEN 1 ELSE 0 END) "
            "FROM tasks"
        )
    ).one()
    done_ok, done_all = row[0] or 0, row[1] or 0
    if not done_all:
        return None
    return round(done_ok * 100.0 / done_all, 2)
