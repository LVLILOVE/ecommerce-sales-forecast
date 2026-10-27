"""`FR-ADM-01` 端到端验证：16 个埋点是不是**真的写进库了**？

`event_dao.record_safe()` 早就存在、`events` 表也建了、管理端看板也接了
`count_by_name()` —— 但 2026-10-26 复评时全仓**没有一处调用**，
看板「埋点概览」永远返回空字典。

本脚本把一条完整业务流跑一遍（注册 → 登录 → 上传（含一次校验失败）
→ 预测 → 导出 → 查历史 → 对比），然后**直接查 `events` 表**，
逐条核对应该出现的事件。

复现结果：
- 修复前 → `events` 表 0 行（或只有零星几条），断言失败；
- 修复后 → 至少 10 个不同事件各有一条。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

_tmp = Path(tempfile.mkdtemp(prefix="fr_adm01_"))
os.environ["DB_PATH"] = str(_tmp / "probe.db")
os.environ["UPLOAD_DIR"] = str(_tmp / "uploads")
os.environ["EXPORT_DIR"] = str(_tmp / "exports")
os.environ["APP_ENV"] = "test"

from fastapi.testclient import TestClient  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import event_names as en  # noqa: E402
from app.models.event import Event  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

DEMO = BASE / "demo" / "演示数据_正常.csv"
BAD = BASE / "demo" / "演示数据_失败_缺仓库列.csv"
EMAIL = "probe_events@example.com"
PWD = "ProbePassw0rd2026"


def main() -> int:
    with TestClient(app) as c:
        # 1) 注册 + 登录
        c.post("/api/v1/auth/register", json={"email": EMAIL, "password": PWD, "nickname": "e"})
        # 2) 登录失败（未知邮箱）→ EV-AUTH-03
        c.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": PWD})
        r = c.post("/api/v1/auth/login", json={"email": EMAIL, "password": PWD})
        tok = r.json()["data"]["access_token"]
        h = {"Authorization": f"Bearer {tok}"}

        # 3) 上传失败（缺仓库列）→ EV-DAT-02
        with BAD.open("rb") as fh:
            c.post("/api/v1/datasets", files={"file": (BAD.name, fh, "text/csv")}, headers=h)
        # 4) 上传成功 → EV-DAT-01
        with DEMO.open("rb") as fh:
            r = c.post("/api/v1/datasets", files={"file": (DEMO.name, fh, "text/csv")}, headers=h)
        body = r.json()["data"]
        dataset_id = body.get("dataset_id") or (body.get("dataset") or {}).get("id")

        # 5) 预测 → EV-PRED-01 / EV-PRED-02
        r = c.post("/api/v1/predictions", json={"dataset_id": dataset_id, "horizon": 7}, headers=h)
        payload = r.json()
        task_id = (payload.get("data") or {}).get("task_id")

        # 6) 结果曝光 → EV-VIS-01（+ 有 L3 时的 EV-VIS-03）
        c.get(f"/api/v1/predictions/{task_id}", headers=h)
        # 7) 导出 → EV-VIS-02
        c.get(f"/api/v1/predictions/{task_id}/export?format=csv", headers=h)
        # 8) 历史列表 → EV-HIS-01
        c.get("/api/v1/history", headers=h)

        # 9) 新开一条任务做对比 → EV-HIS-02
        r = c.post("/api/v1/predictions", json={"dataset_id": dataset_id, "horizon": 14}, headers=h)
        task2 = (r.json().get("data") or {}).get("task_id")
        c.post("/api/v1/history/compare", json={"task_ids": [task_id, task2]}, headers=h)

        # 10) 历史导出（`FR-HIS-03` 新通道）→ 也该记 EV-VIS-02
        r = c.get(f"/api/v1/history/{task_id}/export?format=csv", headers=h)
        ok_hist_export = r.status_code == 200 and len(r.content) > 100

        # 11) 管理端看板：埋点概览必须补齐 16 个 key
        #     先造个管理员（直接改库，避免依赖种子口令）
        from app.dao import user_dao

        with SessionLocal() as db:
            u = user_dao.get_user_by_email(db, EMAIL)
            u.role = "admin"
            db.commit()
        dash = c.get("/api/v1/admin/overview", headers=h).json()
        event_counts = (dash.get("data") or {}).get("event_counts") or {}

    # ---- 查库核对 ----
    with SessionLocal() as db:
        rows = db.execute(
            select(Event.event_name, func.count()).group_by(Event.event_name)
        ).all()
    seen = {name: n for name, n in rows}

    print("=" * 66)
    print(f"events 表实际事件数：{len(seen)} 种 / {sum(seen.values())} 条")
    for name in en.ALL_EVENTS:
        mark = "✓" if seen.get(name) else "·"
        print(f"  [{mark}] {name:<14} {en.EVENT_LABELS[name]:<16} {seen.get(name, 0)}")

    # 一次完整业务流**至少**应该产生这些事件
    required = {
        en.EV_AUTH_01_REGISTER, en.EV_AUTH_02_LOGIN, en.EV_AUTH_03_LOGIN_FAIL,
        en.EV_DAT_01_UPLOAD, en.EV_DAT_02_VALIDATE_FAIL,
        en.EV_PRED_01_SUBMIT, en.EV_PRED_02_COMPLETE,
        en.EV_VIS_01_RESULT_VIEW, en.EV_VIS_02_RESULT_EXPORT,
        en.EV_HIS_01_HISTORY_VIEW, en.EV_HIS_02_HISTORY_COMPARE,
    }
    missing = sorted(required - set(seen))

    ok = True
    if missing:
        print(f"\n[FAIL] 缺少事件：{missing}")
        ok = False
    if not ok_hist_export:
        print("[FAIL] 历史导出接口不可用（`FR-HIS-03`）")
        ok = False
    if len(event_counts) != 16:
        print(f"[FAIL] 管理端埋点概览 key 数 = {len(event_counts)}，应为 16")
        ok = False

    if ok:
        print("\n[ OK ] 埋点链路成立：11 类事件均已落库，看板补齐 16 个 key，"
              "历史导出可用")
    shutil.rmtree(_tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    code = main()
    print("=" * 66)
    print("结论：", "埋点已接通" if code == 0 else "埋点仍未接通")
    sys.exit(code)
