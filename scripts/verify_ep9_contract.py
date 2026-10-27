#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
=============================================================================
`WS-9-08` 契约一致性校验（`DOD-EP9` 第 6 条）
-----------------------------------------------------------------------------
依据文档 : 《项目开发实施方案 V1.0》§7.10 `WS-9-08`
           「前后端字段名 / 类型 / 枚举值完全对齐，无隐式依赖」
           《开发技术文档 V1.0》§5.1.1（统一响应包）、§5.2（错误码全表）
-----------------------------------------------------------------------------
四组检查（每组都是**双向**的）：

  A. 模型字段        `code/backend/schemas.py`  ↔  `web/src/types/index.ts`
  B. 路由与调用点    OpenAPI `paths`            ↔  `web/src/api/client.ts`
  C. 枚举取值域      pydantic `Literal`          ↔  TS 字面量联合类型
  D. 常量同源        后端模块常量                ↔  TS `LIMITS` / `THRESHOLDS`

为什么必须自动化
-----------------------------------------------------------------------------
`EP-4`/`EP-5` 各出过一次同型缺陷，都是「两端其中一个字段悄悄变了」：

  · `EP-4` → `EP-5`：`datasetApi.detail()` 的泛型写成 `DatasetSummary`，
    而 `API-09` 实际返回 `DatasetDetailResponse`。类型断言让 TS 闭了嘴，
    运行期 `detail.file_name` 全是 `undefined` —— 参数页渲染空白概要。
  · `EP-8`：新增管理端时又写了一个 `UpdateUserRequest`，覆盖 `EP-2` 的同名模型，
    导致 `API-05` 读 `body.nickname` 时 `AttributeError` → 500。

两次都不是「逻辑写错」，而是**契约漂移**，而契约漂移的共同特征是
「不会报错」：TS 断言与 Pydantic 的宽松配置都会把它咽下去。
所以这里不靠人眼比对，而是把两端的结构**都解析出来做集合运算**。

🔒 核心机制：`MODEL_TO_TS` 是一张**必须逐条登记的映射表**。
   两端任何一侧新增模型 / 接口而不登记，本脚本即失败。
   这样「忘了同步」在结构上不可能静默通过 —— 这是本脚本与
   `verify_schemas.py`（只查后端一侧）的关键差别。

运行
-----------------------------------------------------------------------------
    <deploy python> scripts/verify_ep9_contract.py

⚠️ 运行纪律：**每个校验脚本单独占一条命令**，不要 for 循环串跑 ——
   平台按「单次工具调用的累计删除数」计阈值（50），串跑会把正常清理
   误判成批量删除并让脚本直接退出（`EP-9` 实测踩过）。
   本脚本只读不删，但仍遵守同一纪律以免与其它脚本叠加计数。
=============================================================================
"""
from __future__ import annotations

import inspect
import json
import os
import re
import sys
import typing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 环境变量必须在 import 应用之前设置（`settings` 是 frozen dataclass，
# import 时即固化，之后改不动 —— 见 `app/core/config.py`）。
TMP_DIR = ROOT / "data" / "tmp" / "verify_ep9_contract"
os.environ.setdefault("DB_PATH", str(TMP_DIR / "app.db"))
os.environ.setdefault("UPLOAD_DIR", str(TMP_DIR / "uploads"))
os.environ.setdefault("EXPORT_DIR", str(TMP_DIR / "exports"))
os.environ.setdefault("JWT_SECRET", "verify-ep9-contract-0123456789abcdefghijkl")
os.environ.setdefault("APP_ENV", "test")

# ---------------------------------------------------------------------------
# 断言框架
# ---------------------------------------------------------------------------
PASSED = 0
FAILED = 0
DETAIL: list[dict] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    ok = bool(cond)
    if ok:
        PASSED += 1
    else:
        FAILED += 1
    DETAIL.append({"name": name, "ok": ok, "detail": detail})
    print(("[ OK ] " if ok else "[FAIL] ") + name + ("" if ok else f"  {detail}"))
    return ok


def section(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


# ---------------------------------------------------------------------------
# TS 侧解析
# ---------------------------------------------------------------------------
def _strip_comments(src: str) -> str:
    """剥掉块注释与行注释。

    ⚠️ 必须先剥注释再解析：本项目的 TS 注释里大量出现 `{code, message, data}`
       与 `//` 形态的说明。不剥的话，花括号配对会被注释里的 `{` 带偏，
       字段正则也会把注释当成字段名（例如把 `数据集` 之类抓成字段）。

    ⚠️ 行注释的 `//` 用 `(?<![:\\w])` 保护：否则 `https://` 会被从中间
       截断，把后面整行都当成注释吞掉 —— 而 URL 恰恰出现在最该被解析的
       字符串字面量里。
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?<![:\w])//[^\n]*", "", src)
    return src


