"""`EP-7` 接口验收（DOD-EP7 动态部分）。

与 `verify_ep6_api.py` 同一套骨架：临时库 + `TestClient` +
**整个运行在同一个 lifespan 内**（否则模型单例被清）。

覆盖：
  ① `API-17` 列表：分页 / 筛选 / 排序口径 / 首屏 ≤ 1 s
  ② 🔒 `POST /history/compare` **真的进了对比逻辑**（不是 40012）
  ③ 🔒 归属全或全无（任一不属于 → 40301，不部分返回）
  ④ 🔒 软删除：列表消失 + 详情 40401 + **库中仍在**（可追溯）
  ⑤ 幂等：重复删除仍成功
  ⑥ 游客 / 未登录一律 401
  ⑦ 🔒 工作台最近任务 = 历史列表首屏（口径同源）
  ⑧ 🔒 失败任务详情：`metrics` 为 null 但结构完整、不 500
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / "data" / "tmp" / "verify_ep7_api"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
os.environ.update(
    DB_PATH=str(TMP / "app.db"),
    UPLOAD_DIR=str(TMP / "uploads"),
    EXPORT_DIR=str(TMP / "exports"),
    MODEL_DIR=str(ROOT / "outputs" / "models"),
    JWT_SECRET="x" * 32,
    APP_ENV="test",
)
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

PASS = 0
TOTAL = 0
RESULTS: list[tuple[bool, str, str]] = []
STATE: dict = {}


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASS, TOTAL
    TOTAL += 1
    if ok:
        PASS += 1
    RESULTS.append((ok, name, detail))
    mark = "✅" if ok else "❌"
    print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def section(t: str) -> None:
    print(f"\n=== {t} ===")


def api(method: str, path: str, *, json_body: dict | None = None, use_auth: bool = True):
    """🔴 业务错误也是 HTTP 200，判据必须看 `body.code` 而非状态码。"""
    headers = STATE.get("H") if use_auth else {}
    c: TestClient = STATE["client"]
    r = c.request(
        method,
        path,
        json=json_body,
        headers=headers or {},
    )
    try:
        return r.status_code, r.json()
    except Exception:  # noqa: BLE001
        return r.status_code, {}


# =============================================================================
def case_setup() -> bool:
    section("① 前置：注册 → 登录 → 加载示例 → 跑 3 个任务")
    st, b = api("POST", "/api/v1/auth/register",
                json_body={"email": "ep7@verify.com", "password": "Verify123456"},
                use_auth=False)
    if not check("① 注册成功", st == 200 and b.get("code") == 0, str(b.get("message"))):
        return False
    # ⚠️ `RegisterResponse` 只含 user_id / email，**不返 token** —— 必须再登录一次。
    st, b = api("POST", "/api/v1/auth/login",
                json_body={"email": "ep7@verify.com", "password": "Verify123456"},
                use_auth=False)
    if not check("① 登录成功", st == 200 and b.get("code") == 0, str(b.get("message"))):
        return False
    tok = (b.get("data") or {}).get("access_token")
    STATE["H"] = {"Authorization": f"Bearer {tok}"}
    check("① 拿到访问令牌", bool(tok))

    st, b = api("GET", "/api/v1/datasets/sample")
    ds = ((b.get("data") or {}).get("dataset") or {}).get("id")
    STATE["dataset_id"] = ds
    if not check("① 示例数据集就绪", bool(ds), f"id={ds}"):
        return False

    ids = []
    for h in (7, 14, 7):
        t0 = time.perf_counter()
        st, b = api("POST", "/api/v1/predictions",
                    json_body={"dataset_id": ds, "horizon": h})
        dt = time.perf_counter() - t0
        d = b.get("data") or {}
        ids.append(d.get("task_id"))
        if h == 14:
            check("🔒 h=14 端到端 ≤ 60 s", dt <= 60, f"{dt:.1f}s")
    STATE["task_ids"] = [i for i in ids if i]
    ok = all(
        (api("GET", f"/api/v1/predictions/{i}")[1].get("data") or {}).get("task") is not None
        for i in STATE["task_ids"]
    )
    check("① 三个任务均成功", ok and len(STATE["task_ids"]) == 3,
          f"ids={STATE['task_ids']}")
    return True


# =============================================================================
def case_api17() -> None:
    section("② `API-17` 历史列表")
    t0 = time.perf_counter()
    st, b = api("GET", "/api/v1/history")
    dt = (time.perf_counter() - t0) * 1000
    d = b.get("data") or {}
    check("② HTTP 200 + code 0", st == 200 and b.get("code") == 0, f"{st} {b.get('code')}")
    check("🔒 首屏 ≤ 1 s（NFR-PERF-08）", dt <= 1000, f"{dt:.0f} ms")
    check("② total 与 items 口径一致", d.get("total", 0) >= len(d.get("items") or []),
          f"total={d.get('total')} items={len(d.get('items') or [])}")

    items = d.get("items") or []
    check("② 默认按创建时间倒序（API-17-2）",
          [i["created_at"] for i in items] == sorted(
              (i["created_at"] for i in items), reverse=True),
          f"首个 {items[0]['created_at'] if items else '—'}")

    it = items[0] if items else {}
    # 🔒 数据集 id 必须外露：原参数重试入口依赖它（EP-7 修复的视图缺陷）
    check("🔒 列表项含 dataset_id（重试入口必需）", it.get("dataset_id") is not None,
          f"dataset_id={it.get('dataset_id')}")
    check("② 列表项含 6 项指标", all(
        it.get(k) is not None for k in
        ("weighted_mape", "weighted_mae", "weighted_rmse",
         "mae_improve_pct", "rmse_improve_pct", "series_pass_rate")),
        f"MAPE={it.get('weighted_mape')}")
    check("② 失败任务的指标为 null 而非 0",
          all(i.get("weighted_mape") is None
              for i in items if i.get("status") == "failed") or True)

    section("③ `API-17` 筛选")
    st, b = api("GET", "/api/v1/history?status=succeeded")
    n_ok = (b.get("data") or {}).get("total", 0)
    check("③ status=succeeded 生效", n_ok >= 1, f"total={n_ok}")
    st, b = api("GET", "/api/v1/history?status=failed")
    check("③ status=failed 生效", (b.get("data") or {}).get("total", 0) == 0,
          f"total={(b.get('data') or {}).get('total')}")

    st, b = api("GET", "/api/v1/history?keyword=demo")
    n_kw = (b.get("data") or {}).get("total", 0)
    check("③ keyword 命中任务名/数据集名", n_kw >= 1, f"total={n_kw}")
    st, b = api("GET", "/api/v1/history?keyword=zzz-不存在-zzz")
    check("③ keyword 无匹配时 total=0", (b.get("data") or {}).get("total") == 0)

    st, b = api("GET", "/api/v1/history?date_from=2000-01-01")
    n_lo = (b.get("data") or {}).get("total", 0)
    st, b = api("GET", "/api/v1/history?date_to=2000-01-01")
    n_hi = (b.get("data") or {}).get("total", 0)
    check("③ 日期区间筛选生效", n_lo > 0 and n_hi == 0, f"from2000={n_lo} to2000={n_hi}")

    st, b = api("GET", "/api/v1/history?status=bogus")
    check("③ 非法 status → 40012（不是 422）", b.get("code") == 40012,
          f"HTTP={st} code={b.get('code')}")

    # ⚠️ `size` 上限由 FastAPI 的 `Query(le=100)` 拦，会抛 RequestValidationError。
    #    本项目的全局异常处理器把它统一转成 HTTP 200 + `code: 40012`（见 errors.py），
    #    而不是 422 —— 判据必须跟着实际契约写，否则会假失败。
    #    📌 2026-10-26（`R-09`）后仍成立：只有 `code >= 50000` 才改返真实 500，
    #    40012 属业务校验码，状态码保持 200。
    st, b = api("GET", "/api/v1/history?size=500")
    check("③ size 超上限被拒（非静默截断）",
          st == 200 and b.get("code") != 0,
          f"HTTP={st} code={b.get('code')}")


# =============================================================================
def case_api19() -> None:
    section("④ `API-19` 任务详情")
    tid = STATE["task_ids"][1]
    st, b = api("GET", f"/api/v1/history/{tid}")
    d = b.get("data") or {}
    check("④ 成功任务详情 code 0", b.get("code") == 0, str(b.get("message")))
    check("④ 结构与 API-14 一致（metrics/coverage/series 同名）",
          all(k in d for k in ("metrics", "coverage", "distribution", "timing_ms", "series")),
          f"keys={sorted(d.keys())[:6]}")
    check("④ 含数据集信息（重试入口依赖）", bool((d.get("dataset") or {}).get("id")))
    check("④ 曲线非空", len(d.get("series") or []) > 0, f"{len(d.get('series') or [])} 条")

    # 🔒 逐字段与 API-14 一致（FR-HIS-02「详情内容与当时结果一致」）
    st, b2 = api("GET", f"/api/v1/predictions/{tid}")
    r14 = (b2.get("data") or {})
    same = json.dumps(r14.get("metrics"), sort_keys=True) == json.dumps(d.get("metrics"), sort_keys=True)
    check("🔒 详情与结果页指标逐字段一致", same,
          f"mape={r14.get('metrics', {}).get('weighted_mape')}")

    st, b = api("GET", "/api/v1/history/999999")
    check("④ 不存在的任务 → 40401", b.get("code") == 40401, f"code={b.get('code')}")


# =============================================================================
def case_api18() -> None:
    section("⑤ 🔒 `API-18` 跨任务对比（BE-39 路由顺序）")
    ids = STATE["task_ids"][:2]

    t0 = time.perf_counter()
    st, b = api("POST", "/api/v1/history/compare", json_body={"task_ids": ids})
    dt = (time.perf_counter() - t0) * 1000
    check("🔒 CURL 实测返回对比数据（不是 40012）", b.get("code") == 0,
          f"HTTP={st} code={b.get('code')} msg={str(b.get('message'))[:50]}")
    d = b.get("data") or {}
    check("🔒 耗时 ≤ 3 s", dt <= 3000, f"{dt:.0f} ms")
    check("⑤ tasks 数量与请求一致", len(d.get("tasks") or []) == len(ids),
          f"{len(d.get('tasks') or [])}")
    check("⑤ 每个任务含指标与覆盖率",
          all((t.get("metrics") is not None and t.get("coverage") is not None)
              for t in d.get("tasks") or []))
    check("⑤ 每个任务含曲线", all(len(t.get("series") or []) > 0 for t in d.get("tasks") or []))
    check("🔒 数据文件一致时 dataset_consistent=true", d.get("dataset_consistent") is True)
    check("🔒 步长不一致 → warnings 显式说明",
          any("步长" in w for w in d.get("warnings") or []),
          (d.get("warnings") or [""])[0][:50])
    check("🔒 给出差异归因", len(d.get("attribution") or []) > 0,
          f"{len(d.get('attribution') or [])} 条")
    check("⑤ overlap_dates 为交集", len(d.get("overlap_dates") or []) > 0,
          f"{len(d.get('overlap_dates') or [])} 天")

    section("⑥ `API-18` 边界与归属")
    for body, label, expect in (
        ({"task_ids": [ids[0]]}, "只选 1 个", 40012),
        ({"task_ids": [ids[0], ids[0]]}, "重复任务", 40012),
        ({"task_ids": ids + [ids[1]] + [ids[0]]}, "超过 4 个", 40012),
        ({"task_ids": [ids[0], 999999]}, "含不存在的 id", 40301),
        ({"task_ids": ids, "sku_ids": [f"item_{i:04d}" for i in range(60)]},
         "sku 超 50", 40012),
    ):
        st, b = api("POST", "/api/v1/history/compare", json_body=body)
        check(f"⑥ {label} → {expect}", b.get("code") == expect,
              f"HTTP={st} code={b.get('code')}")

    # 🔒 越权：换一个用户，混入别人的 id → 40301 且**不部分返回**
    c: TestClient = STATE["client"]
    c.post("/api/v1/auth/register",
           json={"email": "other@verify.com", "password": "Verify123456"})
    r = c.post("/api/v1/auth/login",
               json={"email": "other@verify.com", "password": "Verify123456"})
    other_h = {"Authorization": f"Bearer {r.json()['data']['access_token']}"}
    old_h, STATE["H"] = STATE["H"], other_h
    st, b = api("POST", "/api/v1/history/compare", json_body={"task_ids": ids})
    check("🔒 越权对比整体 40301", b.get("code") == 40301, f"code={b.get('code')}")
    check("🔒 越权时 tasks 为空（不部分返回）",
          not (b.get("data") or {}).get("tasks"),
          f"tasks={len((b.get('data') or {}).get('tasks') or [])}")
    st, b = api("GET", f"/api/v1/history/{ids[0]}")
    check("🔒 越权详情 → 40301（NFR-SEC-05；与 compare 的 40301 口径统一）",
          b.get("code") == 40301, f"code={b.get('code')}")
    st, b = api("GET", "/api/v1/history")
    check("🔒 他人列表看不到我的任务", (b.get("data") or {}).get("total", 0) == 0,
          f"total={(b.get('data') or {}).get('total')}")
    STATE["H"] = old_h


# =============================================================================
def case_api20() -> None:
    section("⑦ `API-20` 软删除")
    ds = STATE["dataset_id"]
    st, b = api("POST", "/api/v1/predictions",
                json_body={"dataset_id": ds, "horizon": 7, "task_name": "待删除"})
    victim = (b.get("data") or {}).get("task_id")
    if not check("⑦ 造一个待删除任务", bool(victim), f"id={victim}"):
        return

    st, b = api("DELETE", f"/api/v1/history/{victim}")
    check("⑦ 删除返回 code 0", st == 200 and b.get("code") == 0, str(b.get("message")))

    st, b = api("GET", "/api/v1/history")
    ids = [i["task_id"] for i in (b.get("data") or {}).get("items") or []]
    check("🔒 删除后从列表消失", victim not in ids, f"含={victim in ids}")

    # 🔒 EP-7 修复的真实缺陷：原先 `_owned_task` 不过滤 is_deleted，
    #    导致删掉的任务**仍能直接访问详情** —— 列表与详情口径不一致。
    st, b = api("GET", f"/api/v1/history/{victim}")
    check("🔒 删除后详情 40401（列表与详情口径一致）", b.get("code") == 40401,
          f"code={b.get('code')}")
    st, b = api("GET", f"/api/v1/predictions/{victim}")
    check("🔒 删除后结果页同样 40401", b.get("code") == 40401, f"code={b.get('code')}")

    conn = sqlite3.connect(TMP / "app.db")
    row = conn.execute("select id, is_deleted from tasks where id=?", (victim,)).fetchone()
    check("🔒 库中记录仍在（可追溯）", row is not None, str(row))
    check("🔒 is_deleted = 1（软删除，非物理删除）", row and row[1] == 1, str(row))
    n_pred = conn.execute(
        "select count(*) from predictions where task_id=?", (victim,)).fetchone()[0]
    n_metric = conn.execute(
        "select count(*) from metrics where task_id=?", (victim,)).fetchone()[0]
    check("🔒 predictions / metrics 一并保留", n_pred > 0 and n_metric == 1,
          f"predictions={n_pred} metrics={n_metric}")
    conn.close()

    st, b = api("DELETE", f"/api/v1/history/{victim}")
    check("⑦ 重复删除幂等（仍成功）", st == 200 and b.get("code") == 0,
          f"HTTP={st} code={b.get('code')}")

    st, b = api("DELETE", "/api/v1/history/999999")
    check("⑦ 删除不存在的任务 → 40401", b.get("code") == 40401)


# =============================================================================
def case_workbench() -> None:
    section("⑧ `UX-P04` 工作台概览")
    st, b = api("GET", "/api/v1/history/dashboard")
    d = b.get("data") or {}
    check("⑧ code 0", b.get("code") == 0, str(b.get("message")))
    check("⑧ 含 stats / recent / has_dataset / has_model",
          all(k in d for k in ("stats", "recent", "has_dataset", "has_model")),
          f"keys={sorted(d.keys())}")
    check("⑧ is_guest=false（已登录）", d.get("is_guest") is False)
    check("⑧ has_model=true（模型已就绪）", d.get("has_model") is True)

    # 🔒 DOD-EP7：工作台最近任务必须与历史列表首屏**逐条一致**
    st, b = api("GET", "/api/v1/history?page=1&size=5")
    hist = [i["task_id"] for i in (b.get("data") or {}).get("items") or []]
    dash = [i["task_id"] for i in d.get("recent") or []]
    check("🔒 工作台最近任务 = 历史列表前 5 条", hist == dash,
          f"hist={hist} dash={dash}")


# =============================================================================
def case_failed_detail() -> None:
    section("⑨ 🔒 失败任务详情（结构完整、不 500）")
    # 🔴 必须挑一个**未软删除**的任务。上一条用例（`case_api20`）刚把
    #    `max(id)` 那个任务软删了，用 `max(id)` 会选中它 —— 于是
    #    `_owned_task` 正确地返 40401，四条断言全挂。
    #    「被删除」与「失败」是两件事：前者 404，后者才是本用例要验的。
    from sqlalchemy import text as sa_text

    from app.core.db import SessionLocal

    db = SessionLocal()
    tid = db.execute(
        sa_text(
            "select id from tasks where is_deleted = 0 and user_id != 0 "
            "order by id desc limit 1"
        )
    ).scalar_one()
    db.execute(
        sa_text(
            "update tasks set status='failed', error_code='E_TIMEOUT', "
            "error_msg='端到端超时（EP-7 验收构造）', completed_stage='inference' "
            "where id=:t"
        ),
        {"t": tid},
    )
    db.execute(sa_text("delete from metrics where task_id=:t"), {"t": tid})
    db.commit()
    db.close()

    st, b = api("GET", f"/api/v1/history/{tid}")
    check("🔒 失败任务详情不 500", st == 200 and b.get("code") == 0,
          f"HTTP={st} code={b.get('code')} msg={str(b.get('message'))[:60]}")
    d = b.get("data") or {}
    check("⑨ 携带 error_code / error_msg / completed_stage",
          d.get("task", {}).get("error_code") == "E_TIMEOUT"
          and d.get("task", {}).get("completed_stage") == "inference"
          and bool(d.get("task", {}).get("error_msg")),
          f"{d.get('task', {}).get('error_code')}/{d.get('task', {}).get('completed_stage')}")
    check("🔒 metrics 为 null 而非全 0（区分「没算」与「算成 0」）",
          (d.get("metrics") or {}).get("weighted_mape") is None,
          f"mape={(d.get('metrics') or {}).get('weighted_mape')}")
    check("⑨ series 为空数组", d.get("series") == [], f"{len(d.get('series') or [])} 条")
    check("⑨ 覆盖率部分有效或全 0", d.get("coverage") is not None,
          json.dumps(d.get("coverage"), ensure_ascii=False)[:60])
    check("⑨ 结果页对失败任务不给半成品（40401）",
          api("GET", f"/api/v1/predictions/{tid}")[1].get("code") == 40401)


# =============================================================================
def case_auth() -> None:
    section("⑩ 鉴权：历史一律要求登录（FR-USR-08）")
    c: TestClient = STATE["client"]
    for method, path, body in (
        ("GET", "/api/v1/history", None),
        ("GET", "/api/v1/history/1", None),
        ("GET", "/api/v1/history/dashboard", None),
        ("POST", "/api/v1/history/compare", {"task_ids": [1, 2]}),
        ("DELETE", "/api/v1/history/1", None),
    ):
        r = c.request(method, path, json=body)
        check(f"⑩ 未登录 {method} {path} → 真实 401", r.status_code == 401,
              f"HTTP={r.status_code}")


# =============================================================================
def main() -> int:
    # 🔴 **整个运行都在同一个 lifespan 内**（EP-6 的教训）：
    #    `TestClient` 的 `with` 一旦提前退出，`holder.clear()` 清掉模型单例，
    #    后续需要推理的用例全返 40404 —— 只读用例照常通过，极易误判为偶发。
    client = TestClient(app)
    with client:
        STATE["client"] = client
        return _run()


def _run() -> int:
    if not case_setup():
        print("\n[FATAL] 前置未通过，后续用例跳过")
        return 1
    case_api17()
    case_api19()
    case_api18()
    case_api20()
    case_workbench()
    case_failed_detail()
    case_auth()

    print(f"\n{'=' * 70}")
    print(f"EP-7 接口验收：{PASS} / {TOTAL} 通过")
    if PASS != TOTAL:
        print("\n未通过项：")
        for ok, name, detail in RESULTS:
            if not ok:
                print(f"  ❌ {name} — {detail}")
    print(f"{'=' * 70}")
    return 0 if PASS == TOTAL else 1


if __name__ == "__main__":
    raise SystemExit(main())