"""
=============================================================================
注册限流端到端校验（`R-07`）
-----------------------------------------------------------------------------
用法（在工作区根目录执行）：
    C:\\ProgramData\\anaconda3\\envs\\deploy\\python.exe scripts/verify_register_rate.py

守的是什么
-----------------------------------------------------------------------------
`/auth/register` 是公开部署后**唯一不需要任何凭证的写入口**。本脚本钉住四件事：

  T-R01~03  前 N 次正常放行（不能把正常用户一起挡掉）
  T-R04     超限后返回 `40104`，且带 `retry_after_seconds`
  T-R05     超限判定发生在 **bcrypt 之前** —— 被拒的请求必须显著快于正常请求，
            否则「限流自己也要付一次 250 ms 哈希」，CPU DoS 照样成立
  T-R06     被拒的请求**不产生任何用户行**（限流不能变成「先建后拒」）

为什么单独一个脚本，而不是塞进 `verify_auth.py`
-----------------------------------------------------------------------------
`verify_auth.py` 一轮要注册 6~7 次，而本脚本必须把阈值压到 3 才能快速触发限流；
两者的 `REGISTER_MAX_ATTEMPTS` 需求冲突（config 是 frozen dataclass，导入后改不了）。
故各自用独立进程 + 独立环境变量。
=============================================================================
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# ⚠️ 必须在 import 应用之前设置环境变量（config 在导入时即读取）
# ---------------------------------------------------------------------------
TMP_DIR = ROOT / "data" / "tmp" / "verify_register_rate"
if TMP_DIR.exists():
    shutil.rmtree(TMP_DIR, ignore_errors=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = TMP_DIR / "app.db"

os.environ["DB_PATH"] = str(DB_FILE)
os.environ["JWT_SECRET"] = "verify-regrate-secret-0123456789abcdefghij"
os.environ["APP_ENV"] = "test"
os.environ["REGISTER_MAX_ATTEMPTS"] = "3"
os.environ["REGISTER_MAX_SUCCESS"] = "0"      # 只验证内存层（尝试数）

sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
except Exception as exc:  # noqa: BLE001
    print(f"[FAIL] 缺少依赖（fastapi / httpx）：{exc}")
    sys.exit(1)

from app.core.config import settings          # noqa: E402
from app.main import app                      # noqa: E402

passed = 0
failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"[ OK ] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name} {detail}")


def body(r) -> dict:
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return {}


def user_rows() -> int:
    """直接数库里的用户行（含游客哨兵）。"""
    with sqlite3.connect(str(DB_FILE)) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


PWD = "Demo12345"


def reg(c: TestClient, email: str) -> tuple[dict, float]:
    """注册一次，返回 (响应体, 耗时毫秒)。"""
    t0 = time.perf_counter()
    r = c.post("/api/v1/auth/register", json={"email": email, "password": PWD})
    return body(r), (time.perf_counter() - t0) * 1000


print("=" * 70)
print("注册限流端到端校验（R-07）")
print("=" * 70)
print(f"[INFO] 临时库：{settings.db_path}")
print(f"[INFO] REGISTER_MAX_ATTEMPTS={settings.register_max_attempts} "
      f"REGISTER_MAX_SUCCESS={settings.register_max_success}")

MAX = settings.register_max_attempts
assert MAX == 3, f"环境变量没生效：register_max_attempts={MAX}"

with TestClient(app) as c:
    check("T-R00 启动自检通过（库落到临时目录）",
          str(settings.db_path).replace("\\", "/").endswith("verify_register_rate/app.db"),
          f"（实际 {settings.db_path}）")

    # ------------------------------------------------------------ 放行区
    ok_times: list[float] = []
    for i in range(MAX):
        d, ms = reg(c, f"rate_{i}_{uuid.uuid4().hex[:6]}@example.cn")
        ok_times.append(ms)
        check(f"T-R0{i + 1} 第 {i + 1}/{MAX} 次注册正常放行",
              d.get("code") == 0, f"（{d}）")

    rows_before = user_rows()

    # ------------------------------------------------------------ 超限区
    d, blocked_ms = reg(c, f"rate_over_{uuid.uuid4().hex[:6]}@example.cn")
    check("T-R04 超限后返回 40104（限流冷却中）",
          d.get("code") == 40104, f"（{d}）")
    retry = (d.get("data") or {}).get("retry_after_seconds")
    check("T-R04b 携带 retry_after_seconds 且落在 (0, 窗口] 区间",
          isinstance(retry, int) and 0 < retry <= settings.register_window_seconds,
          f"retry_after_seconds={retry!r}")

    # 🔒 限流必须在 bcrypt 之前：被拒请求耗时应远小于一次成功注册
    avg_ok = sum(ok_times) / len(ok_times)
    check("T-R05 🔒 超限判定发生在 bcrypt 之前（被拒请求显著更快）",
          blocked_ms * 3 < avg_ok,
          f"（被拒 {blocked_ms:.1f} ms vs 正常均值 {avg_ok:.1f} ms）")

    check("T-R06 被拒的请求不产生用户行",
          user_rows() == rows_before,
          f"（前 {rows_before} 行 / 后 {user_rows()} 行）")

    # 再打两次，确认是持续拒绝而非偶发
    d2, _ = reg(c, f"rate_over2_{uuid.uuid4().hex[:6]}@example.cn")
    check("T-R07 冷却期内持续拒绝（不是一次性抖动）", d2.get("code") == 40104,
          f"（{d2}）")

    h = c.get("/health")
    check("T-R08 限流不影响服务可用性（/health 仍 200）",
          h.status_code == 200 and body(h).get("code") == 0)

print("-" * 70)
print(f"通过 {passed} 项，失败 {failed} 项")
if failed:
    print(f"[INFO] 临时库保留用于复查：{DB_FILE}")
    sys.exit(1)
print("结论：全部通过")
shutil.rmtree(TMP_DIR, ignore_errors=True)