def _match_brace(s: str, i: int) -> str:
    """返回 `s[i]` 处 `{` 所配对花括号的**内部**文本。"""
    if s[i] != "{":
        raise ValueError(f"期望 `{{`，实际 {s[i]!r}（下标 {i}）")
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
            if depth == 0:
                return s[i + 1 : j]
    raise ValueError("花括号不配对")


def _parse_ts_fields(body: str) -> dict[str, bool]:
    """解析 interface 花括号内部，返回 `{字段名: 是否可选}`。

    只在**最外层**（相对本 interface 的 depth == 0）识别字段，
    因此 `algorithm: { backtest_pass_ms: number }` 这类内联对象
    不会被误当成两个顶层字段。
    """
    fields: dict[str, bool] = {}
    depth = 0
    i = 0
    field_re = re.compile(r"([A-Za-z_$][\w$]*)\s*(\?)?\s*:")
    while i < len(body):
        c = body[i]
        if c in "{[(<":
            depth += 1
        elif c in "}])>":
            depth -= 1
        if depth == 0:
            m = field_re.match(body, i)
            if m:
                fields[m.group(1)] = bool(m.group(2))
                i = m.end()
                continue
        i += 1
    return fields


def parse_ts_interfaces(src: str) -> dict[str, dict[str, bool]]:
    out: dict[str, dict[str, bool]] = {}
    for m in re.finditer(r"export\s+interface\s+([A-Za-z_$][\w$]*)[^{]*\{", src):
        name = m.group(1)
        out[name] = _parse_ts_fields(_match_brace(src, m.end() - 1))
    return out


def parse_ts_union(src: str, type_name: str) -> list[str] | None:
    """解析 `export type X = 'a' | 'b';` → ['a', 'b']；非字面量联合返回 None。

    ⚠️ 同时支持**数字字面量**联合：`export type Horizon = 7 | 14 | 30;`
       只抓字符串会得到空列表，于是「TS=[] 后端=[7,14,30]」这种
       **看起来像契约不符、实际是解析器不认**的假失败就出现了。
       （`EP-9` 首轮踩过 —— 判据必须能区分「两端不同」与「我读不出来」。）
    """
    m = re.search(rf"export\s+type\s+{re.escape(type_name)}\s*=\s*([^;]+);", src)
    if not m:
        return None
    rhs = m.group(1)
    if "|" not in rhs:
        return None
    strings = re.findall(r"'([^']*)'", rhs)
    if strings:
        return strings
    numbers = re.findall(r"\b(\d+)\b", rhs)
    return numbers if numbers else None


def ts_object_block(src: str, const_name: str) -> str:
    """取 `export const X = { ... } as const;` 的对象体。"""
    m = re.search(rf"export\s+const\s+{re.escape(const_name)}\s*=\s*\{{", src)
    if not m:
        raise AssertionError(f"TS 中找不到常量 {const_name}")
    return _match_brace(src, m.end() - 1)


def ts_const_number(block: str, key: str) -> int | None:
    m = re.search(rf"\b{re.escape(key)}\s*:\s*([\d_]+)", block)
    if not m:
        return None
    raw = m.group(1).replace("_", "")
    return int(raw)


def ts_const_numbers(block: str, key: str) -> list[int] | None:
    """取 `key: [7, 14, 30] as const` 形式的数字数组。"""
    m = re.search(rf"\b{re.escape(key)}\s*:\s*\[([^\]]*)\]", block)
    if not m:
        return None
    return [int(x.replace("_", "")) for x in re.findall(r"\d[\d_]*", m.group(1))]


def ts_const_strings(src: str, name: str) -> list[str] | None:
    """取 `export const X = ['a', 'b'] as const;`。"""
    m = re.search(rf"export\s+const\s+{re.escape(name)}\s*=\s*\[([^\]]*)\]", src)
    if not m:
        return None
    return re.findall(r"'([^']*)'", m.group(1))


