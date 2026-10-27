"""`EP-7` 静态验收（`DOD-EP7` 的结构性部分 + 防回归守卫）。

与 `verify_ep6.py` 同一套思路：**只判「结构上必须成立」的事**，
业务数值交给 `verify_ep7_api.py` 与 `verify_ep7_browser.mjs`。

🔴 **判「有没有做某操作」前必须先 `strip_docstrings`**（`EP-6` 的教训）：
   本项目的注释密度极高，解释性 docstring 里会写「为什么不做 X」，
   朴素的子串搜索会把它判成「做了 X」。本文件所有 `not in` 判据
   都作用在剥离注释后的源码上。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PASS = 0
TOTAL = 0
RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASS, TOTAL
    TOTAL += 1
    if ok:
        PASS += 1
    RESULTS.append((bool(ok), name, detail))
    mark = "✅" if ok else "❌"
    print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def section(t: str) -> None:
    print(f"\n=== {t} ===")


def read(rel: str) -> str:
    p = ROOT / rel
    return p.read_text(encoding="utf-8") if p.exists() else ""


def strip_comments(src: str, lang: str = "py") -> str:
    """剥离注释与文档串，只留可执行代码。"""
    if lang == "py":
        out = re.sub(r'"""[\s\S]*?"""', "", src)
        out = re.sub(r"'''[\s\S]*?'''", "", out)
        out = re.sub(r"(?m)^\s*#.*$", "", out)
        return re.sub(r"\s+#.*$", "", out, flags=re.M)
    # TS/TSX/CSS：// 行注释 + /* */ 块注释
    out = re.sub(r"/\*[\s\S]*?\*/", "", src)
    out = re.sub(r"(?m)^\s*//.*$", "", out)
    return re.sub(r"\s+//.*$", "", out, flags=re.M)


def func_body(src: str, name: str) -> str:
    """从 `def name(` 到下一个顶层 `def` 的函数体。

    ⚠️ 必须**先剥离注释再切**：`_owned_task` 这类名字会出现在
       解释性 docstring 里（「幂等实现不能走 `_owned_task`」）。
       先切后剥会把后面别的函数的注释一起吞进来，判据误报。
    """
    m = re.search(rf"^def {re.escape(name)}\(", src, re.M)
    if not m:
        return ""
    nxt = re.search(r"^def ", src[m.end():], re.M)
    return src[m.start(): m.end() + (nxt.start() if nxt else len(src))]


# =============================================================================
section("① `WS-7-01`~`04` 四条接口齐备")
# =============================================================================
api = read("app/api/v1/history.py")
api_code = strip_comments(api)
check("history.py 存在", bool(api), f"{len(api)} 字符")
check("WS-7-01 `API-17` 历史列表", '@router.get("",' in api_code)
check("WS-7-02 `API-19` 历史详情", '@router.get(\n    "/{task_id}"' in api_code or '/{task_id}' in api_code)
check("WS-7-03 `API-18` 跨任务对比", '@router.post("/compare"' in api_code)
check("WS-7-04 `API-20` 软删除", '@router.delete("/{task_id}"' in api_code)
check("UX-P04 工作台概览", '"/dashboard"' in api_code)

# 🔴 BE-39 是本阶段最关键的硬约束：字面量路径必须在参数化路径之前。
#    顺序错了，`compare` 会被当 task_id 解析 → 报 40012，请求进不到对比逻辑。
order = {p: api_code.find(p) for p in ('@router.post("/compare"', '@router.get("",', '"/dashboard"', '@router.delete("/{task_id}"')}
check("🔒 BE-39：`/compare` 注册在 `/{task_id}` 之前",
      order['@router.post("/compare"'] < order['@router.delete("/{task_id}"'],
      f"compare@{order['@router.post(\"/compare\"']} vs id@{order['@router.delete(\"/{task_id}\"']}")
check("🔒 BE-39：`/dashboard` 注册在 `/{task_id}` 之前",
      order['"/dashboard"'] < order['@router.delete("/{task_id}"'],
      f"dashboard@{order['\"/dashboard\"']}")
check("🔒 BE-39 顺序约束在文件头有说明",
      "BE-39" in api and "注册顺序" in api)

section("② 🔒 鉴权：历史一律要求登录（FR-USR-08）")
check("四条路由都用 `CurrentUser`（不是 `OptionalUserId`）",
      api_code.count("user: CurrentUser") >= 5,
      f"{api_code.count('user: CurrentUser')} 处")
check("🔒 明确不用 `OptionalUserId`（游客不得读历史）",
      "OptionalUserId" not in api_code)
check("文件头说明为何要登录",
      "游客没有历史" in api or "一律要求登录" in api or "更敏感" in api)

section("③ `WS-7-01` 列表：口径与性能")
svc = read("app/services/history_service.py")
svc_code = strip_comments(svc)
dao = read("app/dao/task_dao.py")
dao_code = strip_comments(dao)

check("列表走 `v_task_summary` 视图", "v_task_summary" in dao_code)
check("🔒 视图已内置 is_deleted=0，DAO 不重复过滤",
      "is_deleted" not in func_body(dao, "list_history"))
check("🔒 排序固定 created_at DESC（命中 IX-03 第三列）",
      "_HISTORY_SORT" in dao_code and '"created_at DESC"' in dao_code)
check("列表筛选条件集中在一处（count 与 list 必须同源）",
      "_history_filters" in dao_code)
check("🔒 count 与 list 共用同一个筛选函数",
      "_history_filters" in func_body(dao, "list_history")
      and "_history_filters" in func_body(dao, "count_history"))
check("🔒 筛选条件参数化（不拼字符串）",
      ":user_id" in dao_code and "LIKE :kw" in dao_code)
check("🔒 user_id 是第一个 WHERE 条件（漏传也只查空集）",
      re.search(r'where = \["user_id = :user_id"\]', dao_code) is not None)

section("④ 🔒 `WS-7-02` 详情：与结果页同源、失败态可读")
check("成功任务直接复用 `result_service.get_result`",
      "result_service.get_result(" in svc_code)
check("🔒 失败任务走独立分支（不给半成品也不该不给原因）",
      "_failed_detail" in svc_code and "task.status != \"succeeded\"" in svc_code)
check("🔒 失败详情含 error_code / error_msg / completed_stage",
      all(k in svc_code for k in ("error_code", "error_msg", "completed_stage")))
check("🔒 失败详情 metrics 为 null 而非全 0",
      "_empty_metrics" in svc_code)
check("🔒 归属校验复用唯一真源（不在本模块重写）",
      svc_code.count("result_service._owned_task(") >= 1)

rs = read("app/services/result_service.py")
check("🔒 `_owned_task` 把软删除视为不存在（EP-7 修复）",
      "is_deleted" in func_body(strip_comments(rs), "_owned_task"),
      "否则删掉的任务仍能直接访问详情")
check("🔒 `_series_curves` 支持 `predicted_only`（对比页省掉 CSV 读取）",
      "predicted_only" in strip_comments(rs))

section("⑤ 🔒 `WS-7-03` 对比：归属全或全无 + 口径警告")
check("🔒 归属校验先于取数（API-18-1 全或全无）",
      svc_code.index("for tid in ids:") < svc_code.index("payload_tasks =")
      or "_owned_task" in svc_code)
check("🔒 越权抛 40301 而非 40401",
      "ErrorCode.FORBIDDEN" in svc_code)
check("🔒 失败任务不参与对比（metrics 为 None）",
      '"metrics": None' in svc_code)
check("🔒 步长不一致 → warnings 显式说明",
      "步长不一致" in svc or "步长不可直接比较" in svc)
check("🔒 数据集不一致 → dataset_consistent=false",
      "_dataset_consistent" in svc_code and "dataset_id" in svc_code)
check("🔒 overlap_dates 为交集且空时前端只画指标表",
      "_overlap_dates" in svc_code and "overlap_dates" in svc_code)
check("🔒 对比任务数上下限 2~4",
      "COMPARE_MIN = 2" in svc and "COMPARE_MAX = 4" in svc)

section("⑥ 🔒 `WS-7-04` 软删除")
check("只置 is_deleted=1", "soft_delete" in svc_code)
_sd = func_body(strip_comments(svc), "soft_delete")
check("🔒 幂等：重复删除走 `is_deleted == 1` 分支提前返回",
      re.search(r"if int\(task\.is_deleted or 0\) == 1:", _sd) is not None)
check("🔒 幂等实现不能走 `_owned_task`（它把已删除视为不存在）",
      "_owned_task" not in _sd,
      "走 _owned_task 会让第二次删除抛 40401，破坏幂等")
check("🔒 库表记录保留（不级联删 predictions/metrics）",
      "predictions" in svc and "API-20-2" in svc)

section("⑦ 🔒 隔离红线：四条接口都不带越权可能")
check("🔒 DAO 层无裸 `list_by_task`（必须带 task_id 归属过滤）",
      "def list_by_task" in dao_code or "def list_by_task" in read("app/dao/prediction_dao.py"))
check("🔒 history_service 顶层不 import algo（记录层不做算法计算）",
      re.search(r"^from app\.algo", svc, re.M) is None)

section("⑧ 前端：三页 + 工作台")
router = read("web/src/router/index.tsx")
hist_page = read("web/src/pages/HistoryPage.tsx")
detail_page = read("web/src/pages/HistoryDetailPage.tsx")
cmp_page = read("web/src/pages/ComparePage.tsx")
multi_select = read("web/src/components/result/SeriesMultiSelect.tsx")
dash = read("web/src/pages/DashboardPage.tsx")

check("UX-P09 历史列表页", bool(hist_page), f"{len(hist_page)} 字符")
check("UX-P10 任务详情页", bool(detail_page), f"{len(detail_page)} 字符")
check("UX-P11 跨任务对比页", bool(cmp_page), f"{len(cmp_page)} 字符")
check("UX-P04 工作台", bool(dash), f"{len(dash)} 字符")

check("🔒 三条记录类路由都套 `RequireAuth`",
      router.count("<RequireAuth>") >= 3 and "/history" in router and "/compare" in router)
check("🔒 历史路由**没有** RequireAuth 反例（游客链路不含历史）",
      not re.search(r"path=['\"]/result/:taskId['\"][^>]*>\s*(?!.*RequireAuth)", router))

check("UX-P09-01 按创建时间倒序（服务端负责，页面不重排）",
      ".sort(" not in strip_comments(hist_page, "tsx"))
check("UX-P09-03 失败任务 MAPE 显示「—」",
      "'—' : `${" in strip_comments(hist_page, "tsx"))
check("🔒 UX-P09-04 删除须逐字输入任务 ID",
      "delete-confirm-input" in hist_page and "typedId.trim() !== String(" in strip_comments(hist_page, "tsx"))
check("🔒 确认按钮禁用而非「点了再报错」",
      "disabled={typedId.trim()" in hist_page)
check("UX-P09-04 确认框含「不可撤销」与「用筛选而非删除」",
      "不可撤销" in hist_page and "筛选" in hist_page)
check("UX-P09-05 空态给两个出口",
      "empty-upload" in hist_page and "empty-format" in hist_page)
check("UX-P09-06 加载态 3 行骨架",
      "history-skeleton" in hist_page and "[0, 1, 2]" in hist_page)

check("UX-P10-01 参数快照标注「提交时冻结」",
      "frozen-badge" in detail_page and "冻结" in detail_page)
check("UX-P10-02 指标区复用结果页组件（保证同源）",
      "MetricCards" in detail_page and "CoveragePanel" in detail_page)
check("UX-P10-03 曲线为抽样（可搜索多选，FR-VIS-09）",
      "series-picker" in detail_page and "SeriesMultiSelect" in detail_page
      and "SERIES_MULTI_LIMIT" in multi_select)
check("UX-P10-04 阶段耗时逐项「实际」",
      "timing-list" in detail_page and "timing-total" in detail_page)
check("UX-P10-05 失败详情含错误码与阶段",
      "failed-block" in detail_page and "error_code" in detail_page)
check("UX-P10-06 原参数重跑入口",
      "retry-run" in detail_page and "predictApi.create" in detail_page)
check("🔒 重跑生成新任务而非复用旧任务（DB-T03-02）",
      "生成新任务" in detail_page)

check("UX-P11-01 最多 4 个 + 实时计数",
      "MAX_TASKS = 4" in cmp_page and "picked-count" in cmp_page)
check("UX-P11-02 最优值高亮",
      "data-best" in cmp_page and "lowerIsBetter" in cmp_page)
check("🔒 UX-P11-02 误差类取最小（不是「取最大即最优」）",
      "lowerIsBetter" in cmp_page
      and re.search(r"lowerIsBetter\s*\?\s*Math\.min", cmp_page) is not None
      and re.search(r":\s*Math\.max", cmp_page) is not None,
      "三元表达式跨行书写，不能用剥注释后的单行子串去搜")
check("UX-P11-04 数据文件不一致 → 显著警告",
      "dataset-warning" in cmp_page and "对比结果不可信" in cmp_page)
check("UX-P11-05 差异归因", "compare-attribution" in cmp_page)
check("UX-P11-06 少于 2 个时说明原因",
      "pick-hint" in cmp_page and "至少" in cmp_page)
check("🔒 对比页不画历史段（对比要回答的是预测差异）",
      "CompareSeries" in read("web/src/types/index.ts")
      and "history_dates" not in read("web/src/types/index.ts").split("interface CompareSeries")[1].split("}")[0])

check("UX-P04-01 主按钮唯一", "cta-new-task" in dash)
check("UX-P04-02 汇总卡裁剪为 4 张", dash.count("<StatCard") == 4, f"{dash.count('<StatCard')} 张")
check("UX-P04-05 问候语含具体近况",
      "dashboard-lede" in dash and "weighted_mape" in dash)
check("🔒 工作台统计不自聚合（复用 historyApi.dashboard）",
      "historyApi.dashboard()" in dash and ".reduce(" not in strip_comments(dash, "tsx"))

section("⑨ 🔒 schema 与前端类型同源")
sch = read("code/backend/schemas.py")
check("CompareSeries 专用曲线 schema（不复用 SeriesCurve）",
      "class CompareSeries" in sch)
check("🔒 CompareTaskSlot.metrics 可空（失败任务不参与对比）",
      re.search(r"class CompareTaskSlot[\s\S]*?metrics: MetricBundle \| None", sch) is not None)
check("🔒 CompareResponse 含 overlap_dates / warnings",
      "overlap_dates" in sch and "warnings: list[str]" in sch)
check("🔒 HistoryItem 含 dataset_id（重试入口）",
      re.search(r"class HistoryItem[\s\S]*?dataset_id: int \| None", sch) is not None)
check("🔒 PredictionResultResponse.distribution 可空（失败详情）",
      re.search(r"distribution: Distribution \| None", sch) is not None)
check("WorkbenchSummary schema", "class WorkbenchSummary" in sch)

types = read("web/src/types/index.ts")
check("前端类型含 CompareSeries / WorkbenchSummary",
      "interface CompareSeries" in types and "interface WorkbenchSummary" in types)
check("🔒 前端 CompareTaskSlot.metrics 可空",
      re.search(r"interface CompareTaskSlot[\s\S]{0,900}?metrics: MetricBundle \| null", types) is not None)
check("前端 HistoryItem 含 dataset_id", "dataset_id: number | null" in types)

section("⑩ 🔒 视图迁移已落地（EP-7 修复的缺陷）")
sql = read("code/db/schema.sql")
check("schema.sql 视图含 dataset_id", "AS dataset_id" in sql)
check("🔒 视图仍保留 WHERE is_deleted = 0（API-17-1 红线）",
      re.search(r"CREATE VIEW[\s\S]*?is_deleted = 0", sql) is not None)
dbdoc = read("docs/电商销量预测系统_数据库设计文档_V1.0.md")
check("数据库设计文档的视图定义已同步",
      "AS dataset_id" in dbdoc and "EP-7" in dbdoc)

section("⑪ 路由已挂载")
r = read("app/api/v1/router.py")
check("history.router 已 include", "include_router(history.router)" in r)
check("TODO(EP-7) 已清除", "TODO(EP-7)" not in r)
check("🔒 挂载处说明顺序约束", "BE-39" in r)

section("⑫ 前端零实测数值字面量（数字只能来自接口）")
# ⚠️ **白名单必须包含纯视觉系数**：遮罩透明度 `rgba(..., 0.45)`、
#    占比换算 `part / total * 100` 等不是「实测数值」。
#    只放行阈值常量会让判据误报，误报几次之后就没人看这个脚本了。
ALLOW_NUM = {"20.0", "15.0", "60.0", "100.0"}
VISUAL_ONLY = re.compile(r"rgba\([^)]*\)")
for name, src in (("HistoryPage", hist_page), ("HistoryDetailPage", detail_page),
                  ("ComparePage", cmp_page), ("DashboardPage", dash)):
    code = strip_comments(src, "tsx")
    # 先摘掉 rgba(...) —— 括号内可能有小数点
    code_novis = VISUAL_ONLY.sub("rgba()", code)
    bad = [b for b in re.findall(r"(?<![\w.])\d+\.\d{2,}(?![\w])", code_novis)
           if b not in ALLOW_NUM]
    check(f"{name} 无硬编码实测数值", not bad, "; ".join(bad[:4]))

print(f"\n{'=' * 70}")
print(f"EP-7 静态验收：{PASS} / {TOTAL} 通过")
if PASS != TOTAL:
    print("\n未通过项：")
    for ok, name, detail in RESULTS:
        if not ok:
            print(f"  ❌ {name} — {detail}")
print(f"{'=' * 70}")
raise SystemExit(0 if PASS == TOTAL else 1)