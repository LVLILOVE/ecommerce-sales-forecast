"""
=============================================================================
认证业务逻辑（注册 / 登录 / 登出 / 资料与密码修改）
-----------------------------------------------------------------------------
版本     : V1.0（EP-2）
依据文档 : 《开发技术文档 V1.0》§4.4（认证与安全）、§5.4（API-01~05）
           《PRD V1.0》FR-USR-01 ~ 07、FR-SYS-03（操作日志）
           《项目开发实施方案 V1.0》WS-2-03、WS-2-05
-----------------------------------------------------------------------------
职责边界（`BE-04`）：本模块只做业务编排，不做 HTTP 序列化，不 import fastapi。
    路由层（`api/v1/auth.py`）负责取参数与组织响应；DAO 负责存取。
    所有「能不能做」的判断都在这里，路由层不得重复实现。
-----------------------------------------------------------------------------
三个必须保持的实现要点
-----------------------------------------------------------------------------
1. 🔒 **注册查重靠唯一索引，不靠「先查后插」**（`API-01-1`）
   「先 SELECT 判断存在、再 INSERT」在并发下会竞态：两个请求同时查到不存在，
   然后都去插入，其中一个撞唯一索引报 500。正确做法是**直接 INSERT 并捕获
   `IntegrityError`**，映射为 `40105`。本项目的唯一索引是
   `ux_users_email ON (email COLLATE NOCASE)`，因此大小写不同的同一邮箱
   也会被索引拦住——这正是 `FR-USR-01`「全局唯一，大小写不敏感」的落地。

2. 🔒 **限流计数落库，不用内存**（`WS-2-05`）
   进程重启后内存计数会丢，攻击者只要等待重启即可绕过。故
   `fail_count` / `locked_until` 存在 `users` 表。
   ⚠️ 例外：**不存在的邮箱**没有行可写。若对它不做限流，攻击者就能用
   「第 4 次是否返回 40104」区分「邮箱已注册」与「邮箱不存在」，
   从而绕过 `API-02-1` 的防枚举约定。故此处额外维护一份
   **仅针对未知邮箱**的内存计数（`_UNKNOWN_FAIL`），使两种情形的响应序列
   完全一致（前 3 次 40103、第 4 次起 40104）。这是对 `API-02-1` 的补强，
   不改变任何对外契约。

3. 🔒 **改密必须同时 +1 `token_version`**（`FR-USR-07` / `API-05-1`）
   JWT 无状态，无法枚举某用户全部在途 Token，所以「改密即全端下线」只能靠
   版本号：`token_version + 1` 后，所有旧 Token 的 `ver` 与库不符，
   在 `get_auth_context` 处统一被拒（40102）。**漏掉这一步 = 改密形同虚设。**
=============================================================================
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import config, security
from app.core.errors import BizError, ErrorCode
from app.dao import oplog_dao, token_dao, user_dao
from app.models.operation_log import RESULT_FAIL, RESULT_SUCCESS
from app.models.user import User

logger = logging.getLogger(__name__)

__all__ = [
    "DATETIME_FMT",
    "normalize_email",
    "default_nickname",
    "register",
    "login",
    "logout",
    "update_profile",
]

# 与 DDL 的 `datetime('now','localtime')` 产出的格式一致（NFR-COMP-04：全本地时区）
DATETIME_FMT = "%Y-%m-%d %H:%M:%S"

# 时序均衡用的哑哈希：cost 与真实一致，仅用于「用户不存在」分支的假比对
# （避免用「响应快 = 邮箱不存在」做时序枚举）。取自 seed.sql 的管理员种子串。
_DUMMY_HASH = "$2b$12$gHPiHYW9yCdlRjs8Ux2eme.EdryDZ44V9HbgR3.49dPAohfUmFLzS"

# ---------------------------------------------------------------------------
# 未知邮箱的限流镜像（见模块 docstring 第 2 条）
# ---------------------------------------------------------------------------
_UNKNOWN_LOCK = threading.Lock()
_UNKNOWN_FAIL: dict[str, tuple[int, datetime | None]] = {}
"""`email -> (fail_count, locked_until)`。

⚠️ `locked_until = None` 表示「在计数、尚未锁定」，与 `datetime` 值语义不同。
   必须用 `None` 而不是 `datetime.min` 之类的哨兵时间：
   否则「判断是否已解锁」的 `remain <= 0` 会同时命中「根本没锁」的状态，
   进而误删正在累积的计数 —— 结果是**未知邮箱的限流永远不生效**
   （EP-2 实测踩到，verify_auth 的 T-A19 就是守着这条）。
   仅在邮箱不存在时使用，命中次数极少、条目数上界为「被爆破的未知邮箱数」。
