"""
=============================================================================
时区纪律守卫（`R-11`，2026-10-26）
-----------------------------------------------------------------------------
用法：
    python scripts/verify_timezone.py

为什么不做「统一改成 UTC」
-----------------------------------------------------------------------------
`R-11` 原始描述是「时间戳混用：DB 14 处 `localtime`、Python 11 处 naive
`datetime.now()`、仅 1 处 tz-aware」。看起来该做的是「全部改 UTC」，但那会**同时**
引入两类真实风险，而收益只在「多地域部署」时才出现（本项目是单机单时区）：

  ① **历史数据不可逆**：库里已有 67k 条 `predictions` 与全部任务时间戳都是本地时间
     字符串。改成 UTC 而不迁移 = 新旧数据差 8 小时，且无法区分哪些是哪种；
     迁移则需要一次性改写全库并回滚预案 —— 对一个 P2 收益来说风险倒挂。
  ② **展示层要跟着改**：前端直接把 `created_at` 当字符串渲染，改存 UTC 后
     中文用户会看到「任务发生在 8 小时前」。要做就得把 14 个时间字段全部
     走一层「UTC→本地」转换，属于跨全栈改造。

⇒ 明确保留 `NFR-COMP-04` 的**单时区假设**（存本地时间、存 TEXT、不转换），
   改用本脚本把这条纪律**从文档约定变成可执行断言**，专治「混用」。

本脚本守三条
-----------------------------------------------------------------------------
  1. 🔒 `code/db/schema.sql` 的时间默认值必须是 `datetime('now', 'localtime')`。
     ⚠️ SQLite 的 `datetime('now')` 是 **UTC**。漏写 `'localtime'` 不会有任何报错，
        只会让这一列的 6 小时/8 小时偏移与其他列不一致 —— 属于「静默错一半」。
  2. 🔒 `app/` 下（除 `core/security.py`）不得出现 `utcnow()` / `timezone.utc` /
     `astimezone()`：业务时间一律 naive local。
  3. 🔒 反过来，`core/security.py` **必须**保留 tz-aware（JWT 的 `iat`/`exp` 按
     RFC 7519 是 UTC epoch 秒）。若被「统一」成 `datetime.now()`，Token 会**多活
     8 小时**才过期 —— 这是最难察觉的安全退化，所以单独正向断言。
=============================================================================
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
SCHEMA = ROOT / "code" / "db" / "schema.sql"

#: 唯一允许（且要求）使用 tz-aware 的模块
TZ_AWARE_ALLOWED = APP / "core" / "security.py"

#: 业务代码里禁止出现的写法
FORBIDDEN = (
    (re.compile(r"\butcnow\s*\("), "utcnow()"),
    (re.compile(r"\btimezone\.utc\b"), "timezone.utc"),
    (re.compile(r"\.astimezone\s*\("), "astimezone()"),
)

results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def strip_py_comments(src: str) -> str:
    """剥注释与 docstring —— 判「有没有这么写」前必须先剥（本项目第 6 次踩）。"""
    out = re.sub(r'"""[\s\S]*?"""', "", src)
    out = re.sub(r"'''[\s\S]*?'''", "", out)
    return re.sub(r"(?m)^\s*#.*$", "", out)


# ---------------------------------------------------------------------------
# 1) DDL 的默认值必须带 'localtime'
# ---------------------------------------------------------------------------
sql = SCHEMA.read_text(encoding="utf-8")
now_calls = re.findall(r"datetime\(\s*'now'\s*(?:,\s*'([a-zA-Z]+)')?\s*\)", sql)
bare_utc = [m for m in now_calls if m != "localtime"]
check(
    "① 🔒 schema.sql 全部 datetime('now', 'localtime')",
    not bare_utc,
    f"发现 {len(bare_utc)} 处裸 datetime('now')（SQLite 里那是 UTC）：{[m for m in now_calls if m != 'localtime']}",
)
check(
    "①-2 时间默认值数量与库内时间列规模相当（防误删）",
    len(now_calls) >= 14,
    f"仅 {len(now_calls)} 处（预期 ≥14）",
)

# ---------------------------------------------------------------------------
# 2) app/ 下禁止 tz-aware（security.py 除外）
# ---------------------------------------------------------------------------
hits: list[str] = []
for path in sorted(APP.rglob("*.py")):
    if path == TZ_AWARE_ALLOWED:
        continue
    code = strip_py_comments(path.read_text(encoding="utf-8"))
    for pattern, label in FORBIDDEN:
        if pattern.search(code):
            hits.append(f"{path.relative_to(ROOT).as_posix()} → {label}")
check(
    "② 🔒 app/ 业务代码无非本地时区写法（utcnow / timezone.utc / astimezone）",
    not hits,
    "；".join(hits),
)

# ---------------------------------------------------------------------------
# 3) security.py 必须保留 tz-aware
# ---------------------------------------------------------------------------
sec_code = strip_py_comments(TZ_AWARE_ALLOWED.read_text(encoding="utf-8"))
check(
    "③ 🔒 security.py 的 JWT 时间戳保持 tz-aware（否则过期时间会偏移 8 小时）",
    "timezone.utc" in sec_code and "utcnow(" not in sec_code,
    "期望出现 timezone.utc（RFC 7519 的 iat/exp 是 UTC epoch 秒）",
)

# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
passed = sum(1 for _, ok, _ in results if ok)
failed = len(results) - passed
print("=" * 70)
print("时区纪律守卫（R-11）：单时区假设 = 存本地时间 + TEXT + 不转换")
print("=" * 70)
for name, ok, detail in results:
    print(f"[{' OK ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if not ok and detail else ""))
print("-" * 70)
print(f"通过 {passed} 项，失败 {failed} 项")
if failed:
    print("结论：存在失败项")
    sys.exit(1)
print("结论：全部通过")
