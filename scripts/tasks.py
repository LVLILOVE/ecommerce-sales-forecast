"""
=============================================================================
统一任务入口（`R-13`）
-----------------------------------------------------------------------------
用法：
    python scripts/tasks.py              # 列出全部任务（等价于 `list`）
    python scripts/tasks.py test         # 后端单测 + 前端单测
    python scripts/tasks.py lint         # ruff + tsc 类型检查
    python scripts/tasks.py gen          # 重新生成 code/backend/schemas.py
    python scripts/tasks.py check        # 全部「秒级静态守卫」（离线可跑）
    python scripts/tasks.py verify NAME  # 跑某一个端到端校验脚本（需模型与种子库）
    python scripts/tasks.py list         # 只列任务

为什么是 `tasks.py` 而不是 `Makefile`
-----------------------------------------------------------------------------
本机（Windows + Git Bash）**没有 `make`**，写一个跑不起来的 Makefile 等于交付一份
没验证过的说明书 —— 而这个项目的纪律是「不写没验证过的东西」。
Python 入口在三个平台都能直接跑，且和「本项目是 Python 项目」这个事实一致。

📌 如果以后要加 Makefile，正确写法是**只做转发**（`make test` → `python scripts/tasks.py test`），
   逻辑仍然只有一份。

退出码：任一子任务失败即非 0（可直接用于 CI / 提交前钩子）。
=============================================================================
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

#: `check` 覆盖的守卫：全部毫秒级、不依赖模型与种子库，因此可以随手跑。
#: 每项是**参数列表**而不是字符串 —— 带空格的字符串会被当成文件名（曾踩：`--check` 变成一个路径）。
FAST_GUARDS: tuple[tuple[str, ...], ...] = (
    ("scripts/verify_orm.py",),
    ("scripts/verify_schema.py",),
    ("scripts/verify_schemas.py",),
    ("scripts/gen_backend_schemas.py", "--check"),
    ("scripts/verify_timezone.py",),
    ("scripts/verify_doc_refs.py",),
    ("scripts/verify_doc_sql.py",),
    ("scripts/verify_ep9.py",),
)


def _run(cmd: list[str], *, cwd: Path = ROOT) -> int:
    print(f"\n$ {' '.join(cmd)}", flush=True)
    try:
        return subprocess.call(cmd, cwd=str(cwd))
    except FileNotFoundError:
        print(f"[SKIP] 找不到可执行文件：{cmd[0]}")
        return 127


def _acc(prev: int, new: int) -> int:
    """累计失败标记：**保留第一个非 0 退出码**（比 `|=` 更可读，也不做位运算）"""
    return prev or new


def _py() -> str:
    """用**当前解释器**跑子任务：换 env 就等于换依赖，别猜。"""
    return sys.executable


def _node() -> str | None:
    return shutil.which("node")


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------
def task_list() -> int:
    print(__doc__.strip().split("退出码")[0].strip())
    print("\n可用的端到端校验（`verify` 的参数）：")
    names = sorted(p.stem for p in (ROOT / "scripts").glob("verify_*.py"))
    for i in range(0, len(names), 3):
        print("  " + "  ".join(f"{n:<28}" for n in names[i:i + 3]))
    return 0


def task_test() -> int:
    code = _run([_py(), "-m", "pytest", "tests", "-q"])
    node = _node()
    if node and (WEB / "node_modules" / "vitest" / "vitest.mjs").exists():
        # ⚠️ `--no-file-parallelism`：本机并发 transform 两个测试文件会被
        #    WorkBuddy 的 node fs 中间层以 EPERM 拒绝，表现为「文件数少一个但退出码 0」。
        code = _acc(
            code,
            _run(
                [node, "node_modules/vitest/vitest.mjs", "run", "--no-file-parallelism"],
                cwd=WEB,
            ),
        )
    else:
        print("[SKIP] 前端单测：未找到 node 或 web/node_modules（先 cd web && npm install）")
    return code


def task_lint() -> int:
    code = _run([_py(), "-m", "ruff", "check", "app", "scripts", "tests"])
    node = _node()
    if node and (WEB / "node_modules" / ".bin").exists():
        code = _acc(
            code, _run([node, "node_modules/typescript/bin/tsc", "--noEmit"], cwd=WEB)
        )
    else:
        print("[SKIP] 前端类型检查：未找到 node 或 web/node_modules")
    return code


def task_gen() -> int:
    return _run([_py(), "scripts/gen_backend_schemas.py"])


def task_check() -> int:
    code = 0
    for args in FAST_GUARDS:
        code = _acc(code, _run([_py(), *args]))
    if code:
        print("\n结论：存在失败项（逐个看上面的输出）")
    else:
        print("\n结论：全部静态守卫通过")
    return code


def task_verify(name: str) -> int:
    script = ROOT / "scripts" / (name if name.endswith(".py") else f"{name}.py")
    if not script.exists():
        print(f"[FAIL] 找不到校验脚本：{script.relative_to(ROOT)}")
        print("       可用列表见：python scripts/tasks.py list")
        return 2
    return _run([_py(), str(script.relative_to(ROOT))])


def main(argv: list[str]) -> int:
    if not argv:
        return task_list()
    cmd, rest = argv[0], argv[1:]
    table = {
        "list": task_list,
        "test": task_test,
        "lint": task_lint,
        "gen": task_gen,
        "check": task_check,
    }
    if cmd == "verify":
        if not rest:
            print("[FAIL] 用法：python scripts/tasks.py verify <脚本名>")
            return 2
        return task_verify(rest[0])
    if cmd not in table:
        print(f"[FAIL] 未知任务：{cmd}")
        print("       可用：list / test / lint / gen / check / verify <NAME>")
        return 2
    return table[cmd]()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