# ---------------------------------------------------------------------------
# Python 侧解析
# ---------------------------------------------------------------------------
def literal_values(ann: typing.Any) -> list[typing.Any] | None:
    """从注解里取出 `Literal[...]` 的取值域（自动穿透 `Optional[...]`）。"""
    if typing.get_origin(ann) is typing.Union:
        members = [a for a in typing.get_args(ann) if a is not type(None)]
        if len(members) == 1:
            ann = members[0]
    if typing.get_origin(ann) is typing.Literal:
        return [v for v in typing.get_args(ann) if v is not type(None)]
    return None


def field_literal(model: typing.Any, field: str) -> list[typing.Any] | None:
    info = model.model_fields.get(field)
    if info is None:
        return None
    return literal_values(info.annotation)


# ---------------------------------------------------------------------------
# 契约清单：后端模型 ↔ 前端 interface
# ---------------------------------------------------------------------------
# 值 = 前端 interface 名；`None` = 前端以**内联结构**承载，理由见 MODELS_INLINE_ONLY。
# 🔒 张表就是契约清单本身：新增模型必须在此登记（哪怕登记为「内联」），
#    否则 A 组会以「未登记」失败。
MODEL_TO_TS: dict[str, str | None] = {
    # ---- 通用包装
    "AppBase": None,                      # 后端基类，不是对外结构
    "ApiResponse": "ApiResponse",
    "Paginated": "Paginated",
    "ValidationErrorData": "ValidationErrorData",
    # ---- 认证与用户
    "RegisterRequest": "RegisterRequest",
    "RegisterResponse": "RegisterResponse",
    "JwtPayload": "JwtPayload",
    "LoginRequest": "LoginRequest",
    "UserProfile": "UserProfile",
    "LoginResponse": "LoginResponse",
    "UpdateUserRequest": "UpdateUserRequest",
    # ---- 数据集
    "DatasetSummary": "DatasetSummary",
    "DatasetPreview": "DatasetPreview",
    "GapWarning": None,                   # 前端内联于 UploadDatasetResponse.gap_warning
    "UploadDatasetResponse": "UploadDatasetResponse",
    "ResolveGapRequest": "ResolveGapRequest",
    "LevelPreview": "LevelPreview",
    "HorizonEstimate": "HorizonEstimate",
    "DatasetDetailResponse": "DatasetDetailResponse",
    # ---- 预测任务
    "CreateTaskRequest": "CreateTaskRequest",
    "CreateTaskResponse": "CreateTaskResponse",
    "TaskStatusResponse": "TaskStatusResponse",
    # ---- 结果
    "MetricBundle": "MetricBundle",
    "Coverage": "Coverage",
    "Distribution": "Distribution",
    "TimingMs": "TimingMs",
    "SeriesCurve": "SeriesCurve",
    "PredictionRow": "PredictionRow",
    "TaskBrief": None,                    # 前端内联于 PredictionResultResponse.task
    "DatasetBrief": None,                 # 前端内联于 PredictionResultResponse.dataset
    "PredictionResultResponse": "PredictionResultResponse",
    "DetailQuery": "PredictionDetailQuery",
    "ExportQuery": None,                  # 前端以 `params: { format }` 内联
    # ---- 历史与对比
    "HistoryQuery": "HistoryQuery",
    "HistoryItem": "HistoryItem",
    "HistoryPage": None,                  # 前端 = `Paginated<HistoryItem>`
    "CompareRequest": "CompareRequest",
    "CompareSeries": "CompareSeries",
    "CompareTaskSlot": "CompareTaskSlot",
    "CompareResponse": "CompareResponse",
    "WorkbenchSummary": "WorkbenchSummary",
    # ---- 系统
    "HealthResponse": "HealthResponse",
    # ---- 管理端
    "AdminOverviewQuery": None,           # 前端以 `params: { days }` 内联
    "AdminUserQuery": "AdminUserQuery",
    "AdminTaskQuery": "AdminTaskQuery",
    "AdminLogQuery": "AdminLogQuery",
    "AdminUpdateUserRequest": None,       # 前端内联 `{status?, role?}`（client.ts 字面量）
    "ResetPasswordRequest": None,         # 前端内联 `{ new_password }`
    "SwitchModelRequest": None,           # 前端内联 `{ is_active: true }`
    "MapeTrendPoint": "MapeTrendPoint",
    "ActiveModelBrief": "ActiveModelBrief",
    "AdminOverviewResponse": "AdminOverview",
    "AdminUserBrief": "AdminUserBrief",
    "AdminUserPage": None,                # 前端 = `Paginated<AdminUserBrief>`
    "AdminUpdateUserResponse": None,      # 前端内联 `{ user: AdminUserBrief }`
    "ResetPasswordResponse": "ResetPasswordResult",
    "AdminTaskBrief": "AdminTaskBrief",
    "AdminTaskPage": None,                # 前端 = `Paginated<AdminTaskBrief>`
    "AdminDeleteTaskResponse": "AdminDeleteResult",
    "AdminModelBrief": "AdminModelBrief",
    "AdminModelListResponse": "AdminModelList",
    "SwitchModelResponse": "SwitchModelResult",
    "AdminLogBrief": "AdminLogBrief",
    "AdminLogPage": None,                 # 前端 = `Paginated<AdminLogBrief>`
}

