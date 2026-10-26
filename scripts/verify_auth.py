"""
=============================================================================
EP-2 认证链路端到端校验
-----------------------------------------------------------------------------
用法（在工作区根目录执行）：
    C:\\ProgramData\\anaconda3\\envs\\deploy\\python.exe scripts/verify_auth.py

覆盖 `DOD-EP2` 全部条目 + 若干「文档写了但容易漏」的边界：
  闭环    注册 → 登录 → /users/me → 改密 → 旧 Token 立即失效 → 登出
  限流    连续 3 次失败触发冷却，第 4 次被拒且提示剩余秒数
  边界    72 / 73 字节（bcrypt）、大小写邮箱、弱密码、坏 Token、无 Token
  安全    响应无 password_hash、库中无明文密码、游客哨兵不可登录
  权限    get_current_admin：普通用户 40301、管理员放行
  审计    operation_logs 落下 login / register / logout 记录

设计约定
-----------------------------------------------------------------------------
1. 🔒 **用独立临时库，不碰 `data/app.db`**
   校验过程会注册用户、反复失败触发锁定、改密等，若直接打在开发库上会污染
   演示数据。故脚本启动**最先**把 `DB_PATH` 指向 `data/tmp/verify_auth/app.db`，
   再 import 应用。config 优先读系统环境变量，因此这不依赖 .env 的改写。
2. 退出码 0 = 全通过；非 0 = 存在未通过项（可用于 CI / 提交前钩子）。
3. 失败时保留临时库目录，便于用 sqlite3 复查现场。
=============================================================================
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# ⚠️ 必须在 import 应用之前设置环境变量（config 在模块导入时即读取）
# ---------------------------------------------------------------------------
TMP_DIR = ROOT / "data" / "tmp" / "verify_auth"
if TMP_DIR.exists():
    shutil.rmtree(TMP_DIR, ignore_errors=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)

os.environ["DB_PATH"] = str(TMP_DIR / "app.db")
os.environ["JWT_SECRET"] = "verify-auth-secret-0123456789abcdefghijklmnop"   # ≥32 字符
os.environ["APP_ENV"] = "test"
os.environ["LOGIN_MAX_FAIL"] = "3"
os.environ["LOGIN_LOCK_SECONDS"] = "60"

sys.path.insert(0, str(ROOT))

try:
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient
except Exception as exc:  # noqa: BLE001
    print(f"[FAIL] 缺少依赖（fastapi / httpx）：{exc}")
    sys.exit(1)

from app.core import security                              # noqa: E402
from app.core.config import settings                       # noqa: E402
from app.core.deps import get_current_admin                # noqa: E402
from app.core.errors import register_exception_handlers    # noqa: E402
from app.main import app                                   # noqa: E402

passed = 0
failed = 0
notes: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"[ OK ] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name} {detail}")


def note(text: str) -> None:
    notes.append(text)
    print(f"[NOTE] {text}")


def body(r) -> dict:
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return {}


print("=" * 70)
print("EP-2 认证链路端到端校验")
print("=" * 70)
print(f"[INFO] 临时库：{settings.db_path}")

EMAIL = f"verify_{uuid.uuid4().hex[:8]}@example.cn"
EMAIL_UPPER = EMAIL.replace("verify", "VERIFY").replace("@example.cn", "@Example.CN")
PWD = "Demo12345"
PWD2 = "NextPass6789"

with TestClient(app) as c:
    # =============================================================== 环境
    h = c.get("/health")
    check("T-A01 /health 可达且返回统一响应包",
          h.status_code == 200 and body(h).get("code") == 0,
          f"（{h.status_code}）")
    check("T-A02 启动自检通过（DB 落到临时目录，未污染 data/app.db）",
          str(settings.db_path).replace("\\", "/").endswith("verify_auth/app.db"),
          f"（实际 {settings.db_path}）")

    # =============================================================== 注册
    r = c.post("/api/v1/auth/register",
               json={"email": EMAIL_UPPER, "password": PWD, "nickname": "校验用户"})
    d = body(r)
    check("T-A03 注册成功，返回 user_id 与归一化（小写）邮箱",
          r.status_code == 200 and d.get("code") == 0
          and isinstance(d["data"]["user_id"], int)
          and d["data"]["email"] == EMAIL,
          f"（{r.status_code} {d}）")
    uid = d.get("data", {}).get("user_id")

    r = c.post("/api/v1/auth/register", json={"email": EMAIL, "password": PWD})
    check("T-A04 同邮箱重复注册（大小写不同）被拒 → 40105",
          body(r).get("code") == 40105,
          f"（{body(r)}）")

    for pwd, label in (("12345678", "纯数字"), ("abcdefgh", "纯字母"), ("ab12", "过短")):
        r = c.post("/api/v1/auth/register", json={"email": f"w_{label}@x.cn", "password": pwd})
        check(f"T-A05 弱密码（{label}）被拒 → 40011", body(r).get("code") == 40011,
              f"（{body(r)}）")

    # 🔒 72 字节边界：bcrypt 5.x 对 >72 字节**抛错**（4.x 静默截断）
    #    用例必须是「字符数 < 64 但字节数 > 72」的中文串，否则先被 max_length 拦下，
    #    测到的是字符上限而不是字节上限（假通过）。
    b72 = "中文" * 11 + "中" + "a12"        # 72 字节 / 26 字符
    b73 = "中文" * 11 + "中" + "a123"       # 73 字节 / 27 字符
    r = c.post("/api/v1/auth/register", json={"email": f"b72_{uuid.uuid4().hex[:6]}@x.cn", "password": b72})
    check("T-A06 恰好 72 字节的密码被接受（注册成功）",
          body(r).get("code") == 0, f"（{body(r)}）")
    r = c.post("/api/v1/auth/register", json={"email": f"b73_{uuid.uuid4().hex[:6]}@x.cn", "password": b73})
    check("T-A07 73 字节的密码被拒 → 40011（不是 500）",
          r.status_code == 200 and body(r).get("code") == 40011,
          f"（HTTP {r.status_code} / {body(r)}）")

    # =============================================================== 登录
    r = c.post("/api/v1/auth/login", json={"email": EMAIL, "password": PWD})
    d = body(r)
    check("T-A08 登录成功，返回 access_token / token_type / expires_in / user",
          r.status_code == 200 and d.get("code") == 0
          and d["data"]["token_type"] == "bearer"
          and d["data"]["expires_in"] == settings.jwt_expire_minutes * 60
          and d["data"]["user"]["id"] == uid)
    check("T-A09 响应中不含 password_hash（NFR-SEC-09）",
          "password_hash" not in r.text)
    check("T-A10 user.role 为 'user'（CH-01 默认值）",
          d["data"]["user"]["role"] == "user")
    token = d["data"]["access_token"]
    H = {"Authorization": f"Bearer {token}"}

    # =============================================================== /users/me
    r = c.get("/api/v1/users/me", headers=H)
    check("T-A11 GET /users/me 成功且字段与 UserProfile 对齐",
          r.status_code == 200 and body(r)["code"] == 0
          and set(("id", "email", "nickname", "status", "role", "created_at"))
          <= set(body(r)["data"]),
          f"（{body(r)}）")

    r = c.get("/api/v1/users/me")
    check("T-A12 无 Token → HTTP 401 + code 40101（真实状态码）",
          r.status_code == 401 and body(r).get("code") == 40101,
          f"（HTTP {r.status_code} / {body(r)}）")

    r = c.get("/api/v1/users/me", headers={"Authorization": "Bearer not.a.jwt"})
    check("T-A13 坏 Token → HTTP 401 + code 40102",
          r.status_code == 401 and body(r).get("code") == 40102,
          f"（HTTP {r.status_code} / {body(r)}）")

    r = c.get("/api/v1/users/me", headers={"Authorization": "token " + token})
    check("T-A14 scheme 非 Bearer → 40101（不做静默放行）",
          r.status_code == 401 and body(r).get("code") == 40101,
          f"（{body(r)}）")

    # =============================================================== 限流
    lock_email = f"lock_{uuid.uuid4().hex[:6]}@example.cn"
    c.post("/api/v1/auth/register", json={"email": lock_email, "password": PWD})
    fails = []
    for i in range(1, 4):
        rr = c.post("/api/v1/auth/login", json={"email": lock_email, "password": "WrongPass1"})
        fails.append(body(rr))
    check("T-A15 连续 3 次密码错误均返回 40103，且 fail_count 递增 1→3",
          all(f.get("code") == 40103 for f in fails)
          and [f["data"]["fail_count"] for f in fails] == [1, 2, 3],
          f"（{[f.get('code') for f in fails]} / {[f['data'].get('fail_count') for f in fails]}）")
    check("T-A16 第 3 次失败已在 data 中标记 locked=true（前端据此切冷却态）",
          fails[2]["data"].get("locked") is True)

    r = c.post("/api/v1/auth/login", json={"email": lock_email, "password": PWD})
    d = body(r)
    check("T-A17 第 4 次（即使密码正确）被拒 → 40104，且给出剩余秒数（BE-19）",
          d.get("code") == 40104 and isinstance(d["data"].get("retry_after_seconds"), int)
          and 0 < d["data"]["retry_after_seconds"] <= settings.login_lock_seconds,
          f"（{d}）")
    check("T-A18 冷却提示文案含具体秒数（不是「请稍后再试」）",
          re.search(r"\d+\s*秒", d.get("message", "")) is not None,
          f"（{d.get('message')}）")

    # 未知邮箱也必须被限流，否则可用「第 4 次是否 40104」枚举已注册邮箱（API-02-1）
    ghost = f"ghost_{uuid.uuid4().hex[:6]}@example.cn"
    codes = []
    for _ in range(3):
        codes.append(body(c.post("/api/v1/auth/login", json={"email": ghost, "password": "WrongPass1"})).get("code"))
    codes.append(body(c.post("/api/v1/auth/login", json={"email": ghost, "password": "WrongPass1"})).get("code"))
    check("T-A19 未注册邮箱同样在 3 次后进入冷却（防邮箱枚举）",
          codes == [40103, 40103, 40103, 40104], f"（{codes}）")

    # =============================================================== 改密
    r = c.patch("/api/v1/users/me", headers=H, json={"new_password": PWD2})
    check("T-A20 未提供原密码 → 40103", body(r).get("code") == 40103, f"（{body(r)}）")

    r = c.patch("/api/v1/users/me", headers=H,
                json={"old_password": "NotThePassword1", "new_password": PWD2})
    check("T-A21 原密码错误 → 40103（与「未提供」同一码，不泄露差异）",
          body(r).get("code") == 40103, f"（{body(r)}）")

    r = c.patch("/api/v1/users/me", headers=H, json={"nickname": "改名后"})
    check("T-A22 仅改昵称成功，且未影响 Token 有效性",
          body(r).get("code") == 0 and body(r)["data"]["nickname"] == "改名后"
          and c.get("/api/v1/users/me", headers=H).status_code == 200,
          f"（{body(r)}）")

    r = c.patch("/api/v1/users/me", headers=H,
                json={"old_password": PWD, "new_password": PWD2})
    check("T-A23 改密成功（FR-USR-07）", body(r).get("code") == 0, f"（{body(r)}）")

    r = c.get("/api/v1/users/me", headers=H)
    check("T-A24 🔒 改密后**旧 Token 立即失效** → 40102",
          r.status_code == 401 and body(r).get("code") == 40102,
          f"（HTTP {r.status_code} / {body(r)}）")

    r = c.post("/api/v1/auth/login", json={"email": EMAIL, "password": PWD})
    check("T-A25 旧密码已不可用 → 40103", body(r).get("code") == 40103)
    r = c.post("/api/v1/auth/login", json={"email": EMAIL, "password": PWD2})
    check("T-A26 新密码可登录", body(r).get("code") == 0, f"（{body(r)}）")
    token2 = body(r)["data"]["access_token"]
    H2 = {"Authorization": f"Bearer {token2}"}

    # =============================================================== 登出
    r = c.post("/api/v1/auth/logout", headers=H2)
    check("T-A27 登出成功", body(r).get("code") == 0, f"（{body(r)}）")
    r = c.get("/api/v1/users/me", headers=H2)
    check("T-A28 🔒 登出后旧 Token 立即失效 → 40102（黑名单生效）",
          r.status_code == 401 and body(r).get("code") == 40102,
          f"（HTTP {r.status_code} / {body(r)}）")

    # 🔒 R-03（2026-10-26）：黑名单已从进程内存迁到 `revoked_tokens` 表。
    #    这里用**独立的 sqlite3 连接**直接数行，而不是调某个内存计数函数 ——
    #    这条断言的语义就是「进程重启后仍然生效」：重启丢的只有内存，丢不了库。
    jti2 = security.decode_access_token(token2).get("jti")
    with sqlite3.connect(str(settings.db_path)) as _conn:
        _rows = _conn.execute(
            "SELECT COUNT(*) FROM revoked_tokens WHERE expires_at > ?", (int(time.time()),)
        ).fetchone()[0]
        _hit = _conn.execute(
            "SELECT user_id, expires_at FROM revoked_tokens WHERE jti = ?", (jti2,)
        ).fetchone()
    check("T-A29 🔒 黑名单落库（revoked_tokens 有效行 >= 1，重启不丢）",
          _rows >= 1, f"（{_rows} 行）")
    check("T-A29b 落库的 jti 与本次登出的 Token 一一对应，且带 user_id / exp",
          _hit is not None and isinstance(_hit[0], int) and _hit[1] > int(time.time()),
          f"（jti={jti2} row={_hit}）")

    # =============================================================== 管理员
    r = c.post("/api/v1/auth/login",
               json={"email": "admin@salesforecast.cn", "password": "Admin@2026"})
    d = body(r)
    check("T-A30 🔒 管理员种子账号（seed.sql，cost=12）可登录且 role='admin'",
          d.get("code") == 0 and d["data"]["user"]["role"] == "admin",
          f"（{d}）")
    admin_token = d["data"]["access_token"] if d.get("code") == 0 else None

    # 游客哨兵 `id=0` 的 password_hash 是占位值 '!'，天然不可登录（JT-10 设计）
    r = c.post("/api/v1/auth/login",
               json={"email": "__guest__@local.invalid", "password": "AnyPass123"})
    d = body(r)
    check("T-A31 游客哨兵账号不可登录（绝不签发 Token）",
          d.get("code") != 0 and "access_token" not in r.text,
          f"（HTTP {r.status_code} / {d}）")

    # =============================================================== 审计
    con = sqlite3.connect(settings.db_path)
    try:
        rows = con.execute(
            "SELECT action, COUNT(*) FROM operation_logs GROUP BY action"
        ).fetchall()
    finally:
        con.close()
    actions = {a: n for a, n in rows}
    check("T-A32 operation_logs 落下 register / login / logout 记录（FR-SYS-03）",
          actions.get("register", 0) >= 1 and actions.get("login", 0) >= 1
          and actions.get("logout", 0) >= 1,
          f"（{actions}）")

    # =============================================================== 无明文密码
    con = sqlite3.connect(settings.db_path)
    try:
        hashes = [r[0] for r in con.execute("SELECT password_hash FROM users").fetchall()]
    finally:
        con.close()
    bcrypt_re = re.compile(r"^\$2[aby]\$\d{2}\$")
    check("T-A33 🔒 库中 password_hash 全部为 bcrypt 串（无明文）",
          all(bcrypt_re.match(h) or h == "!" for h in hashes) and len(hashes) >= 3,
          f"（{hashes[:3]}...）")
    check("T-A34 明文密码未以任何形式出现在 users 表",
          not any(h in (PWD, PWD2, "Admin@2026") for h in hashes))


# ---------------------------------------------------------------------------
# get_current_admin 的独立校验
# 需要一个「管理端」路由才能走到依赖；在独立的临时 FastAPI 实例上挂一个探针路由，
# 而不是往生产 app 里塞测试接口。
# ---------------------------------------------------------------------------
print("-" * 70)
probe = FastAPI()
register_exception_handlers(probe)


@probe.get("/admin-probe")
def _admin_probe(_admin=Depends(get_current_admin)) -> dict:
    return {"code": 0, "message": "ok", "data": {"ok": True}}


with TestClient(app) as c:
    r_user = c.post("/api/v1/auth/login", json={"email": EMAIL, "password": PWD2})
    user_token = body(r_user)["data"]["access_token"]
    r_admin = c.post("/api/v1/auth/login",
                     json={"email": "admin@salesforecast.cn", "password": "Admin@2026"})
    admin_token = body(r_admin)["data"]["access_token"]

with TestClient(probe) as pc:
    r = pc.get("/admin-probe", headers={"Authorization": f"Bearer {user_token}"})
    check("T-A35 普通用户访问管理端 → 40301（HTTP 200 + body.code）",
          r.status_code == 200 and body(r).get("code") == 40301,
          f"（HTTP {r.status_code} / {body(r)}）")
    r = pc.get("/admin-probe", headers={"Authorization": f"Bearer {admin_token}"})
    check("T-A36 管理员访问管理端 → 通过", body(r).get("code") == 0, f"（{body(r)}）")
    r = pc.get("/admin-probe")
    check("T-A37 未登录访问管理端 → 401（先认证后授权，不能返回 403）",
          r.status_code == 401 and body(r).get("code") == 40101,
          f"（HTTP {r.status_code} / {body(r)}）")

note("黑名单已落库（revoked_tokens · DB-T09 · R-03）：进程重启后已登出的 Token 仍然失效，"
     "多 worker 下各进程查同一张表也不会互相绕过（历史局限 T-Q12 已关闭）。")
note("未知邮箱的失败计数在内存（_UNKNOWN_FAIL），进程重启即清空；"
     "真实用户的计数落库，不受重启影响。这是对 API-02-1 防枚举的补强。")
note("「记住我」不影响 Token 有效期（后端统一 JWT_EXPIRE_MINUTES）。")

print("=" * 70)
print(f"通过 {passed} 项，失败 {failed} 项")
print("结论：全部通过" if failed == 0 else "结论：存在未通过项")
if failed == 0:
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    print(f"[INFO] 已清理临时库目录：{TMP_DIR}")
else:
    print(f"[INFO] 保留临时库以便复查：{settings.db_path}")
print("=" * 70)
sys.exit(0 if failed == 0 else 1)
