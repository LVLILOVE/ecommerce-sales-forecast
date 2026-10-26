"""跨模块引用的「存在性」守卫。

为什么需要这个测试
-----------------------------------------------------------------------------
`R-06` 把 `dataset_service._load_csv` 迁到 `app.validators.load_csv` 后，
`result_service` 里还有一处调用没跟着改：

```python
df, _enc = dataset_service._load_csv(path, None)   # AttributeError
```

这个错误**没有让任何请求失败** —— 它被 `except Exception` 吞掉，只记了一条
「数据集读取失败，历史段降级为空」的 warning，症状是**结果页曲线少了左侧历史段**。
`verify_ep4_api`（119 项）全绿也没发现，因为那条链路不校验曲线内容；
最后是 `verify_ep6_api` 的两条曲线断言把它抓出来的。

⇒ 教训不是「少用 except」，而是：**跨模块引用私有成员时，改名不会有任何编译期保护**。
   本测试把这类引用逐个 `getattr` 一遍，改名/删除会立刻红。

判据：扫描 `app/**/*.py`，找出所有「对 app 内模块对象的属性访问」（形如
`dataset_service._load_csv`），逐个 `getattr` 校验其存在。不检查非 app 模块
（第三方库的属性访问由类型检查负责）。
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"


def _app_modules() -> list[str]:
    """列出 app 包内全部可 import 的模块名（不含 tests / 非 .py 文件）。"""
    names: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP.parent).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        names.append(".".join(parts))
    return names


def _module_aliases(tree: ast.Module) -> dict[str, str]:
    """返回 `本地别名 -> 模块完整路径`。

    ⚠️ 这里**必须**用 import 语句里的真实路径，不能靠「末段同名」去猜：
       `from app.core import config` 与 `from app.algo import config` 的别名
       都叫 `config`，靠末段匹配会解析到错的那个（`app.algo.config` 没有
       `settings`），于是本测试会对着一堆正确代码误报。
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("app."):
                    aliases[a.asname or a.name.split(".")[-1]] = a.name
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if not mod.startswith("app."):
                continue
            for a in node.names:
                # `from app.services import dataset_service` → 可能是子模块
                # `from app.core.errors import BizError` → 是类，后面 find_spec 会过滤掉
                aliases[a.asname or a.name] = f"{mod}.{a.name}"
    return aliases


def _is_module(path: str) -> bool:
    """该路径是「可 import 的模块」吗（区分「子模块」与「模块里的类/函数」）。"""
    try:
        return importlib.util.find_spec(path) is not None
    except (ImportError, ModuleNotFoundError, AttributeError, ValueError):
        return False


def _collect_refs() -> list[tuple[str, int, str, str]]:
    """返回 [(源文件, 行号, 模块完整路径, 属性名)]。"""
    refs: list[tuple[str, int, str, str]] = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for alias, full in _module_aliases(tree).items():
            if not _is_module(full):
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == alias
                ):
                    refs.append(
                        (
                            path.relative_to(APP.parent).as_posix(),
                            node.lineno,
                            full,
                            node.attr,
                        )
                    )
    return refs


def test_cross_module_attributes_exist() -> None:
    """`app.xxx` 模块上的每个属性引用都必须真实存在（含私有名）。"""
    refs = _collect_refs()
    assert refs, "没扫到任何跨模块引用 —— 扫描逻辑可能失效了"

    cache: dict[str, object] = {}
    missing: list[str] = []
    for src, line, full, attr in refs:
        mod = cache.get(full)
        if mod is None:
            mod = importlib.import_module(full)
            cache[full] = mod
        if not hasattr(mod, attr):
            missing.append(f"{src}:{line}  {full}.{attr}")

    assert not missing, (
        "以下跨模块引用指向了不存在的属性（多为改名/删除后没同步调用方）：\n  "
        + "\n  ".join(missing)
    )


@pytest.mark.parametrize("name", ["app.services.result_service", "app.services.predict_service"])
def test_services_import_cleanly(name: str) -> None:
    """服务层模块必须能独立 import —— 少了这条，上面的扫描会因 import 失败而漏报。"""
    importlib.import_module(name)