# 只存在于前端、后端**刻意**没有对应模型的类型（每一条都要有理由）。
TS_ONLY: dict[str, str] = {
    "TimingDetail":
        "`metrics.timing_json` 的自由结构（EP-5 埋点），落库即 JSON 文本、"
        "不参与任何接口的 response_model，因此后端**没有** Pydantic 模型。"
        "它是「随 API-12 回带的载荷」而不是契约模型（见 types/index.ts 注释）。",
}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
section("载入两端结构")

ts_src = _strip_comments((ROOT / "web" / "src" / "types" / "index.ts").read_text(encoding="utf-8"))
# ⚠️ client.ts 也必须先剥注释 —— 这是 EP-9 首轮的一处**真实误判**：
#    该文件注释里出现了反引号包起来的 `` `http.get('/health')` ``，
#    那是**反例说明**（讲「原先写错会打到 /api/v1/health 而 404」）。
#    按字面提取调用点就会凭空多出一条 `GET /api/v1/health`，
#    于是「前端每个调用点都能在后端命中」这条断言被自己的注释打败。
#    教训：校验脚本解析代码时，**注释永远是第一号污染源**。
ts_client_src = _strip_comments(
    (ROOT / "web" / "src" / "api" / "client.ts").read_text(encoding="utf-8"))
ts_ifaces = parse_ts_interfaces(ts_src)
print(f"[INFO] 前端 interface 共 {len(ts_ifaces)} 个")

try:
    import app.schemas as py_schemas
    from app.core.errors import ErrorCode
    from app.main import app as fastapi_app
except Exception as exc:  # noqa: BLE001
    print(f"[FAIL] 无法导入后端应用：{type(exc).__name__}: {exc}")
    sys.exit(1)

from pydantic import BaseModel

py_models: dict[str, type[BaseModel]] = {}
for name, obj in vars(py_schemas).items():
    if name.startswith("_"):
        continue
    if inspect.isclass(obj) and issubclass(obj, BaseModel) and obj is not BaseModel:
        # 过滤从 typing / pydantic 转发过来的名字（无 model_fields 的抽象基类）
        if hasattr(obj, "model_fields"):
            py_models[name] = obj
print(f"[INFO] 后端 Pydantic 模型共 {len(py_models)} 个"
      f"（其中对外结构 {sum(1 for v in MODEL_TO_TS.values() if v)} 个）")


# ---------------------------------------------------------------- A. 模型字段
section("A. 模型字段双向比对（code/backend/schemas.py ↔ web/src/types/index.ts）")

check("映射表覆盖后端全部模型（无未登记项）",
      set(py_models) == set(MODEL_TO_TS),
      f"未登记：{sorted(set(py_models) - set(MODEL_TO_TS))}；"
      f"表中有但已删除：{sorted(set(MODEL_TO_TS) - set(py_models))}")

mapped_ts = {v for v in MODEL_TO_TS.values() if v}
check("映射表覆盖前端全部 interface（含 TS_ONLY 白名单）",
      set(ts_ifaces) == mapped_ts | set(TS_ONLY),
      f"未登记：{sorted(set(ts_ifaces) - mapped_ts - set(TS_ONLY))}；"
      f"表中有但已删除：{sorted((mapped_ts | set(TS_ONLY)) - set(ts_ifaces))}")

for py_name in sorted(MODEL_TO_TS):
    ts_name = MODEL_TO_TS[py_name]
    if ts_name is None:
        check(f"A {py_name} → 前端内联（已登记）", True)
        continue
    if py_name not in py_models or ts_name not in ts_ifaces:
        check(f"A {py_name} ↔ {ts_name} 两端均可解析", False, "（有一侧缺失）")
        continue
    py_fields = set(py_models[py_name].model_fields)
    ts_fields = set(ts_ifaces[ts_name])
    only_py = sorted(py_fields - ts_fields)
    only_ts = sorted(ts_fields - py_fields)
    check(f"A {py_name} ↔ {ts_name} 字段名双向一致",
          not only_py and not only_ts,
          f"（仅后端有：{only_py}；仅前端有：{only_ts}）")


