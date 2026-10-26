#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
=============================================================================
`WS-9-09` 校验链全绿 —— 清单守卫 + `DOD-EP9` 交付物守卫
-----------------------------------------------------------------------------
依据文档 : 《项目开发实施方案 V1.0》§7.10 `WS-9-09`
           「校验链全绿：4 套脚本（见 §9.1）」
           `DOD-EP9` 第 6 条「§9.1 的 4 套校验脚本全部通过」
           《开发技术文档 V1.0》§9.1（测试分层）、§9.2（已有资产）、
                              §9.4（B-01~B-25）、§9.7（性能 7 项）
-----------------------------------------------------------------------------
本脚本做两件事，都是**静态**的：

  ① 校验链清单守卫
     · 正表：`CHAIN` 列出 EP-9 必须全绿的每一个脚本；
     · 反表：`scripts/` 下任何 `verify_*` / `a11y_audit` / `self_check`
       若不在 `CHAIN` 里，**失败** —— 防止新增校验脚本后忘跑；
     · 覆盖度：`verify_ep9_api.py` 必须真的含 `B-01`~`B-25` 全部编号；
       `verify_ep9_perf.py` 必须真的含 §9.7 的 7 个测试项。

  ② `DOD-EP9` 交付物守卫
     测试报告 / 性能报告 / 契约报告 / a11y 日志 / 浏览器截图目录，
     以及**测试报告的四个必备章节**（通过用例 / 失败用例 / 修复记录 /
     遗留问题清单）。

⚠️ 为什么本脚本**不亲自实跑**那些校验脚本
-----------------------------------------------------------------------------
    平台的批量删除保护（safety hook）按**单次工具调用的累计删除数**计数
    （阈值 50）。多个校验脚本会各自清理自己的临时目录，若由本脚本用
    `subprocess` 串起来跑，删除量会累积到同一个调用上 → 被判为批量删除
    → 后续脚本直接退出，日志只留一行 `STATUS: FAILED`。
    `EP-9` 首轮就踩过这个坑（`ep5_api` / `ep7_api` / `ep8_api` 三个脚本
    「假失败」，实际是平台的保护机制，不是代码缺陷）。

    因此本脚本只守卫**结构与交付物**；「全绿」的事实由 EP-9 期间
    **逐个单独执行**并落盘日志来证明，结果登记在测试报告第 2 章。
    这是一条刻意的分工，不是遗漏。

运行
-----------------------------------------------------------------------------
    <deploy python> scripts/verify_ep9.py

退出码 0 = 全部通过。
=============================================================================
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
OUTPUTS = ROOT / "outputs"
DOCS = ROOT / "docs"

PASSED = 0
FAILED = 0
ITEMS: list[dict] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    ok = bool(cond)
    if ok:
        PASSED += 1
    else:
        FAILED += 1
    ITEMS.append({"name": name, "ok": ok, "detail": detail})
    print(("[ OK ] " if ok else "[FAIL] ") + name + ("" if ok else f"  {detail}"))
    return ok


