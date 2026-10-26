"""
=============================================================================
`EP-6` 静态验收（`DOD-EP6` 的结构与纪律部分）
-----------------------------------------------------------------------------
版本     : V1.0（EP-6）
依据文档 : 《项目开发实施方案 V1.0》§7.7（`WS-6-01` ~ `WS-6-09`）
           《开发技术文档 V1.0》§5.6（`API-14` ~ `API-16`）
           《UI/UX 设计文档 V1.0》§8.4（`UX-P08-01` ~ `UX-P08-13`）

🔒 **静态验收要抓的是「纪律」，不是「功能」**
   功能对不对由 `verify_ep6_api.py`（真接口）与 `verify_ep6_browser.mjs`
   （真渲染）负责。本脚本抓的是那些**跑起来也看不出来、但一改就悄悄漂移**的
   结构性约定 —— 典型如：
     · 导出列序被谁改动了（跑一次看不出来，用户打开 Excel 才发现）
     · 结果服务里混进了 `round()`（数值看起来差不多，但 DOD 要求逐项一致）
     · 排序列白名单被绕过（前端能点一个后端不认的列，点了没反应）
     · 前端自己算了一遍指标（那就是「两套数字」的前车之鉴）

用法：在**工作区根目录**执行
    C:/ProgramData/anaconda3/envs/deploy/python.exe scripts/verify_ep6.py
=============================================================================
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = 0
TOTAL = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASS, TOTAL
    TOTAL += 1
    if ok:
        PASS += 1
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def section(t: str) -> None:
    print(f"\n{'=' * 70}\n{t}\n{'=' * 70}")


def read(rel: str) -> str:
    p = ROOT / rel
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def strip_docstrings(src: str) -> str:
    """剥离 docstring 与注释，只留可执行代码。

    🔒 这是判「有没有做某个操作」的前提：`_metric_bundle` 的 docstring 里
       写着「接口层一旦 `round()`…就…」，那是**解释为什么不这么做**，
       却被朴素的子串搜索判成了「做了 round()」。同理 `_coverage` 的注释里
       提到 `count_by_level`（说明为什么不用它）也会被误判。

    ⚠️ 必须同时处理 **JS/TS 的块注释**（斜杠星号 … 星号斜杠）——
       前端源文件普遍用 JSDoc 写模块头说明（`ResultPage.tsx` 就在注释里写了
       「与 outputs/eval_report.json 同源」）。只剥三引号与井号的话，
       前端文件等于没剥，判据必然误报。
    """
    out = re.sub(r"/\*[\s\S]*?\*/", "", src)             # JS/TS 块注释
    out = re.sub(r'"""[\s\S]*?"""', "", out)             # docstring
    out = re.sub(r"'''[\s\S]*?'''", "", out)
    out = re.sub(r"(?m)^\s*#.*$", "", out)                # 整行注释
    out = re.sub(r"\s+//.*$", "", out, flags=re.M)        # 行尾 //
    out = re.sub(r"\s+#.*$", "", out, flags=re.M)         # 行尾 #
    return out


# =============================================================================
section("① WS-6-05~07 后端：三个接口已实现且契约齐全")
# =============================================================================
routes = read("app/api/v1/predictions.py")
routes_code = strip_docstrings(routes)   # 判「有没有做某操作」前先剥 docstring（§七.2）
check("API-14 路由 GET /{task_id}", '@router.get(\n    "/{task_id}",' in routes)
check("API-15 路由 /{task_id}/detail", '"/{task_id}/detail"' in routes)
check("API-16 路由 /{task_id}/export", '"/{task_id}/export"' in routes)
check("API-14 用 OptionalUserId（游客可看结果）",
      "user_id: OptionalUserId" in routes)
check("🔒 API-16 用 CurrentUser（导出必须登录）",
      re.search(r"def prediction_export[\s\S]{0,400}?user: CurrentUser", routes) is not None)
check("API-15 明细独立成接口（不并入 API-14）",
      'Paginated[PredictionRow]' in routes)
check("🔒 API-16 文件名双写 filename*=UTF-8''",
      "filename*=UTF-8''" in routes_code and "quote(" in routes_code,
      # 原判据是 `"quote(name" in routes`：既**依赖变量名**（实现里叫 `quoted`，
      # 改名即误报），又**没剥 docstring**（本函数上方那段说明里也有
      # `filename*=UTF-8''`，注释单独就能让断言通过 → 假阳性）。
      # 现改为：先剥 docstring，再要求「编码函数 + RFC 5987 写法」同时出现在**代码**里。
      "期望代码中出现 quote(...) 与 filename*=UTF-8''（docstring 不计）")
check("API-16 响应 no-store（明细数据不缓存）",
      '"Cache-Control": "no-store"' in routes)

svc = read("app/services/result_service.py")
check("result_service 模块存在", bool(svc), f"{len(svc)} 字符")
check("WS-6-05 结果接口含全部区块",
      all(k in svc for k in ("_metric_bundle", "_coverage", "_distribution", "_timing_ms", "_series_curves")))
check("WS-6-06 明细分页独立实现", "def list_detail(" in svc)
check("WS-6-07 导出固定 7 列",
      "EXPORT_HEADER" in svc and svc.count('"granularity_level"') >= 1)

def func_body(src: str, name: str) -> str:
    """精确提取顶层函数体（从 `def name(` 到下一个顶层 `def`）。

    ⚠️ 不能用 `split("def _x")[1].split("def _y")[0]` ——
       函数体里的注释/docstring 经常**包含下一个函数的名字**
       （例如 `_coverage` 的注释里写「见 `_distribution`」），
       切出来的片段会跨到别的函数去，判据随之失真。
    """
    m = re.search(rf"^def {re.escape(name)}\(", src, re.M)
    if not m:
        return ""
    nxt = re.search(r"^def ", src[m.end():], re.M)
    return src[m.start(): m.end() + (nxt.start() if nxt else len(src))]




# =============================================================================
section("② WS-6-01~04 指标纪律（🔒 最要紧的一组）")
# =============================================================================
# ⚠️ 判据要点：`result_service` 里**允许**在函数内部 import `app.algo.config`
#    取列名常量（sku_id / date / sales），那是元数据不是算法。
#    真正要禁的是**模块顶层** import algo —— 那会让接口层长出算法依赖。
_top_level_algo = re.search(r"^from app\.algo", svc, re.M) is not None
check("🔒 结果服务顶层不 import algo（接口层不依赖算法层）",
      not _top_level_algo,
      "函数内 import config 取列名常量是允许的")

_mb_code = strip_docstrings(func_body(svc, "_metric_bundle"))
check("🔒 _metric_bundle 不做 round（逐项一致 DOD）",
      "round(" not in _mb_code)
check("🔒 主指标只统计 L1 的口径在案",
      "l1_mape" in _mb_code and "l2_mape" in _mb_code,
      "L1/L2 分组指标分别透出，不混算")
_cov_code = strip_docstrings(func_body(svc, "_coverage"))
check("🔒 覆盖率以 tasks 冗余列为准（而非明细行数）",
      "count_by_level" not in _cov_code and "l1_count" in _cov_code,
      "明细行数 = 序列数 × horizon，直接用会放大 horizon 倍")
check("🔒 NaN → None（API-14-2，避免非法 JSON）",
      "math.isnan" in svc and "def _num(" in svc)
check("🔒 明细无真值时 error 为 null 而非 0",
      "None if actual is None" in svc)
check("🔒 导出无真值时写空串而非 0",
      '"" if actual is None' in svc)
check("🔒 曲线 actual 恒为 null（在线预测真值未发生）",
      '"actual": None' in svc)
check("🔒 L3 不补齐曲线（不均值伪装）",
      "补齐" in svc)

dao = read("app/dao/prediction_dao.py")
check("🔒 排序列有白名单（防注入 + 防静默退化）",
      "SORTABLE_COLUMNS" in dao)
check("🔒 分页与计数共用同一过滤条件",
      "_filtered(" in dao and dao.count("_filtered(") >= 2)
check("🔒 分页追加稳定次级排序键（防跨页重复/遗漏）",
      "Prediction.id.asc()" in dao)
check("🔒 导出用 yield_per（不物化全量）",
      "yield_per" in dao)

# =============================================================================
section("③ API-14 / 15 / 16 契约（schemas）")
# =============================================================================
sch = read("code/backend/schemas.py")
check("PredictionResultResponse 含 truncated（API-14-3）",
      re.search(r"class PredictionResultResponse[\s\S]{0,1200}?truncated", sch) is not None)
check("DetailQuery 存在（API-15）", "class DetailQuery" in sch)
check("🔒 DetailQuery 默认每页 20 行", "size: int = Field(20" in sch)
check("🔒 DetailQuery size 上限 100", "le=100" in sch)
check("🔒 排序列在 schema 层再校验（与 DAO 白名单同源）",
      "DETAIL_SORT_COLUMNS" in sch and "_check_sort" in sch)
check("ExportQuery 限制 csv/xlsx",
      "class ExportQuery" in sch and 'pattern="^(csv|xlsx)$"' in sch)
check("导出列序在 schemas 与前端类型中成对存在",
      "EXPORT_COLUMNS" in read("web/src/types/index.ts")
      and "EXPORT_HEADER" in svc,
      "列序真源在后端 EXPORT_HEADER，前端 EXPORT_COLUMNS 仅用于文案提示")
check("🔒 导出列序为 7 列且含 granularity_level",
      re.search(r"EXPORT_HEADER[^=]*=\s*\((?:[^)]*)\)", svc) is not None
      and '"granularity_level"' in svc
      and '"forecast_sales"' in svc and '"actual_sales"' in svc)

# =============================================================================
section("④ UX-P08 结果页全要素")
# =============================================================================
page = read("web/src/pages/ResultPage.tsx")
panels = read("web/src/components/result/MetricPanels.tsx")
cover = read("web/src/components/result/CoveragePanel.tsx")
line = read("web/src/components/result/LineCompare.tsx")
dist = read("web/src/components/result/DistributionChart.tsx")
detail = read("web/src/components/result/DetailTable.tsx")
export_ui = read("web/src/components/result/ExportButtons.tsx")

# ⚠️ 必须比 **JSX 里的使用位置**，不是 import 行 —— import 语句必然在最前，
#    用 index 比较会永远判「结论条在前」而误判通过。
_v_use = page.find("<VerdictBar")
_m_use = page.find("<MetricCards")
check("UX-P08-01 结论条在第一张卡（JSX 位置）",
      _v_use > 0 and _m_use > 0 and _v_use < _m_use,
      f"VerdictBar@{_v_use} < MetricCards@{_m_use}")
check("UX-P08-02 指标卡含「主口径」徽标", "主口径" in panels)
check("UX-P08-02 明示评估对象（L1 / 预测点）", "评估对象" in panels)
check("UX-P08-03 基线表含判定列", "判定" in panels and "BaselineTable" in page)
check("UX-P08-03 声明基线同口径", "完全相同的预测点" in panels)
check("UX-P08-04 覆盖率面板必展示", "CoveragePanel" in page)
check("UX-AC-06 前端再校验三项之和", "coverageBroken" in cover or "不一致" in cover)
check("UX-P08-05 折线三线 + 预测起点标注",
      all(k in line for k in ("历史销量", "模型预测", "移动平均基线", "预测起点")))
check("UX-P08-05 历史实线 / 预测虚线",
      "type: 'dashed'" in line)
check("UX-P08-06 L3 预测段留空", "L3" in line and "isL3" in line)
check("UX-P08-07 明细每行标注分级与置信度",
      "granularity_level" in detail and "CONFIDENCE" in detail)
check("UX-P08-08 导出列序提示与契约一致",
      "商品 / 仓库 / 日期 / 预测销量 / 分级 / 实际销量 / 误差" in export_ui)
check("UX-P08-09 直方图分箱 ≤ 20", "BUCKET_SIZE" in dist)
check("UX-P08-10 评估前提原样展示",
      "eval-note" in page and "eval_note" in page)
check("UX-P08-12 空态说明半成品不落盘",
      "不落盘半成品" in page and "empty-rerun" in page)
# ⚠️ **判据缺陷修正**：原断言在**含注释的原文**上找 `<details`，而本页的注释里恰好
#    写着「刻意不用 <details>：可折叠 = 用户可以视而不见」—— 于是「解释为什么不用」
#    被读成了「用了」，断言必然失败（而功能其实是对的）。
#    📌 与「先 strip_docstrings 再判有没有做某操作」是同一条纪律：**注释不是代码**。
#    改法：① 先剥 JSX 注释；② 用**代码锚点**（`data.is_over_threshold &&`）定位告警块，
#    不再依赖只出现在注释里的「超标警告」四个字。
_page_code = re.sub(r"\{/\*.*?\*/\}", "", page, flags=re.S)
_page_code = re.sub(r"//[^\n]*", "", _page_code)
_alert_seg = re.split(r"</NoticeBar>|/>", _page_code.split("is_over_threshold &&")[-1])[0]
check("🔒 FR-VIS-06 超标警告不可折叠（告警块内无 <details>）",
      "is_over_threshold" in _page_code and "<details" not in _alert_seg,
      "告警块含 <details>" if "<details" in _alert_seg else "常驻展示，不可折叠")
check("UX-GOV-10 图表无障碍替代（数据表）",
      "line-data-table" in line and "无障碍替代" in line)
check("🔒 游客导出禁用而非隐藏", "disabled={busy !== null || !isAuthenticated}" in export_ui)

# =============================================================================
section("⑤ 🔒 前端不得自行计算指标（UX-P08-13）")
# =============================================================================
result_dir = ROOT / "web" / "src" / "components" / "result"
bad_calc = []
for f in sorted(result_dir.glob("*.tsx")):
    txt = f.read_text(encoding="utf-8")
    # ⚠️ 判据只针对「指标」：禁止对 metrics 字段做二次聚合。
    #    `DistributionChart` 里的 `buckets.reduce(...)` 是**直方图总数**
    #    （把 20 个分箱的计数加起来当分母），属展示层必需，不是指标重算 ——
    #    早期版本把它一并禁掉，结果是误报。故这里精确匹配 metrics 字段名。
    for field in ("weighted_mape", "weighted_mae", "weighted_rmse",
                  "mae_improve_pct", "rmse_improve_pct", "series_pass_rate"):
        if re.search(rf"{field}[^;\n]{{0,60}}(reduce|Math\.avg)", txt):
            bad_calc.append(f"{f.name}: {field} 被二次聚合")
    if re.search(r"\.reduce\([^)]*\)[^;\n]{0,40}(metrics|weighted)", txt):
        bad_calc.append(f"{f.name}: reduce 作用于 metrics")
check("🔒 结果组件内无指标二次聚合计算", not bad_calc, "; ".join(bad_calc))
check("直方图总数为分箱计数之和（展示层，允许）",
      "buckets.reduce" in dist, "不是指标重算")

check("🔒 前端阈值集中为常量（THRESHOLDS）",
      "THRESHOLDS" in read("web/src/types/index.ts"))
check("🔒 阈值与后端 models/metric.py 同源命名",
      "weighted_mape_max" in read("web/src/types/index.ts")
      and "THRESHOLD_MAPE" in read("app/models/metric.py"))

# =============================================================================
section("⑥ 结果页数据来源单一（不得绕过 API-14）")
# =============================================================================
check("结果页只经 predictApi 取数（result + detail）",
      "predictApi" in page and ".result(" in page and ".detail(" in page,
      "不得直接 fetch 或读本地文件")
# ⚠️ 必须在**剥离注释后**的代码上判：`ResultPage.tsx` 的模块头注释里
#    写着「与 outputs/eval_report.json 同源」—— 那是在**说明要求**，
#    朴素子串搜索会把它当成「页面读了本地文件」而误报。
check("🔒 结果页不读取本地算法产物文件",
      "eval_report" not in strip_docstrings(page)
      and "readFileSync" not in page
      and not re.search(r"fetch\(\s*['\"][^'\"]*\.json", page))
check("🔒 明细走服务端分页（不在前端全量排序）",
      "fetcher(" in detail and "sort_by" in detail)

# =============================================================================
section("⑦ 路由与游客链路")
# =============================================================================
router = read("web/src/router/index.tsx")
check("/result/:taskId 已挂载", "/result/:taskId" in router)
# ⚠️ 不能用 `\{[^{}]*\}` 匹配 —— 路由元素对象里含 `{ component: ... }`，
#    会让 [^{}]* 立刻失配（EP-5 脚本的同类正则陷阱）。改为取该行附近的片段。
_r_idx = router.find("path: '/result/:taskId'")
_r_ctx = router[_r_idx: _r_idx + 260] if _r_idx >= 0 else ""
check("🔒 结果页不加 RequireAuth（游客可看）",
      _r_idx >= 0 and "RequireAuth" not in _r_ctx,
      _r_ctx.replace("\n", " ").strip()[:90])

# =============================================================================
section("⑧ 校验脚本自身齐备")
# =============================================================================
api_script = read("scripts/verify_ep6_api.py")
check("verify_ep6_api.py 存在", bool(api_script))
if api_script:
    check("API 校验覆盖「指标与 eval_report 逐项一致」",
          "eval_report" in api_script or "weighted_mape" in api_script)
    check("API 校验覆盖导出 BOM", "utf-8-sig" in api_script or "BOM" in api_script)
    check("API 校验覆盖 L3 稀疏序列", "L3" in api_script)
check("verify_ep6_browser.mjs 存在", bool(read("scripts/verify_ep6_browser.mjs")))

# =============================================================================
print(f"\n{'=' * 70}")
print(f"EP-6 静态验收：{PASS} / {TOTAL} 通过")
if PASS != TOTAL:
    print("\n未通过项：")
print(f"{'=' * 70}")
sys.exit(0 if PASS == TOTAL else 1)