# ---------------------------------------------------------------- B. 路由与调用
section("B. 路由 ↔ 前端调用点双向比对（OpenAPI paths ↔ web/src/api/client.ts）")


def norm_path(p: str) -> str:
    """把路径参数归一成 `{}`，使 `${id}` 与 `{dataset_id}` 可比。"""
    p = re.sub(r"\$\{[^}]+\}", "{}", p)
    p = re.sub(r"\{[^}]+\}", "{}", p)
    return p.rstrip("/") or "/"


# 🔒 显式登记的后端路由全集（口径说明见下方断言处）。
#    ⚠️ 登记的是**集合**而不是数量：数量会因「一条路由拆成两条」而变，
#       集合则要求「谁改了路径，谁就必须来改这张表」。
#
#    归一化约定：路径参数一律写作 `{}`（见 `norm_path()`）。
#    按 API 编号分组，便于与《技术文档》§5.3 逐条对照。
EXPECTED_ROUTES: set[tuple[str, str]] = {
    # ---- API-01~05 认证与用户（5）
    ("POST", "/api/v1/auth/register"),
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/logout"),
    ("GET", "/api/v1/users/me"),
    ("PATCH", "/api/v1/users/me"),
    # ---- API-06~11 数据集（7）—— ⚠️ 比编号数多 1：
    #      `API-11` 的断档处置另占 `/datasets/gap`，而 `API-21` 之外的
    #      `template` / `errors` 也与编号一一对应，故此处 7 条。
    ("POST", "/api/v1/datasets"),
    ("GET", "/api/v1/datasets/sample"),
    ("GET", "/api/v1/datasets/template"),
    ("GET", "/api/v1/datasets/{}"),
    ("GET", "/api/v1/datasets/{}/preview"),
    ("GET", "/api/v1/datasets/{}/errors"),
    ("POST", "/api/v1/datasets/gap"),
    # ---- API-12~16 预测任务与结果（5）
    ("POST", "/api/v1/predictions"),
    ("GET", "/api/v1/predictions/{}/status"),
    ("GET", "/api/v1/predictions/{}"),
    ("GET", "/api/v1/predictions/{}/detail"),
    ("GET", "/api/v1/predictions/{}/export"),
    # ---- API-17~20 历史与对比（5）—— ⚠️ 比编号数多 1：
    #      `/history/dashboard`（UX-P04 工作台概览）与 `API-19` 同属历史域，
    #      但它是独立路由，不与任何编号重合。
    ("GET", "/api/v1/history"),
    ("GET", "/api/v1/history/dashboard"),
    ("POST", "/api/v1/history/compare"),
    ("GET", "/api/v1/history/{}/export"),
    ("GET", "/api/v1/history/{}"),
    ("DELETE", "/api/v1/history/{}"),
    # ---- API-21 健康检查（1）—— ★ 挂在**根路径**，不在 `/api/v1` 下
    ("GET", "/health"),
    # ---- API-22~30 管理端（9）
    ("GET", "/api/v1/admin/overview"),
    ("GET", "/api/v1/admin/users"),
    ("PATCH", "/api/v1/admin/users/{}"),
    ("POST", "/api/v1/admin/users/{}/reset-password"),
    ("GET", "/api/v1/admin/tasks"),
    ("DELETE", "/api/v1/admin/tasks/{}"),
    ("GET", "/api/v1/admin/models"),
    ("PATCH", "/api/v1/admin/models/{}"),
    ("GET", "/api/v1/admin/logs"),
}

spec = fastapi_app.openapi()
backend_routes: set[tuple[str, str]] = set()
for path, ops in spec["paths"].items():
    for method in ops:
        m = method.upper()
        if m in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            backend_routes.add((m, norm_path(path)))

frontend_calls: set[tuple[str, str]] = set()
for m in re.finditer(r"\b(?:http|axios)\.(get|post|patch|delete|put)\(\s*[`'\"]([^`'\"]+)[`'\"]",
                     ts_client_src):
    method, path = m.group(1).upper(), m.group(2)
    frontend_calls.add((method, norm_path("/api/v1" + path)))