def section(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


# ---------------------------------------------------------------------------
# 校验链清单
# ---------------------------------------------------------------------------
# 🔒 正表：EP-9 必须全绿的脚本。第 3 列是「实跑方式」，用于测试报告复现。
#    `log` 字段非空表示 EP-9 期间会把它单独跑一遍并把输出落盘。
CHAIN: list[tuple[str, str, str]] = [
    # ---- 算法层基线（§9.2 / TEST-1：改算法必须重跑且保持 69/69）
    ("self_check.py", "算法层自检 69/69（TEST-1 基线）", "算法"),
    ("eval_report.py", "端到端评估 + 四条硬指标（§9.2）", "算法"),
    # ---- 数据层 / 模型层静态守卫
    ("verify_orm.py", "ORM 实体与 schema.sql 表结构对齐", "静态"),
    ("verify_schema.py", "数据库对象齐备性 + 游客态与外键完整性", "静态"),
    ("verify_schemas.py", "Pydantic 模型、格式约束与 bcrypt 72 字节边界", "静态"),
    ("gen_backend_schemas.py", "R-08 生成件一致性：code/backend/schemas.py ↔ app/schemas/", "静态"),
    # ---- 前端静态守卫
    ("verify_types.mjs", "前端类型语法（node 直跑，不进浏览器）", "前端静态"),
    ("verify_tokens.py", "设计令牌 56 个 / oklch 取值", "前端静态"),
    ("verify_prototype.mjs", "原型页面渲染校验（EP-0 资产，保留用于回归）", "前端静态"),
    # ---- 文档守卫
    ("verify_doc_refs.py", "文档交叉引用（PRD ↔ 技术文档 ↔ 实施方案）", "文档"),
    ("verify_doc_sql.py", "文档内嵌 SQL 与 DDL 一致", "文档"),
    ("verify_timezone.py", "R-11 时区纪律：单时区假设（无 utcnow / DDL 必带 localtime）", "静态"),
    # ---- 认证（API-01~05）
    ("verify_auth.py", "认证与会话：Token 生命周期 / 越权 / 改密撤回", "API"),
    ("verify_register_rate.py", "R-07 注册限流：放行区 / 超限 / 先于 bcrypt 拒绝", "API"),
    # ---- 逐阶段静态守卫
    ("verify_ep3.py", "EP-3 前台首页与全局框架", "静态"),
    ("verify_ep4.py", "EP-4 数据集上传（含注册顺序守卫）", "静态"),
    ("verify_ep5.py", "EP-5 参数页 / 执行页 / 五阶段编排", "静态"),
    ("verify_ep6.py", "EP-6 结果页全要素 / 指标口径", "静态"),
    ("verify_ep7.py", "EP-7 历史与对比 / 工作台（含 BE-39 顺序守卫）", "静态"),
    ("verify_ep8.py", "EP-8 管理端 / 隔离红线 AST 扫描", "静态"),
    # ---- 逐阶段接口守卫
    ("verify_ep4_api.py", "API-06~11 全量回归", "API"),
    ("verify_ep5_api.py", "API-12~13 全量回归", "API"),
    ("verify_ep5_equiv.py", "在线编排与离线算法的一致性（等价性）", "API"),
    ("verify_ep6_api.py", "API-14~16 全量回归", "API"),
    ("verify_ep7_api.py", "API-17~20 全量回归", "API"),
    ("verify_ep8_api.py", "API-22~30 全量回归", "API"),
    # ---- 浏览器走查
    ("verify_ep3_browser.mjs", "EP-3 首页 UX-P01", "浏览器"),
    ("verify_ep4_browser.mjs", "EP-4 上传页 UX-P05", "浏览器"),
    ("verify_ep5_browser.mjs", "EP-5 参数页 / 执行页", "浏览器"),
    ("verify_ep6_browser.mjs", "EP-6 结果页 UX-P08", "浏览器"),
    ("verify_ep7_browser.mjs", "EP-7 历史 / 详情 / 对比 / 工作台", "浏览器"),
    ("verify_ep8_browser.mjs", "EP-8 管理端 AD-P01/P02", "浏览器"),
    # ---- 无障碍（WS-9-07）
    ("a11y_audit.py", "无障碍审计 53 项，退出码 0（UX-AC-01）", "浏览器"),
    # ---- EP-9 本阶段新增四套
    ("verify_ep9_api.py", "§9.4 边界与异常 B-01~B-25", "API"),
    ("verify_ep9_perf.py", "WS-9-01/9-04/9-05：主干路径 / 异常操作 / §9.7 性能 7 项", "性能"),
    ("verify_ep9_browser.mjs", "WS-9-06 兼容性：1366×768 / 1600×900 / 首屏 / 控制台零错误", "浏览器"),
    ("verify_ep9_contract.py", "WS-9-08 契约一致性（四组双向比对）", "契约"),
    ("verify_ep9.py", "本脚本：清单守卫 + DOD-EP9 交付物守卫", "静态"),
    # ---- EP-10 本阶段新增（WS-10-03 接口文档离线化 / WS-10-05·10-06 演示冻结）
    ("verify_docs_browser.mjs", "WS-10-03 /docs 与 /redoc 离线渲染（零外链 / 零加载失败）", "浏览器"),
    ("verify_demo_data.py", "WS-10-05/10-06/10-07 冻结演示数据与失败分支端到端复现", "API"),
    # ---- EP-10 本轮补充（WS-10-08 文档收敛：源 md → 导出件的齐备性 / 新鲜度守卫）
    ("verify_doc_exports.py", "WS-10-08 docs/*.md 与 outputs/ 下 docx·pdf 齐备且未过期", "静态"),
]

CHAIN_NAMES = [c[0] for c in CHAIN]


section("① 校验链清单守卫")

for name, why, kind in CHAIN:
    p = SCRIPTS / name
    check(f"[{kind}] {name} 存在", p.is_file(), f"（缺失：{p}）")

# 反向：scripts/ 下任何校验脚本都必须登记，否则「新写了脚本但没进校验链」会静默溜过。
#
# ⚠️ 两个集合的**口径必须分开**（`EP-9` 首轮的真实缺陷）：
#    · `audited`   = 「算得上校验脚本」的文件 → 服务**反向登记**检查；
#    · `all_names` = `scripts/` 下**全部**文件   → 服务**幽灵条目**检查。
#    首轮把二者混成一个集合，而它按 `verify_` 前缀过滤 —— 于是清单里的
#    `eval_report.py` / `self_check.py` / `a11y_audit.py` 这些**不以 verify_
#    命名**的已有资产被判定为「清单有但磁盘上没有」，一次报出 3 条假失败。
#    教训：一个集合不要同时服务两种口径。
all_names = {p.name for p in SCRIPTS.iterdir() if p.is_file()}
audited = sorted(
    n for n in all_names
    if n.startswith("verify_") or n in {"a11y_audit.py", "self_check.py"}
)
unregistered = [n for n in audited if n not in CHAIN_NAMES]
check(f"scripts/ 下 {len(audited)} 个校验脚本全部已登记（无漏网）",
      not unregistered, f"（未登记：{unregistered}）")
missing_on_disk = [n for n in CHAIN_NAMES if n not in all_names]
check("清单中的脚本都在磁盘上（无幽灵条目）",
      not missing_on_disk, f"（清单有但不存在：{missing_on_disk}）")


section("② §9.4 / §9.7 覆盖度守卫")

ep9_api_src = (SCRIPTS / "verify_ep9_api.py").read_text(encoding="utf-8")
missing_b = [f"B-{i:02d}" for i in range(1, 26) if f"B-{i:02d}" not in ep9_api_src]
check("verify_ep9_api.py 含 §9.4 全部 B-01~B-25 编号（无漏测）",
      not missing_b, f"（缺：{missing_b}）")

doc_tech = (DOCS / "电商销量预测系统_开发技术文档_V1.0.md").read_text(encoding="utf-8")
doc_missing_b = [f"B-{i:02d}" for i in range(1, 26) if f"B-{i:02d}" not in doc_tech]
check("技术文档 §9.4 表格含全部 B-01~B-25（脚本与文档同步）",
      not doc_missing_b, f"（缺：{doc_missing_b}）")

perf_src = (SCRIPTS / "verify_ep9_perf.py").read_text(encoding="utf-8")
PERF_ITEMS = [
    ("端到端耗时", "§9.7-1 示例数据 ≤ 60 s"),
    ("极限规模", "§9.7-2 1000 序列 × horizon=30"),
    ("并发", "§9.7-3 3 任务同时提交"),
    ("首屏", "§9.7-4 ≤ 3 s"),
    ("预览接口", "§9.7-5 ≤ 1 s"),
    ("历史列表", "§9.7-6 ≤ 1 s"),
    ("CPU", "§9.7-7 强制 CPU 回退 ≤ 60 s"),
]
missing_perf = [why for kw, why in PERF_ITEMS if kw not in perf_src]
check("verify_ep9_perf.py 覆盖 §9.7 全部 7 项",
      not missing_perf, f"（缺：{missing_perf}）")

# ⚠️ 用「整格匹配」`| 词 |` 而不是「行首 + 词 + 空格」：后者在 `EP-9` 首轮
#    意外匹配到 0 行，而把实际匹配结果放进 detail 才看清是解析式写窄了。
#    教训：**断言失败时必须能一眼看出「实际值是多少」** ——
#    只有 `== 7` 而没有实际值的断言，失败时等于没给线索。
_perf_rows = re.findall(
    r"^\|\s*(端到端耗时|极限规模|并发|首屏|预览接口|历史列表|GPU 回退)\s*\|",
    doc_tech, re.M)
check("技术文档 §9.7 表格仍为 7 行（性能基线未被改动）",
      len(_perf_rows) == 7,
      f"（实际匹配 {len(_perf_rows)} 行：{_perf_rows}）")


section("③ DOD-EP9 交付物守卫")

DELIVERABLES = [
    (OUTPUTS / "ep9_perf_report.json", "性能与集成实测数据（WS-9-01/9-04/9-05）"),
    (OUTPUTS / "ep9_contract_report.json", "契约一致性报告（WS-9-08）"),
    (OUTPUTS / "ep9_a11y_log.txt", "无障碍审计落盘日志（WS-9-07 退出码 0 的证据）"),
    (OUTPUTS / "ep9-shots", "兼容性走查截图目录（WS-9-06）"),
    (DOCS / "电商销量预测系统_测试报告_V1.0.md", "EP-9 核心交付物：测试报告"),
]
for p, why in DELIVERABLES:
    check(f"交付物存在：{p.name}（{why}）", p.exists(), f"（缺：{p}）")

report_path = DOCS / "电商销量预测系统_测试报告_V1.0.md"
if report_path.is_file():
    rep = report_path.read_text(encoding="utf-8")
    for kw in ("通过用例", "失败用例", "修复记录", "遗留问题"):
        check(f"测试报告含必备章节「{kw}」（DOD-EP9 第 7 条）", kw in rep)
else:
    check("测试报告含必备四章节（报告尚未生成，跳过内容检查）", False,
          "（报告未生成 —— EP-9 收口前必须补齐）")

check("前端可构建（web/dist/index.html 存在，WS-9-06 走查前置）",
      (ROOT / "web" / "dist" / "index.html").is_file())
pkg = json.loads((ROOT / "web" / "package.json").read_text(encoding="utf-8"))
check("前端提供 typecheck 脚本（§11.1 提交前自检）",
      "typecheck" in pkg.get("scripts", {}))


section("汇总")
print(f"通过 {PASSED} 项，失败 {FAILED} 项")

OUTPUTS.mkdir(parents=True, exist_ok=True)
out = OUTPUTS / "ep9_chain_report.json"
out.write_text(json.dumps({
    "stage": "WS-9-09 校验链全绿（清单守卫）",
    "passed": PASSED,
    "failed": FAILED,
    "chain_size": len(CHAIN),
    "items": ITEMS,
}, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"报告已写入：{out}")
print("结论：全部通过" if FAILED == 0 else "结论：存在未通过项")
sys.exit(0 if FAILED == 0 else 1)