"""


def normalize_email(email: str) -> str:
    """邮箱归一化：去首尾空白 + 转小写。

    🔒 写入与查询必须都经过本函数，否则 `Alice@X.com` 与 `alice@x.com`
       会在**应用层**被当成两个账号——虽然 `ux_users_email` 的 COLLATE NOCASE
       能拦住重复插入，但「查到不存在的错觉」会先发生，报错信息也会变得难懂。
    """
    return email.strip().lower()


def default_nickname(email: str) -> str:
    """昵称为空时取邮箱 `@` 前缀（`API-01` 请求体说明）。"""
    local = email.split("@", 1)[0]
    return local[:32] or "用户"


def _now() -> datetime:
    return datetime.now()


def _fmt(dt: datetime) -> str:
    return dt.strftime(DATETIME_FMT)


def _parse(text: str) -> datetime:
    try:
        return datetime.strptime(text, DATETIME_FMT)
    except ValueError:
        # 列被手工写坏时不让整个登录流程 500：按「未锁定」处理并留日志
        logger.warning("locked_until 格式异常，按未锁定处理：%r", text)
        return datetime.min


def _record(
    db: Session,
    *,
    action: str,
    result: str = RESULT_SUCCESS,
    user_id: int | None = None,
    target_id: str | int | None = None,
    detail: str | None = None,
    ip: str | None = None,
) -> None:
    """写操作日志（`FR-SYS-03`）。

    ⚠️ 审计写入**不得**影响主流程：登录日志写失败不应该让登录失败。
       故整段包 try/except，且不 re-raise。
    """
    try:
        oplog_dao.record(
            db,
            action=action,
            result=result,
            user_id=user_id,
            target_id=target_id,
            detail=detail,
            ip=ip,
        )
    except Exception:  # noqa: BLE001 - 审计失败不影响业务
        logger.exception("操作日志写入失败 action=%s", action)


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------


def register(
    db: Session,
    *,
    email: str,
    password: str,
    nickname: str | None = None,
    ip: str | None = None,
) -> User:
    """注册（`API-01` / `FR-USR-01`~03）。

    密码强度与 72 字节上限已由 `RegisterRequest` 校验；此处再经
    `security.hash_password` 兜底一次（防止绕过 schema 的内部调用）。

    :raises BizError: `40105` 邮箱已注册 / `40104` 注册过于频繁（`R-07`）
    """
    # 🔒 限流必须在 hash_password **之前**：bcrypt 是注册链路里最贵的一步，
    #    放在后面等于「限流自己也要付一次哈希成本」，被刷时形同虚设。
    _assert_register_allowed(db, ip=ip)

    email = normalize_email(email)
    password_hash = security.hash_password(password)
    nickname = (nickname or "").strip() or default_nickname(email)

    try:
        user = user_dao.create(db, email=email, password_hash=password_hash, nickname=nickname)
        db.flush()
    except IntegrityError:
        # 🔒 API-01-1：唯一索引冲突 → 40105。必须先 rollback 清掉失败事务，
        #    否则同一会话上的后续操作（含下面的审计写入）会 PendingRollbackError。
        db.rollback()
        logger.info("注册被拒：邮箱已存在 email=%s", email)
        raise BizError(
            ErrorCode.EMAIL_TAKEN,
            "该邮箱已注册，请直接登录或使用其他邮箱",
            {"field": "email"},
        ) from None

    _record(db, action="register", user_id=user.id, target_id=user.id, ip=ip)
    logger.info("注册成功 user_id=%s email=%s", user.id, email)
    return user


# ---------------------------------------------------------------------------
# 注册限流（`R-07`，2026-10-26）
# ---------------------------------------------------------------------------
_REG_ATTEMPT_LOCK = threading.Lock()
_REG_ATTEMPTS: dict[str, tuple[int, datetime]] = {}
"""`ip -> (窗口内已尝试次数, 窗口起始时刻)`，**固定窗口**计数。

🔒 **为什么是两层，而不是只做一层**（这是本节最容易被"简化"掉的地方）：

- **库层**（`operation_logs` 里 `action='register'` 的成功条数）：管「真实建出来的
  账号数」。必须抗重启，否则攻击者等一次发布就能把配额清零。
- **内存层**（本字典）：管「每一次尝试」，**含注定失败的**。因为 `register()`
  里 bcrypt 在入库之前执行，一个「邮箱已存在」的失败请求同样要付 ~250 ms
  的哈希成本 ⇒ 只限成功数的话，刷 40105 就是一个免费的 CPU DoS。

  反过来把「尝试数」也写进库是不可行的：被刷时审计表先爆，
  等于把「消耗算力」升级成「消耗磁盘 + 污染审计日志」。
  内存层重启清零可以接受 —— 它只是「降速」，真正的配额在库层。