# `API-21` 的 /health 不在 /api/v1 下，且地址由函数拼装（axios.get(healthUrl())），
# 正则抓不到字面量 —— 单独处理，否则会误判成「后端有死接口」。
check("API-21 /health 在客户端有显式实现（healthUrl 剥 baseURL 路径）",
      "healthUrl" in ts_client_src and "'/health'" in ts_client_src.replace('"/health"', "'/health'"))
frontend_calls.add(("GET", "/health"))

print(f"[INFO] 后端路由 {len(backend_routes)} 个；前端调用点 {len(frontend_calls)} 个")
print("[INFO] 后端路由清单：")
for _m, _p in sorted(backend_routes):
    print(f"        {_m:6s} {_p}")

only_backend = sorted(backend_routes - frontend_calls)
only_frontend = sorted(frontend_calls - backend_routes)
check("后端每个路由都被前端消费（无死接口）",
      not only_backend, f"（前端未调用：{only_backend}）")
check("前端每个调用点都能在后端路由表中命中（无 404 隐患）",
      not only_frontend, f"（后端无此路由：{only_frontend}）")

# 🔒 **口径说明**：技术文档 §5.3 说「21（既有）+ 9（管理端）= 30」，但那个 30
#    是 **`API-01`~`API-30` 的编号数**，与 OpenAPI 的**路由条目数**并不相等 ——
#    一个 API 编号可能落成多条路由（`API-21` 的 `/health` 独挂根路径；
#    `API-11` 的断档处置另占 `/datasets/gap`）。`EP-9` 首轮在这里直接
#    断言「路由数 == 31」，结果实测 32 而失败 —— **拿编号数当路由数**，
#    属于跨口径硬比，是断言设计错误而不是产品问题。
#
#    改用**显式登记清单**：实际路由集合必须与 `EXPECTED_ROUTES` 完全相等。
#    这样任何新增/删除/改路径都必须同步登记，比数数更有证明力。
check("后端路由集合与登记清单完全一致（增删或改路径都必须同步登记）",
      backend_routes == EXPECTED_ROUTES,
      f"（多出：{sorted(backend_routes - EXPECTED_ROUTES)}；"
      f"缺失：{sorted(EXPECTED_ROUTES - backend_routes)}）")


# ---------------------------------------------------------------- C. 枚举取值域
section("C. 枚举取值域比对（pydantic Literal ↔ TS 字面量联合类型）")

ENUM_SPECS: list[tuple[str, list[tuple[str, str]], str]] = [
    ("TaskStatus", [("CreateTaskResponse", "status"), ("TaskStatusResponse", "status")],
     "任务状态机四态（开发方案 §2.3，已去掉 queued）"),
    ("GranularityLevel", [("SeriesCurve", "granularity_level"),
                          ("PredictionRow", "granularity_level"),
                          ("CompareSeries", "granularity_level")],
     "建模三级 L1/L2/L3"),
    ("TaskErrorCode", [("CreateTaskResponse", "error_code")], "任务级失败原因码"),
    ("UserRole", [("UserProfile", "role")], "用户角色（CH-01）"),
]

for ts_type, backends, why in ENUM_SPECS:
    ts_vals = parse_ts_union(ts_src, ts_type)
    if ts_vals is None:
        check(f"C 前端类型 {ts_type} 可解析", False, "（不是字面量联合）")
        continue
    ok_all = True
    detail = ""
    for model_name, field in backends:
        model = py_models.get(model_name)
        vals = field_literal(model, field) if model else None
        if vals is None:
            ok_all = False
            detail += f" {model_name}.{field} 不是 Literal；"
            continue
        if [str(v) for v in vals] != [str(v) for v in ts_vals]:
            ok_all = False
            detail += f" {model_name}.{field}={vals} ≠ TS {ts_vals}；"
    check(f"C {ts_type} 取值域两端一致（{why}）", ok_all, detail)

# Horizon：前端是 7|14|30，后端在 CreateTaskRequest.horizon 上校验（非 Literal）
ts_horizons = parse_ts_union(ts_src, "Horizon")
py_horizons = field_literal(py_models["CreateTaskRequest"], "horizon")
if py_horizons is None:
    # 后端用 field_validator 限定取值域，退化为「取有效值探针」
    probe: list[int] = []
    for h in (7, 14, 30, 15, 0):
        try:
            py_models["CreateTaskRequest"](dataset_id=1, horizon=h)
            probe.append(h)
        except Exception:  # noqa: BLE001
            pass
    py_horizons = probe
