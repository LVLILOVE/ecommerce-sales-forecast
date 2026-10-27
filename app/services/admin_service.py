"""管理端服务（`API-22` ~ `API-30`，`CH-01` / `AD-P01` / `AD-P02`）。

=============================================================================
🔒 本模块是数据隔离红线的**唯一显式例外**（`BE-ADM-01`，`NFR-SEC-02`）
-----------------------------------------------------------------------------
普通用户接口的数据范围是 `user_id = 当前用户`；本模块的 9 个函数
都是**跨用户聚合**，不携带 `user_id` 过滤（除显式按用户筛选的 `list_users` /
`list_tasks` 外）。因此：

  1. 例外**只允许**被 `app/api/v1/admin.py` 调用 —— 该文件是唯一入口，
     且每个路由都必须声明 `CurrentAdmin` 依赖；
  2. 任何在 `auth.py` / `datasets.py` / `predictions.py` / `history.py` /
     `system.py` 里出现「不带 `user_id` 过滤的查询」都视为红线破坏；
  3. **四个写操作必须全部留痕**（`WS-8-05`）：启停用、角色变更、
     重置密码、强制删除、切换模型 —— 一律 `action='admin'`，
     `detail` 记「子动作 + 操作者 + 目标」。

🔒 与用户侧软删除的语义差异（`QD-03`）
-----------------------------------------------------------------------------
  用户侧 `API-20`：**软删除** `tasks.is_deleted = 1`，`predictions` /
    `metrics` 全部保留（`FR-HIS-05` 可追溯），界面上写「删除后可在回收路径恢复」。
  管理侧 `API-27`：**硬删除**物理行 + 级联 `predictions` / `metrics`，
    `datasets` **不动**（`ON DELETE RESTRICT`，可能被其他任务引用），
    `operation_logs` **保留**（审计不随任务消失）。
  两者文案必须在界面上明确区分 —— 把硬删写成软删的文案是误导。

🔒 不重算任何指标。看板上的 MAPE 取自 `metrics.mape`（逐任务落库值），
   `avg_mape_recent` 只是**趋势粗略值**，不是验收口径（见 `metric_dao` 注释）。
=============================================================================
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.core import security
from app.core.config import settings
from app.core.deps import GUEST_USER_ID
from app.core.errors import BizError, ErrorCode
from app.dao import (
    dataset_dao,
    event_dao,
    metric_dao,
    model_dao,
    oplog_dao,
    prediction_dao,
    task_dao,
    user_dao,
)
from app.models.event_names import ALL_EVENTS
from app.models.user import ROLE_ADMIN, ROLE_USER, User
from app.models.task import STATUS_FAILED, STATUS_SUCCEEDED

logger = logging.getLogger(__name__)

__all__ = [
    "overview",
    "list_users",
    "update_user",
    "reset_password",
    "list_tasks",
    "force_delete_task",
    "list_models",
    "switch_model",
    "list_logs",
    "ACTION_ADMIN",
]

# 🔒 管理端写操作统一用这一个 action 值（`QD-02`：`action` 扩 8 类而非 +4 值），
#    子动作（enable/disable/role_change/reset_pwd/hard_delete/switch_model）
#    写进 `detail`，这样按「谁动过什么」筛日志时一个 WHERE 就够。
ACTION_ADMIN = "admin"

# 🔒 看板与列表的默认跨度 / 页长上限（防拖库）
DEFAULT_DAYS = 30
_MAX_DAYS = 180
_MAX_PAGE_SIZE = 100


def _clamp_days(days: int | None) -> int:
    """`?days=` 收窄到 `[1, 180]`，非法值按 `BAD_PARAM` 拒而不是静默兜底。

    📌 静默兜底（`days=99999` 悄悄变30）会让运营以为在看半年的数据，
       实际只有一个月 —— 与本项目「宁可显式报错」的纪律冲突。
    """
    if days is None:
        return DEFAULT_DAYS
    try:
        value = int(days)
    except (TypeError, ValueError):
        raise BizError(
            ErrorCode.BAD_PARAM,
            f"days 必须是 1 ~ {_MAX_DAYS} 的整数，收到 {days!r}",
            {"days": days},
        ) from None
    if not (1 <= value <= _MAX_DAYS):
        raise BizError(
            ErrorCode.BAD_PARAM,
            f"days 只能是 1 ~ {_MAX_DAYS} 之间的整数，收到 {value}",
            {"days": value},
        )
    return value


def _clamp_size(size: int | None, *, default: int = 20) -> int:
    """页长收窄到 `[1, 100]`。管理端列表同样要防拖库。"""
    value = default if size is None else int(size)
    return max(1, min(_MAX_PAGE_SIZE, value))


# =============================================================================
# 审计（`WS-8-05` 全操作审计）
# =============================================================================
def _audit(
    db: Session,
    *,
    sub: str,
    operator_id: int,
    target: str | int | None = None,
    extra: str | None = None,
    result: str = "success",
) -> None:
    """写一条 `action='admin'` 的操作日志。

    🔒 `detail` 格式固定为 `sub=<子动作>;target=<目标>;by=<操作者>[;extra]`
       —— 固定格式才能被 `API-30` 的「展开 detail 显示子动作」稳定解析，
       不要自由发挥写中文句子（那会让日志查询无法按子动作过滤）。

    ⚠️ 与 `get_db` 的事务关系（`BE-17`）：本函数只 `flush` 不 `commit`，
       由请求结束时的 `get_db` 统一提交 —— 审计与业务变更**同一事务**，
       不会出现「业务成功但审计丢了」或反之。
    """
    detail = f"sub={sub};target={target};by={operator_id}"
    if extra:
        detail = f"{detail};{extra}"
    oplog_dao.record(
        db,
        action=ACTION_ADMIN,
        result=result,
        user_id=operator_id,
        target_id=target,
        detail=detail,
    )


# =============================================================================
# `API-22` 运营看板概览
# =============================================================================
def overview(db: Session, *, days: int | None = DEFAULT_DAYS) -> dict[str, Any]:
    """看板概览（`AD-P01` 顶部）。

    ⚠️ **成功率的分母不含未完成任务**：`succeeded / (succeeded + failed)`。
       把 `created`/`running` 算进分母会让「刚上传还没点预测」变成失败，
       运营看到成功率下跌会去查模型，实际是自己的数据没跑完。
    """
    span = _clamp_days(days)

    recent = task_dao.list_active_versions(db, limit=20)
    # 🔒 MAPE 走势**只查一次** metrics（`list_by_task_ids` 一条 `IN (...)`）。
    #    看板要画最近 20 个任务，逐个 `get_by_task` 就是 20 次查询 —— N+1。
    mape_points = _mape_trend(db, recent)

    event_counts = event_dao_counts(db, span)

    return {
        "user_count": user_dao.count_all(db),
        "admin_count": _count_by_role(db, ROLE_ADMIN),
        "dataset_count": dataset_dao.count_all(db),
        "task_count": task_dao.count_all(db),
        "success_rate": task_dao.success_rate(db),
        "avg_duration_ms": _avg_duration_ms(db),
        "avg_mape": metric_dao.avg_mape_recent(db, days=span),
        "mape_trend": mape_points,
        "active_model": _active_model_brief(db),
        "event_counts": event_counts,
        "days": span,
    }


def _mape_trend(db: Session, tasks: list[Any]) -> list[dict[str, Any]]:
    """一次查完这批任务的 metrics，拼成看板折线点（时间正序）。

    🔒 与 `list_by_task_ids` 配套：**一次查询**，不做 N+1。
    ⚠️ `list_by_task_ids` 的返回顺序不保证与入参一致（SQL 无 ORDER BY 承诺），
       所以先按 `task_id` 建字典，再按传入顺序输出 —— 折线的 x 轴必须是
       时间正序，否则运营看到的是一条乱序折线。
    """
    if not tasks:
        return []
    # 传入是「新 → 旧」（list_active_versions 按 created_at DESC），
    # 折线要「旧 → 新」，故这里 reversed 后再输出。
    ordered = list(reversed(tasks))
    by_task = {row.task_id: row for row in metric_dao.list_by_task_ids(
        db, [t.id for t in ordered]
    )}

    points: list[dict[str, Any]] = []
    for task in ordered:
        metric = by_task.get(task.id)
        points.append(
            {
                "task_id": task.id,
                "task_name": task.task_name or f"任务 #{task.id}",
                "created_at": task.created_at,
                "weighted_mape": None if metric is None else metric.mape,
                "status": task.status,
            }
        )
    return points


def event_dao_counts(db: Session, days: int) -> dict[str, int]:
    """埋点事件计数（`AD-P01` 埋点概览）。

    🔴 **必须补齐 16 个事件的全集，缺的补 0**（2026-10-27 修复 `FR-ADM-01`）：
    `count_by_name()` 只返回**有数据**的事件。若直接透传，看板上会缺行 ——
    而「缺行」和「该事件为 0」在视觉上是两件事：验收方问「16 个事件都接了吗」，
    看到 5 行会以为只接了 5 个，实际是另外 11 个还没被触发过。
    补齐 0 之后，这份字典本身就是「16 个事件都接好了」的证据。
    """
    counts = event_dao.count_by_name(db, since=_since(days))
    return {name: int(counts.get(name, 0)) for name in ALL_EVENTS}


def _since(days: int) -> str:
    """N 天前的本地时间字符串（`created_at` 存的是 `'YYYY-MM-DD HH:MM:SS'`）。"""
    from datetime import datetime, timedelta

    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _count_by_role(db: Session, role: str) -> int:
    return user_dao.count(db, role=role)


def _avg_duration_ms(db: Session) -> float | None:
    """已结束任务的平均端到端耗时（毫秒）。"""
    from sqlalchemy import func, select

    from app.models.task import Task

    stmt = select(func.avg(Task.duration_ms)).where(
        Task.status.in_((STATUS_SUCCEEDED, STATUS_FAILED)),
        Task.duration_ms.is_not(None),
    )
    val = db.execute(stmt).scalar_one_or_none()
    return round(float(val), 1) if val is not None else None


def _active_model_brief(db: Session) -> dict[str, Any] | None:
    """当前启用模型的摘要（`AD-P01` 模型版本卡片）。"""
    reg = model_dao.get_active(db)
    if reg is None:
        return None
    return {
        "version": reg.version,
        "window_size": reg.window_size,
        "hidden_size": reg.hidden_size,
        "num_layers": reg.num_layers,
        "param_count": reg.param_count,
        "val_mape": reg.val_mape,
        "trained_at": reg.trained_at,
        "device": reg.device,
        "is_active": bool(reg.is_active),
        # 🔒 看板必须显示这个：`.env` 与库不一致时运营才看得出「要重启才生效」
        "env_match": reg.version == settings.active_model,
    }


# =============================================================================
# `API-23` 用户列表
# =============================================================================
def list_users(
    db: Session,
    *,
    page: int = 1,
    size: int = 20,
    keyword: str | None = None,
    status: int | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    """用户列表。

    🔒 **绝不返回 `password_hash`**（`NFR-SEC-05` / `WS-8-12` 最小暴露）。
       这不是「记得别写」，而是由 `_user_brief` 显式**白名单挑字段** ——
       改成 `User.__dict__` 或 pydantic `from_attributes` 自动展开就会漏。
    """
    if role is not None and role not in (ROLE_USER, ROLE_ADMIN):
        raise BizError(
            ErrorCode.BAD_PARAM,
            f"role 只能是 user / admin，收到 {role!r}",
            {"role": role},
        )
    if status is not None and status not in (0, 1):
        raise BizError(
            ErrorCode.BAD_PARAM,
            f"status 只能是 0（停用）或 1（启用），收到 {status!r}",
            {"status": status},
        )

    page_size = _clamp_size(size)
    rows = user_dao.list_paged(
        db, page=page, size=page_size, keyword=keyword, status=status, role=role
    )
    total = user_dao.count(db, keyword=keyword, status=status, role=role)
    return {
        "items": [_user_brief(row) for row in rows],
        "total": total,
        "page": page,
        "size": page_size,
    }


def _user_brief(user: User) -> dict[str, Any]:
    """用户摘要 —— **白名单字段**，没有密码哈希。"""
    return {
        "id": user.id,
        "email": user.email,
        "nickname": user.nickname,
        "status": int(user.status),
        "role": user.role,
        "last_login_at": user.last_login_at,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
    }


# =============================================================================
# `API-24` 启停用 + 角色变更
# =============================================================================
def update_user(
    db: Session,
    *,
    target_user_id: int,
    operator: User,
    status: int | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    """启停用 / 改角色。

    🔒 **三条自我保护**（缺一条就会出现「管理员把自己锁在门外」）：
      1. 不能停用自己 —— 否则自己的 Token 立刻失效且没人能恢复；
      2. 不能降级自己的角色 —— 同上，`API-24` 就再也调不动了；
      3. 不能停用 / 降级**最后一个**管理员 —— 哪怕是从别人账号操作。
    """
    if status is None and role is None:
        raise BizError(
            ErrorCode.BAD_PARAM,
            "请至少指定一项要修改的内容（status 或 role）",
            {},
        )
    if status is not None and status not in (0, 1):
        raise BizError(
            ErrorCode.BAD_PARAM,
            f"status 只能是 0（停用）或 1（启用），收到 {status!r}",
            {"status": status},
        )
    if role is not None and role not in (ROLE_USER, ROLE_ADMIN):
        raise BizError(
            ErrorCode.BAD_PARAM,
            f"role 只能是 user / admin，收到 {role!r}",
            {"role": role},
        )

    target = user_dao.get_by_id(db, target_user_id)
    if target is None or target.id == GUEST_USER_ID:
        raise BizError(
            ErrorCode.BAD_PARAM,
            f"用户 {target_user_id} 不存在",
            {"user_id": target_user_id},
        )

    self_id = int(operator.id)
    is_self = target.id == self_id

    if is_self and status == 0:
        raise BizError(
            ErrorCode.BAD_PARAM,
            "不能停用自己的账号，否则将立即失去管理端访问能力",
            {"user_id": target_user_id},
        )
    if is_self and role == ROLE_USER:
        raise BizError(
            ErrorCode.BAD_PARAM,
            "不能降级自己的角色，否则将立即失去管理端访问能力",
            {"user_id": target_user_id},
        )

    # 「最后一个管理员」判定：目标当前是 admin 且操作会让他不再是 admin
    losing_admin = (status == 0 and target.role == ROLE_ADMIN) or (
        role == ROLE_USER and target.role == ROLE_ADMIN
    )
    if losing_admin and _count_admins(db) <= 1:
        raise BizError(
            ErrorCode.BAD_PARAM,
            "系统至少需要保留一个启用状态的管理员账号，无法停用或降级最后一名管理员",
            {"user_id": target_user_id},
        )

    changed: list[str] = []
    if status is not None and int(target.status) != status:
        user_dao.update_status(db, target.id, status)
        changed.append("disable" if status == 0 else "enable")
    if role is not None and target.role != role:
        user_dao.update_role(db, target.id, role)
        changed.append("role_change")

    if changed:
        # ⚠️ `status=0` 必须 `token_version + 1`：否则该用户的旧 Token
        #    仍能通过 `get_auth_context` 的校验 —— 停用只挡住了新登录。
        if status == 0:
            user_dao.bump_token_version(db, target.id)
        _audit(
            db,
            sub=changed[0],
            operator_id=self_id,
            target=target.id,
            extra=f"from={target.status},{target.role};to={status},{role}",
        )
        db.flush()
        db.refresh(target)

    return _user_brief(target)


def _count_admins(db: Session) -> int:
    """当前管理员数量（`API-24` 的「最后一个管理员」保护判定）。

    ⚠️ 这里数的是 `role='admin'` 的**全部**账号（含已停用的）。
       判定意图是「不能把最后一个能登录的管理员弄没了」——
       若已停用的管理员不算，那停用之后再降级就会把系统锁死。
    """
    return user_dao.count(db, role=ROLE_ADMIN)


# =============================================================================
# `API-25` 重置密码
# =============================================================================
def reset_password(
    db: Session, *, target_user_id: int, operator: User, new_password: str
) -> dict[str, Any]:
    """重置用户密码。

    🔒 **必须同时 `token_version + 1`**（`BE-41` / 数据库设计文档 §10.5）：
       JWT 是无状态的，「改密码」本身不影响已签发的 Token。不 bump 的话，
       攻击者手上的旧 Token 在密码重置后仍可用到自然过期 ——
       **重置形同虚设**。这是 `EP-2` 改密逻辑已验证过的联动。

    🔒 新密码的强度校验复用与注册**同一套**规则（≥8 位、含字母与数字、
       ≤72 字节）—— 由 schema 层 `ResetPasswordRequest` 的 validator 兜住，
       服务层再 `hash_password` 一次（防止绕过 schema 的内部调用，
       与 `auth_service.register` 的做法一致）。
    """
    if not new_password or len(new_password) < 8:
        raise BizError(
            ErrorCode.WEAK_PASSWORD,
            "新密码需不少于 8 位，且同时包含字母与数字",
            {},
        )
    has_alpha = any(c.isalpha() for c in new_password)
    has_digit = any(c.isdigit() for c in new_password)
    if not (has_alpha and has_digit):
        raise BizError(
            ErrorCode.WEAK_PASSWORD,
            "新密码需不少于 8 位，且同时包含字母与数字",
            {},
        )

    target = user_dao.get_by_id(db, target_user_id)
    if target is None or target.id == GUEST_USER_ID:
        raise BizError(
            ErrorCode.BAD_PARAM,
            f"用户 {target_user_id} 不存在",
            {"user_id": target_user_id},
        )

    user_dao.set_password(db, target.id, security.hash_password(new_password))
    user_dao.bump_token_version(db, target.id)
    # 重置后顺手解锁：否则用户拿着新密码仍被 lockout 挡住，且管理员无从得知
    if int(target.status) == 1:
        user_dao.reset_fail_count(db, target.id)

    _audit(
        db,
        sub="reset_pwd",
        operator_id=int(operator.id),
        target=target.id,
        extra=f"token_version={int(target.token_version) + 1}",
    )
    db.flush()

    return {
        "user_id": target.id,
        "email": target.email,
        "token_version": int(target.token_version) + 1,
        "message": "密码已重置，该用户全部旧登录状态已失效",
    }


# =============================================================================
# `API-26` 全量任务列表
# =============================================================================
def list_tasks(
    db: Session,
    *,
    page: int = 1,
    size: int = 20,
    user_id: int | None = None,
    status: str | None = None,
    keyword: str | None = None,
) -> dict[str, Any]:
    """全量任务列表（跨用户）。

    ⚠️ **含已软删除的任务**（`is_deleted=1`）：管理端是运维视角，
       需要看清「用户删过什么」。用户侧列表过滤掉，这里不过滤 ——
       界面上用 `is_deleted` 列标灰区分。
    """
    if status is not None and status not in (
        "created",
        "running",
        "succeeded",
        "failed",
    ):
        raise BizError(
            ErrorCode.BAD_PARAM,
            f"status 只能是 created / running / succeeded / failed，收到 {status!r}",
            {"status": status},
        )

    page_size = _clamp_size(size)
    rows = task_dao.list_all(
        db, page=page, size=page_size, user_id=user_id, status=status, keyword=keyword
    )
    total = task_dao.count_all(
        db, user_id=user_id, status=status, keyword=keyword
    )
    return {
        "items": _task_briefs(db, rows),
        "total": total,
        "page": page,
        "size": page_size,
    }


def _task_briefs(db: Session, tasks: list[Any]) -> list[dict[str, Any]]:
    """批量拼任务摘要 —— **一次查用户 + 一次查 metrics**，不做 N+1。

    🔒 与看板 `_mape_trend` 同源的纪律：列表页每行都要显示「归属用户」
       和「加权 MAPE」，逐行查就是 2N 次查询（20 条 = 40 次）。
    """
    if not tasks:
        return []

    owner_ids = {t.user_id for t in tasks if t.user_id}
    owners: dict[int, User] = {}
    if owner_ids:
        owners = {u.id: u for u in user_dao.get_by_ids(db, sorted(owner_ids))}

    metrics = {
        row.task_id: row
        for row in metric_dao.list_by_task_ids(db, [t.id for t in tasks])
    }

    briefs: list[dict[str, Any]] = []
    for task in tasks:
        owner = owners.get(task.user_id) if task.user_id else None
        metric = metrics.get(task.id)
        briefs.append(
            {
                "id": task.id,
                "task_name": task.task_name or f"任务 #{task.id}",
                "user_id": task.user_id,
                "owner_email": (
                    owner.email
                    if owner is not None
                    else ("游客" if task.user_id == GUEST_USER_ID else "用户已删除")
                ),
                "status": task.status,
                "is_deleted": int(task.is_deleted or 0),
                "granularity": task.granularity,
                "horizon": task.horizon,
                "model_version": task.model_version,
                "weighted_mape": None if metric is None else metric.mape,
                "duration_ms": task.duration_ms,
                "created_at": task.created_at,
            }
        )
    return briefs


# =============================================================================
# `API-27` 强制删除任务（硬删除）
# =============================================================================
def force_delete_task(db: Session, *, task_id: int, operator: User) -> dict[str, Any]:
    """强制删除任务（**硬删除** + 级联）。

    🔒 与用户侧软删除的差异见模块 docstring。级联范围严格按
       数据库设计文档 §5.5.2：`tasks` / `predictions` / `metrics` 删，
       `datasets` **保留**（`RESTRICT`，可能被其他任务引用），
       `operation_logs` **保留**（审计不随任务消失）。

    ⚠️ 删之前先 `count` 出要连带删的行数放进返回体与审计 detail ——
       运营需要知道「这一下删掉了多少明细」，不可逆操作没有回执很危险。
    """
    task = task_dao.get_by_id(db, task_id)
    if task is None:
        raise BizError(
            ErrorCode.BAD_PARAM,
            f"任务 {task_id} 不存在或已被删除",
            {"task_id": task_id},
        )

    pred_count = prediction_dao.count_by_task(db, task_id)
    metric_count = metric_dao.count_by_task(db, task_id)

    # 外键是 ON DELETE CASCADE，正常只删 tasks 一行即可；
    # 但 SQLite 默认不开 `PRAGMA foreign_keys`（`core/db.py` 里按连接事件开），
    # 一旦某个连接没开就会留下孤儿行 —— 显式先删子表，**不依赖外键生效**。
    prediction_dao.delete_by_task(db, task_id)
    metric_dao.delete_by_task(db, task_id)
    task_dao.hard_delete(db, task_id)

    _audit(
        db,
        sub="hard_delete",
        operator_id=int(operator.id),
        target=task_id,
        extra=f"predictions={pred_count};metrics={metric_count};owner={task.user_id}",
    )
    db.flush()

    return {
        "task_id": task_id,
        "deleted_predictions": pred_count,
        "deleted_metrics": metric_count,
        "message": f"已强制删除任务 {task_id} 及其 {pred_count} 条预测明细、"
        f"{metric_count} 条指标（不可恢复）",
    }


# =============================================================================
# `API-28` 模型版本列表
# =============================================================================
def list_models(db: Session) -> dict[str, Any]:
    """模型版本列表（`AD-P01` 模型版本表）。"""
    regs = model_dao.list_all(db)
    return {
        "items": [
            {
                "id": reg.id,
                "version": reg.version,
                "window_size": reg.window_size,
                "hidden_size": reg.hidden_size,
                "num_layers": reg.num_layers,
                "dropout": reg.dropout,
                "learning_rate": reg.learning_rate,
                "batch_size": reg.batch_size,
                "param_count": reg.param_count,
                "val_mape": reg.val_mape,
                "val_mae": reg.val_mae,
                "val_rmse": reg.val_rmse,
                "device": reg.device,
                "peak_memory_mb": reg.peak_memory_mb,
                "train_data_range": reg.train_data_range,
                "trained_at": reg.trained_at,
                "created_at": reg.created_at,
                "is_active": bool(reg.is_active),
                # 🔒 每行都给 env_match：切换后运营要一眼看出「库改了、
                #    .env 没改，需要重启」，否则 `BE-06` 静默故障很难发现
                "env_match": reg.version == settings.active_model,
            }
            for reg in regs
        ],
        "active_version": settings.active_model,
        "total": len(regs),
    }


# =============================================================================
# `API-29` 切换启用版本
# =============================================================================
def switch_model(db: Session, *, version: str, operator: User) -> dict[str, Any]:
    """切换启用模型版本。

    🔒 `BE-ADM-02`：**两个 UPDATE 必须在同一事务内，且顺序固定（先降旧、再升新）**。
       `model_registry` 上有部分唯一索引
       `ux_model_active ON model_registry (is_active) WHERE is_active = 1`，
       保证「全局有且仅有一个启用版本」。
       ⏱️ 实测：把两个 `is_active` 都置 1 会被
       `UNIQUE constraint failed: model_registry.is_active` 拒绝。
       **顺序颠倒（先升新再降旧）同样会撞索引** —— 所以这不是「顺序偏好」，
       是数据库强制的。

    🔒 `BE-ADM-03`：切换后必须校验与 `.env` 的 `ACTIVE_MODEL` 一致。
       不一致会导致「数据库说启用了 v2、进程实际加载 v1」的静默故障：
       后续预测全部走老模型，而看板上显示的是新模型。
       本实现：提交后立即 `reload_active_model()` 让进程内模型与库同步；
       若 `.env` 未同步则**仍返回成功但带 `warning`** —— 因为把 `.env`
       写进数据库不是接口该做的事，而悄悄失败更糟。
    """
    reg = model_dao.get_by_version(db, version)
    if reg is None:
        raise BizError(
            ErrorCode.BAD_PARAM,
            f"模型版本 {version!r} 不存在，请先训练并注册",
            {"version": version},
        )

    current = model_dao.get_active(db)
    if current is not None and current.version == version:
        # 幂等：已是启用状态直接成功，不做无意义的两次 UPDATE
        return {
            "version": version,
            "is_active": True,
            "changed": False,
            "env_match": True,
            "message": f"模型 {version} 已是当前启用版本",
        }

    previous = current.version if current is not None else None

    # 🔒 先降后升，同一事务（由 get_db 在请求结束时统一提交）
    model_dao.deactivate_all(db)
    model_dao.activate_version(db, version)
    db.flush()

    # 提交前把新版本再查一次，确认唯一索引没有拦下别的并发切换
    now_active = model_dao.get_active(db)
    if now_active is None or now_active.version != version:
        raise BizError(
            ErrorCode.DB_ERROR,
            f"模型切换失败：期望启用 {version}，实际为 "
            f"{now_active.version if now_active else '无'}",
            {"version": version},
        )

    _audit(
        db,
        sub="switch_model",
        operator_id=int(operator.id),
        target=version,
        extra=f"from={previous};to={version}",
    )
    db.flush()

    # 🔒 `BE-ADM-03`：让进程内模型与库同步（热切换，不需重启）。
    #    失败不抛错 —— 数据已切换成功，把「重载失败」当成功返回会掩盖问题，
    #    但把已完成的切换回滚也不对。改为返回 warning 让界面显示。
    warnings: list[str] = []
    reloaded = False
    try:
        from app.core import model_holder

        model_holder.reload_active_model(db)
        reloaded = True
    except Exception as exc:  # pragma: no cover - 重载失败路径
        logger.warning("model reload failed after switch: %s", exc)
        warnings.append(
            f"模型已在数据库中切换，但进程内重载失败（{exc}）；"
            "请检查模型文件与显存后重启服务"
        )

    env_match = version == settings.active_model
    if not env_match:
        warnings.append(
            f".env 的 ACTIVE_MODEL={settings.active_model!r} 与新启用版本 "
            f"{version!r} 不一致，重启服务后会回退到前者，请同步修改 .env"
        )

    return {
        "version": version,
        "is_active": True,
        "changed": True,
        "previous_version": previous,
        "env_match": env_match,
        "reloaded": reloaded,
        "warnings": warnings,
        "message": f"已切换到模型 {version}"
        + (f"（原 {previous}）" if previous else ""),
    }


# =============================================================================
# `API-30` 操作日志查询
# =============================================================================
def list_logs(
    db: Session,
    *,
    page: int = 1,
    size: int = 20,
    action: str | None = None,
    result: str | None = None,
    user_id: int | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """操作日志查询（`AD-P02` 日志 Tab）。

    ⚠️ **刻意不在 schema 层收窄 `action` / `result` 取值域**：
       收窄会抛 pydantic `ValidationError` → 500，而非法筛选值应按本项目
       错误契约返 `40012`（与 `EP-7` 的 `HistoryQuery.status` 同理）。
       这里用白名单校验，给出「有哪些可选值」的提示。
    """
    if action is not None and action not in _LOG_ACTIONS:
        raise BizError(
            ErrorCode.BAD_PARAM,
            f"action 只能是 {' / '.join(sorted(_LOG_ACTIONS))}，收到 {action!r}",
            {"action": action},
        )
    if result is not None and result not in ("success", "fail"):
        raise BizError(
            ErrorCode.BAD_PARAM,
            f"result 只能是 success / fail，收到 {result!r}",
            {"result": result},
        )

    page_size = _clamp_size(size)
    rows = oplog_dao.list_paged(
        db,
        page=page,
        size=page_size,
        action=action,
        result=result,
        user_id=user_id,
        start=start,
        end=end,
    )
    total = oplog_dao.count(
        db, action=action, result=result, user_id=user_id, start=start, end=end
    )
    return {
        "items": [_log_brief(row) for row in rows],
        "total": total,
        "page": page,
        "size": page_size,
    }


# 与 `operation_logs` 的 CHECK 约束同源（8 类）
_LOG_ACTIONS = frozenset(
    {"login", "register", "logout", "upload", "predict", "export", "delete", "admin"}
)


def _log_brief(row: Any) -> dict[str, Any]:
    """日志条目 —— 把 `detail` 的 `k=v;k=v` 拆成 `sub_action` 等结构化字段。

    🔒 拆而不是原样丢给前端：`AD-P02` 要求「`action='admin'` 的 detail
       展开显示子动作」，前端要按子动作渲染徽标与筛选，不该自己 split。
    """
    detail = row.detail or ""
    parsed: dict[str, str] = {}
    for part in detail.split(";"):
        if "=" in part:
            key, _, value = part.partition("=")
            parsed[key.strip()] = value.strip()

    return {
        "id": row.id,
        "user_id": row.user_id,
        "action": row.action,
        "target_id": row.target_id,
        "result": row.result,
        "detail": detail,
        "sub_action": parsed.get("sub"),
        "operator_id": int(parsed["by"]) if parsed.get("by", "").isdigit() else None,
        "ip": row.ip,
        "created_at": row.created_at,
    }


# 显式引用，避免 linter 报未使用
_ = (ROLE_USER, ACTION_ADMIN)