"""


def _rate_limited(retry_after: float) -> BizError:
    """构造限流异常。

    错误码**复用 `40104`**（前端镜像里它的注释就是「限流冷却中」）：
    PRD 的错误码表是按「登录失败锁定」命名的，但它表达的状态
    （操作过于频繁 + `retry_after_seconds`）与注册限流完全一致。
    新增一个 `40106` 需要同步改 PRD / 技术文档 / 前端镜像三处，
    收益只是让枚举值更"整洁"，不值得 —— 用户看到的是 `message`，不是码。
    """
    remain = max(1, int(retry_after))
    return BizError(
        ErrorCode.LOGIN_LOCKED,
        f"操作过于频繁，请 {remain} 秒后再试",
        {"retry_after_seconds": remain},
    )


def _assert_register_allowed(db: Session, *, ip: str | None) -> None:
    """注册前置限流（`R-07`）。

    :raises BizError: `40104` 冷却中，`data.retry_after_seconds` 给出剩余秒数
    """
    window = max(1, int(config.settings.register_window_seconds))
    now = _now()

    # ---- 第 1 层：窗口内的成功注册数（库计数，抗重启） ----
    max_success = int(config.settings.register_max_success)
    if max_success > 0:
        since = _fmt(now - timedelta(seconds=window))
        done, oldest = oplog_dao.count_recent(
            db, action="register", ip=ip, since=since
        )
        if done >= max_success:
            # 用「窗口内最早那条」推算解锁时刻，才能给出准确的重试秒数
            retry = float(window)
            if oldest:
                retry = (_parse(oldest) + timedelta(seconds=window) - now).total_seconds()
            logger.warning(
                "注册被限流（成功数）ip=%s done=%s/%s", ip, done, max_success
            )
            raise _rate_limited(retry)

    # ---- 第 2 层：窗口内的尝试次数（内存固定窗口，含失败） ----
    max_attempts = int(config.settings.register_max_attempts)
    if max_attempts <= 0:
        return
    key = ip or "-"
    with _REG_ATTEMPT_LOCK:
        count, start = _REG_ATTEMPTS.get(key, (0, now))
        if (now - start).total_seconds() >= window:
            count, start = 0, now
        if count >= max_attempts:
            logger.warning("注册被限流（尝试数）ip=%s count=%s/%s", ip, count, max_attempts)
            raise _rate_limited(window - (now - start).total_seconds())
        # 条目数上界防御：被大量不同 IP 刷时不能让字典无限长
        if len(_REG_ATTEMPTS) > 4096:
            _REG_ATTEMPTS.clear()
        _REG_ATTEMPTS[key] = (count + 1, start)


# ---------------------------------------------------------------------------
# 登录（含限流）
# ---------------------------------------------------------------------------


def _lock_remaining(user: User) -> int:
    """该用户剩余冷却秒数；<=0 表示不在冷却期。"""
    if not user.locked_until:
        return 0
    remain = (_parse(user.locked_until) - _now()).total_seconds()
    return max(0, int(remain) + 1) if remain > 0 else 0


def _unknown_remaining(email: str) -> int:
    """未知邮箱的剩余冷却秒数；`0` 表示「未锁定」（**不清计数**）。"""
    with _UNKNOWN_LOCK:
        item = _UNKNOWN_FAIL.get(email)
        if not item:
            return 0
        _count, until = item
        if until is None:            # 在计数、未锁定 → 保留记录，让计数能累加到阈值
            return 0
        remain = (until - _now()).total_seconds()
        if remain > 0:
            return int(remain) + 1
        _UNKNOWN_FAIL.pop(email, None)   # 冷却已过 → 清理，重新计 3 次
        return 0


def _bump_unknown(email: str, max_fail: int, lock_seconds: int) -> int:
    """对未知邮箱记一次失败，返回**本次**失败序号。

    达到 `max_fail` 时写入冷却并把计数归零（与真实用户路径一致：
    冷却结束后重新计 3 次），但调用方拿到的仍是「这是第几次失败」的语义值。

    🔴 **条目数上界**（2026-10-26 复评修复）：本字典的键是「被爆破的未知邮箱」，
    上界就是攻击者愿意尝试的邮箱数 —— 随机邮箱持续打登录即可让它单调增长
    （每条约 100 字节），长期运行构成内存泄漏。
    对照同文件的 `_REG_ATTEMPTS` 早就有 4096 的上界防御，这里补齐同一条。
    """
    with _UNKNOWN_LOCK:
        count, _ = _UNKNOWN_FAIL.get(email, (0, None))
        count += 1
        if len(_UNKNOWN_FAIL) > 4096:
            _UNKNOWN_FAIL.clear()
        if count >= max_fail:
            _UNKNOWN_FAIL[email] = (0, _now() + timedelta(seconds=lock_seconds))
        else:
            _UNKNOWN_FAIL[email] = (count, None)
        return count


def login(
    db: Session,
    *,
    email: str,
    password: str,
    ip: str | None = None,
) -> tuple[str, int, User]:
    """登录并签发 Token（`API-02` / `FR-USR-04` / `FR-USR-06`）。

    :return: `(access_token, expires_in_seconds, user)`
    :raises BizError: `40103` 凭证错误 / `40104` 冷却中
    """
    email = normalize_email(email)
    max_fail = int(config.settings.login_max_fail)
    lock_seconds = int(config.settings.login_lock_seconds)

    user = user_dao.get_user_by_email(db, email)

    # ---------- 1. 冷却检查（先于密码比对，避免白算 bcrypt） ----------
    if user is not None:
        remain = _lock_remaining(user)
        if remain > 0:
            raise BizError(
                ErrorCode.LOGIN_LOCKED,
                f"密码连续错误次数过多，请 {remain} 秒后重试",
                {"retry_after_seconds": remain, "max_fail": max_fail},
            )
    else:
        remain = _unknown_remaining(email)
        if remain > 0:
            raise BizError(
                ErrorCode.LOGIN_LOCKED,
                f"密码连续错误次数过多，请 {remain} 秒后重试",
                {"retry_after_seconds": remain, "max_fail": max_fail},
            )

    # ---------- 2. 比对密码 ----------
    # ⚠️ 用户不存在时也走一次假比对（对哨兵哈希），使响应耗时与真实用户接近，
    #    避免用「响应快 = 邮箱不存在」做时序枚举。
    hashed = user.password_hash if user is not None else _DUMMY_HASH
    ok = security.verify_password(password, hashed)

    if user is None or not ok:
        if user is not None:
            fail_no = int(user.fail_count) + 1
            if fail_no >= max_fail:
                # 记满阈值：写冷却并归零（两步同事务）
                user_dao.set_login_lock(
                    db, user.id, _fmt(_now() + timedelta(seconds=lock_seconds))
                )
            else:
                user_dao.bump_fail_count(db, user.id, None)
            db.flush()
        else:
            fail_no = _bump_unknown(email, max_fail, lock_seconds)

        _record(db, action="login", result=RESULT_FAIL, user_id=None, detail=f"email={email}", ip=ip)

        # 🔒🔒 必须在抛错**之前**显式提交（技术文档 `BE-17` 的例外场景）：
        #   `get_db` 的契约是「异常 → rollback」。若这里直接 raise，
        #   失败计数与失败审计都会被回滚掉 —— 结果是 **限流永远不生效**：
        #   攻击者可以无限次试密码，每次 fail_count 都从 0 开始。
        #   这个缺陷在单次请求的返回值上是「看不出问题」的（仍返回 40103），
        #   只有连打 4 次才会暴露，故必须在这里钉死。
        db.commit()

        logger.info("登录失败 email=%s 第 %s 次", email, fail_no)
        raise BizError(
            ErrorCode.BAD_CREDENTIAL,
            "邮箱或密码错误，请重试",
            {
                "fail_count": fail_no,
                "max_fail": max_fail,
                "lock_seconds": lock_seconds,
                "locked": fail_no >= max_fail,
            },
        )

    # ---------- 3. 停用检查（EP-8 补） ----------
    # 🔴🔴 这段是 `EP-8` 补上的**真实缺陷修复**，此前 `login()` 全程
    #    **没有任何 `status` 判断** —— 管理端 `API-24` 把用户停用了，
    #    他照样能用正确密码登录进来拿新 Token。
    #
    #    当时的误判是「`token_version + 1` 已经挡住了」：
    #    停用时确实 bump 了 `token_version`，那只能让**已签发的旧 Token**
    #    失效（`get_auth_context` 会比对），但**新的登录请求不受约束** ——
    #    攻击者/被停用者重新 POST 一次 `/auth/login` 就绕过了。
    #    「停用」若挡不住重新登录，等于没停用。
    #
    #    ⚠️ 位置很讲究：必须在**密码比对成功之后**、签发 Token 之前判。
    #       放太早会泄露「该邮箱存在但被停用」（邮箱可枚举，`RG-07`）；
    #       放太晚则 Token 已经签发出去了。
    if int(user.status) != 1:
        # 归40103 而非 40104/40102：对外表现与「密码错误」一致，
        # 避免让探测者区分出「邮箱存在且被停用」。
        _record(
            db, action="login", result=RESULT_FAIL, user_id=None,
            detail=f"email={email}", ip=ip,
        )
        db.commit()
        logger.info("登录被拒（账号已停用）email=%s user_id=%s", email, user.id)
        raise BizError(
            ErrorCode.BAD_CREDENTIAL,
            "邮箱或密码错误，请重试",
            {"reason": "account_disabled"},
        )

    # ---------- 4. 成功 ----------
    user_dao.reset_fail_count(db, user.id)
    user_dao.update_last_login(db, user.id, _fmt(_now()))
    db.flush()
    with _UNKNOWN_LOCK:
        _UNKNOWN_FAIL.pop(email, None)

    token, expires_in = security.create_access_token(user.id, user.token_version)
    _record(db, action="login", user_id=user.id, target_id=user.id, ip=ip)
    logger.info("登录成功 user_id=%s email=%s", user.id, email)

    # 刷新 ORM 对象，使响应里的 last_login_at / fail_count 为最新值
    db.refresh(user)
    return token, expires_in, user


# ---------------------------------------------------------------------------
# 登出
# ---------------------------------------------------------------------------


def logout(db: Session, *, user_id: int, jti: str, exp: int, ip: str | None = None) -> None:
    """登出：把当前 Token 的 `jti` 加入黑名单（`API-03` / `FR-USR-05`）。

    🔒 黑名单落库（`revoked_tokens`，`DB-T09`，`R-03` 起）。此前是进程内存，
       重启后已登出的 Token 会「复活」、多 worker 下各算一套 —— 且都不报错。
       现在与登出在**同一事务**里写入：登出成功 ⇔ 黑名单生效，
       不会出现「审计写了但 Token 还能用」的错位。
    """
    token_dao.revoke(db, jti=jti, expires_at=exp, user_id=user_id)
    _record(db, action="logout", user_id=user_id, target_id=user_id, ip=ip)
    logger.info("token revoked jti=%s user_id=%s", jti, user_id)


# ---------------------------------------------------------------------------
# 修改资料 / 密码
# ---------------------------------------------------------------------------


def update_profile(
    db: Session,
    *,
    user: User,
    nickname: str | None = None,
    old_password: str | None = None,
    new_password: str | None = None,
    ip: str | None = None,
) -> User:
    """部分更新（`API-05` / `FR-USR-07`）。未传字段不变。

    :raises BizError: `40103` 原密码缺失或错误；`40011` 新密码强度/字节超限
    """
    changed: list[str] = []

    if nickname is not None:
        nickname = nickname.strip()
        if nickname and nickname != user.nickname:
            user.nickname = nickname[:32]
            changed.append("nickname")

    if new_password is not None:
        # 🔒 改密必须验原密码。缺失与错误**共用** 40103，避免攻击者用
        #    「提示不同」判断是否猜中了原密码（与登录同一条防枚举原则）。
        if not old_password:
            raise BizError(
                ErrorCode.BAD_CREDENTIAL,
                "修改密码需先输入当前密码",
                {"field": "old_password"},
            )
        if not security.verify_password(old_password, user.password_hash):
            logger.info("改密被拒：原密码错误 user_id=%s", user.id)
            raise BizError(
                ErrorCode.BAD_CREDENTIAL,
                "当前密码不正确，请重新输入",
                {"field": "old_password"},
            )

        user.password_hash = security.hash_password(new_password)
        changed.append("password")

    if not changed:
        # 无字段变化：不视为错误，原样返回（幂等的 PATCH 语义）
        return user

    # 🔒 FR-USR-07：改密后 token_version+1，使该用户**全部**旧 Token 立即失效。
    #    放在同一事务里与 password_hash 一起提交，避免「密码已改但旧 Token 仍有效」
    #    的窗口期。
    if "password" in changed:
        user_dao.bump_token_version(db, user.id)

    db.flush()
    # ⚠️ 不写 operation_logs：FR-SYS-03 要求的四类动作是
    #    login / upload / predict / export，改资料不属任何一类；
    #    而 `action` 列的 CHECK 只允许 8 个固定值，没有「更新资料」。
    #    硬塞成 'login' 会让审计语义失真，故只落应用日志。
    logger.info("资料更新 user_id=%s fields=%s", user.id, "+".join(changed))
    db.refresh(user)
    return user