check("C Horizon 取值域两端一致（FR-MOD-02 三档）",
      [int(x) for x in (ts_horizons or [])] == [int(x) for x in py_horizons],
      f"（TS={ts_horizons} 后端={py_horizons}）")

# ErrorCode：前端是 `const ERR = {...}`，后端是 `class ErrorCode`
err_block = ts_object_block(ts_src, "ERR")
ts_err = {k: int(v.replace("_", ""))
          for k, v in re.findall(r"\b([A-Z][A-Z0-9_]*)\s*:\s*(\d[\d_]*)\s*,", err_block)}
py_err = {k: v for k, v in vars(ErrorCode).items()
          if k.isupper() and isinstance(v, int)}
check("C 错误码常量表两端完全一致（PRD FR-SYS-02 / 技术文档 §5.2）",
      ts_err == py_err,
      f"（仅前端：{sorted(set(ts_err) - set(py_err))}；"
      f"仅后端：{sorted(set(py_err) - set(ts_err))}；"
      f"值不同：{sorted(k for k in set(ts_err) & set(py_err) if ts_err[k] != py_err[k])}）")
print(f"[INFO] 错误码 {len(py_err)} 个（含 OK=0）")


# ---------------------------------------------------------------- D. 常量同源
section("D. 常量同源比对（后端模块常量 ↔ TS LIMITS / THRESHOLDS）")

from app.algo import config as algo_config              # noqa: E402
from app.models.metric import (                          # noqa: E402
    THRESHOLD_IMPROVE_PCT,
    THRESHOLD_MAPE,
    THRESHOLD_PASS_RATE,
)
from app.models.prediction import EXPORT_COLUMNS         # noqa: E402

limits = ts_object_block(ts_src, "LIMITS")
thresholds = ts_object_block(ts_src, "THRESHOLDS")

NUM_PAIRS = [
    ("max_file_size_mb", "MAX_FILE_SIZE_MB", py_schemas.MAX_FILE_SIZE_MB, "NFR-PERF-03"),
    ("max_rows", "MAX_ROWS", py_schemas.MAX_ROWS, "NFR-PERF-04"),
    ("max_series", "MAX_SERIES", py_schemas.MAX_SERIES, "NFR-PERF-05"),
    ("sla_seconds", "SLA_SECONDS", py_schemas.SLA_SECONDS, "NFR-PERF-01"),
    ("l1_min_valid_points", "L1_MIN_VALID_POINTS", py_schemas.L1_MIN_VALID_POINTS, "FR-FEA-09"),
    ("password_max_chars", "PASSWORD_MAX_CHARS", py_schemas.PASSWORD_MAX_CHARS, "JT-12"),
    ("password_max_bytes", "BCRYPT_MAX_BYTES", py_schemas.BCRYPT_MAX_BYTES, "JT-12"),
]
for ts_key, py_name, py_val, ref in NUM_PAIRS:
    got = ts_const_number(limits, ts_key)
    check(f"D LIMITS.{ts_key} == {py_name}（{ref}）",
          got == int(py_val), f"（TS={got} 后端={py_val}）")

horizons_ts = ts_const_numbers(limits, "allowed_horizons")
check("D LIMITS.allowed_horizons == CreateTaskRequest 的可接受取值",
      horizons_ts == [int(x) for x in (py_horizons or [])],
      f"（TS={horizons_ts} 后端={py_horizons}）")

limit_mape = ts_const_number(limits, "weighted_mape_limit")
limit_improve = ts_const_number(limits, "baseline_improve_limit")
limit_pass = ts_const_number(limits, "series_pass_rate_target")
check("D LIMITS 三阈值 == app/models/metric.py 的 THRESHOLD_*",
      float(limit_mape or -1) == THRESHOLD_MAPE
      and float(limit_improve or -1) == THRESHOLD_IMPROVE_PCT
      and float(limit_pass or -1) == THRESHOLD_PASS_RATE,
      f"（LIMITS={limit_mape}/{limit_improve}/{limit_pass} "
      f"后端={THRESHOLD_MAPE}/{THRESHOLD_IMPROVE_PCT}/{THRESHOLD_PASS_RATE}）")

