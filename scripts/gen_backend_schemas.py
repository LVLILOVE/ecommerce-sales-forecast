"""
=============================================================================
生成 `code/backend/schemas.py`（`R-08`）
-----------------------------------------------------------------------------
用法：
    python scripts/gen_backend_schemas.py            # 生成/覆盖
    python scripts/gen_backend_schemas.py --check    # 只校验是否与真源一致（退出码 1 = 不一致）

为什么需要「生成」而不是「直接 import」
-----------------------------------------------------------------------------
`code/backend/schemas.py` 不是运行期依赖（`app/schemas/` 才是），但它仍被六处**静态**
使用：`verify_schemas.py` 直接 `import schemas`，`verify_ep6/7/8` 读它的文本做正则与
AST 扫描，`verify_ep9_contract.py` 拿它和 `web/src/frontend/types.ts` 做字段双向比对。
所以它必须是一个**独立、可单文件 import** 的真实代码文件，不能改成一行的转发 shim ——
`verify_schemas.py` 只把 `code/backend` 加进 `sys.path`，转发 shim 会 `ImportError`。

⇒ 真源是 `app/schemas/*.py`；本脚本把它们按**依赖顺序**拼成一个独立文件。
   拼接时要剥掉的东西：各模块的 docstring、`from __future__`、第三方 import
   （合并到文件头）、`from app.schemas...` 的跨模块 import（拼完就在同一命名空间里）、
   以及各模块自己的 `__all__`。

⚠️ 顺序即依赖：`common → auth → dataset → task → result → history → admin`。
   顺序错了，pydantic 会在类创建时因为注解里的名字还没定义而 `NameError`。
=============================================================================
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_PKG = ROOT / "app" / "schemas"
TARGET = ROOT / "code" / "backend" / "schemas.py"

# 依赖顺序：前一个模块可以被后一个模块 import
ORDER = ["common", "auth", "dataset", "task", "result", "history", "admin"]

BANNER = '''"""
=============================================================================
电商销量预测系统 · 后端请求/响应模型（Pydantic v2）
-----------------------------------------------------------------------------
⚠️ **本文件由 `scripts/gen_backend_schemas.py` 自动生成，请勿手工编辑。**
  修改会被下次生成覆盖，且 `python scripts/gen_backend_schemas.py --check`
  （已进 `verify_ep9` 的 CHAIN）会判定不一致而失败。

真源     : `app/schemas/`（common / auth / dataset / task / result / history / admin）
版本     : V1.1（`R-08`，2026-10-26）
依据文档 : 《开发技术文档 V1.0》第 4 章（后端设计）、第 5 章（接口设计）
对应前端 : code/frontend/types.ts（字段逐一对应，全链路 snake_case）

存在的理由
-----------------------------------------------------------------------------
`app/schemas/` 是运行期真源；本文件是**给静态使用方看的独立副本**：
  · `scripts/verify_schemas.py`        直接 import 它跑 54 条格式约束断言
  · `scripts/verify_ep6/7/8*.py`       读文本做正则 / AST 扫描
  · `scripts/verify_ep9_contract.py`   与前端 `types.ts` 做字段双向比对
这些脚本只把 `code/backend` 加进 `sys.path`，所以本文件必须能**单文件 import**，
不能改成一个转发 shim（那会 `ImportError`）。

设计约定（与真源一致）：
  1. 每个模型只负责「格式校验」，不承载业务规则（业务规则属 services 层）。
     例外：password 的强度规则由 PRD 明确规定（FR-USR-02），属格式约束。
  2. 响应模型使用 from_attributes = True，由 ORM 实体直接转换。
  3. 面向用户的 message 必须包含「错在哪 + 怎么改」（NFR-USE-03）。
=============================================================================
"""'''


def _module_body(name: str) -> tuple[list[str], set[str], set[str]]:
    """返回 (保留的源码行, typing 名字集合, pydantic 名字集合)。"""
    path = SCHEMAS_PKG / f"{name}.py"
    src = path.read_text(encoding="utf-8")
    lines = src.split("\n")
    tree = ast.parse(src)

    drop: list[tuple[int, int]] = []
    typing_names: set[str] = set()
    pydantic_names: set[str] = set()

    for node in tree.body:
        # 模块 docstring：丢弃（文件头已有统一说明）
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and node is tree.body[0]
        ):
            drop.append((node.lineno, node.end_lineno))
            continue
        # 未来导入：文件头统一加
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            drop.append((node.lineno, node.end_lineno))
            continue
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "typing":
                typing_names |= {a.name for a in node.names}
                drop.append((node.lineno, node.end_lineno))
                continue
            if mod == "pydantic":
                pydantic_names |= {a.name for a in node.names}
                drop.append((node.lineno, node.end_lineno))
                continue
            if mod.startswith("app.schemas"):
                # 拼接后同处一个命名空间，跨模块 import 必须去掉
                drop.append((node.lineno, node.end_lineno))
                continue
        # 各模块自己的 __all__：拼接后由使用方按属性访问，不需要
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "__all__" for t in node.targets
        ):
            drop.append((node.lineno, node.end_lineno))
            continue

    skip = set()
    for a, b in drop:
        skip.update(range(a, b + 1))
    kept = [ln for i, ln in enumerate(lines, start=1) if i not in skip]
    return kept, typing_names, pydantic_names


def render() -> str:
    parts: list[str] = [BANNER, "from __future__ import annotations", ""]
    typing_names: set[str] = set()
    pydantic_names: set[str] = set()
    bodies: list[tuple[str, list[str]]] = []

    for name in ORDER:
        kept, t, p = _module_body(name)
        typing_names |= t
        pydantic_names |= p
        bodies.append((name, kept))

    if typing_names:
        parts.append("from typing import " + ", ".join(sorted(typing_names)))
    if pydantic_names:
        parts.append("from pydantic import " + ", ".join(sorted(pydantic_names)))
    parts.append("")

    for name, kept in bodies:
        # 去掉尾部多余空行，模块之间留两个空行
        while kept and not kept[-1].strip():
            kept.pop()
        while kept and not kept[0].strip():
            kept.pop(0)
        parts.append(f"# ---- 源模块：app/schemas/{name}.py " + "-" * 40)
        parts.append("")
        parts.extend(kept)
        parts.append("")
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def main(argv: list[str]) -> int:
    check = "--check" in argv
    text = render()
    if check:
        old = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if old == text:
            print(f"[ OK ] {TARGET.relative_to(ROOT)} 与 app/schemas/ 一致")
            return 0
        print(
            f"[FAIL] {TARGET.relative_to(ROOT)} 与 app/schemas/ 不一致 —— "
            "改了模型却忘了重新生成。请执行：python scripts/gen_backend_schemas.py"
        )
        return 1

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(text, encoding="utf-8", newline="\n")
    print(f"GENERATED {TARGET.relative_to(ROOT)}  ({len(text.splitlines())} 行)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