th_mape = re.search(r"weighted_mape_max\s*:\s*([\d.]+)", thresholds)
th_imp = re.search(r"baseline_improve_min\s*:\s*([\d.]+)", thresholds)
th_pass = re.search(r"series_pass_rate_min\s*:\s*([\d.]+)", thresholds)
check("D THRESHOLDS 三阈值 == app/models/metric.py 的 THRESHOLD_*",
      float(th_mape.group(1) if th_mape else -1) == THRESHOLD_MAPE
      and float(th_imp.group(1) if th_imp else -1) == THRESHOLD_IMPROVE_PCT
      and float(th_pass.group(1) if th_pass else -1) == THRESHOLD_PASS_RATE,
      f"（THRESHOLDS={th_mape.group(1) if th_mape else None}/"
      f"{th_imp.group(1) if th_imp else None}/{th_pass.group(1) if th_pass else None}）")

ts_req_cols = ts_const_strings(ts_src, "REQUIRED_COLUMNS")
check("D REQUIRED_COLUMNS 两端一致（PRD §3.2.2 强制列名契约）",
      tuple(ts_req_cols or ()) == tuple(py_schemas.REQUIRED_COLUMNS),
      f"（TS={ts_req_cols} 后端={list(py_schemas.REQUIRED_COLUMNS)}）")

ts_export = ts_const_strings(ts_src, "EXPORT_COLUMNS")
# 🔒 前端 `EXPORT_COLUMNS` 对应的是后端的 **`result_service.EXPORT_HEADER`**
#    （交付给业务方的表头），**不是** `models/prediction.EXPORT_COLUMNS`。
#
#    ⚠️ 这两个名字在两端都存在、都是 7 列、且都自称与 `FR-VIS-05` 有关，
#       但语义完全不同：
#         · `models.prediction.EXPORT_COLUMNS` = **库表列名**（forecast_date /
#           predicted_value / actual_value / baseline_value）
#         · `result_service.EXPORT_HEADER`    = **导出文件表头**（date /
#           forecast_sales / actual_sales / error）
#       两者列名**刻意不同**（`result_service.py:70` 起有详细理由：不向业务方
#       泄漏 `forecast_date` 这类实现术语）。
#       `EP-9` 首轮把前端常量对到了库列名上，于是报出「两端不一致」——
#       结论看似严重、其实是我的映射错了。**同名不同义**是契约审查里
#       最容易误报的一类，必须在此显式记下来，避免下一个人再错一次。
from app.services.result_service import EXPORT_HEADER  # noqa: E402

check("D EXPORT_COLUMNS(前端) == EXPORT_HEADER(后端业务表头) 且顺序相同（FR-VIS-05）",
      tuple(ts_export or ()) == tuple(EXPORT_HEADER),
      f"（TS={ts_export} 后端={list(EXPORT_HEADER)}）")

check("D 库列序与业务表头**必须不同**（同名不同义，防止有人「顺手统一」掉）",
      tuple(EXPORT_COLUMNS) != tuple(EXPORT_HEADER),
      f"（库={list(EXPORT_COLUMNS)}；表头={list(EXPORT_HEADER)}）")

check("D 两者列数相同（7 列，仅列名口径不同）",
      len(EXPORT_COLUMNS) == len(EXPORT_HEADER) == 7)

ts_validate = ts_const_strings(ts_src, "VALIDATE_ORDER")
check("D VALIDATE_ORDER 两端一致且**顺序**相同（PRD §3.2.2 越靠前越快失败）",
      tuple(ts_validate or ()) == tuple(algo_config.VALIDATE_ORDER),
      f"（TS={ts_validate} 后端={list(algo_config.VALIDATE_ORDER)}）")

check("D TOKEN_VERSION_DEFAULT 两端一致（TQ-1；DDL DEFAULT 1 为同一真源）",
      re.search(r"export\s+const\s+TOKEN_VERSION_DEFAULT\s*=\s*(\d+)", ts_src)
      and int(re.search(r"export\s+const\s+TOKEN_VERSION_DEFAULT\s*=\s*(\d+)", ts_src).group(1))
      == py_schemas.TOKEN_VERSION_DEFAULT)


# ---------------------------------------------------------------- 汇总
section("汇总")
print(f"通过 {PASSED} 项，失败 {FAILED} 项")

report = {
    "stage": "WS-9-08 契约一致性",
    "passed": PASSED,
    "failed": FAILED,
    "ts_interfaces": len(ts_ifaces),
    "py_models": len(py_models),
    "backend_routes": len(backend_routes),
    "frontend_calls": len(frontend_calls),
    "error_codes": len(py_err),
    "items": DETAIL,
}
out = ROOT / "outputs" / "ep9_contract_report.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"报告已写入：{out}")
print("结论：全部通过" if FAILED == 0 else "结论：存在未通过项")
sys.exit(0 if FAILED == 0 else 1)
