"""C-06 端到端验证：60 秒硬约束**真的能中断**任务吗？

做法：把 `TASK_BUDGET_MS` 调到 **1 毫秒**，跑一个真实预测任务，看它是否
在**流水线中途**就被拦下并转成软失败（而不是跑完再说、也不是抛 500）。

判定（三条缺一不可）：
1. `create_and_run` **不抛异常**（软失败，`API-12-2` 禁止 502/504）；
2. 返回 `status="failed"` + `error_code="E_TIMEOUT"` + `completed_stage` 有值；
3. `tasks.status = 'failed'`（**不是** running 僵尸，也不是 succeeded）。

修复前的行为：唯一的检查点在两遍流水线**跑完之后**，1 ms 预算形同虚设 ——
任务会跑完整条流水线并被标记 `succeeded`，60 秒承诺没有任何约束力。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

_tmp = Path(tempfile.mkdtemp(prefix="c06_deadline_"))
os.environ["DB_PATH"] = str(_tmp / "probe.db")
os.environ["UPLOAD_DIR"] = str(_tmp / "uploads")
os.environ["EXPORT_DIR"] = str(_tmp / "exports")
os.environ["APP_ENV"] = "test"

from app.core import config as app_config  # noqa: E402
from app.core import model_holder  # noqa: E402
from app.core.db import SessionLocal, init_db  # noqa: E402
from app.dao import dataset_dao, user_dao  # noqa: E402
from app.services import predict_service  # noqa: E402

DEMO = BASE / "demo" / "演示数据_正常.csv"


def main() -> int:
    init_db()

    with SessionLocal() as db:
        user = user_dao.create(
            db, email="deadline@example.com", password_hash="x", nickname="probe"
        )
        db.commit()
        user_id = user.id

    upload_dir = Path(os.environ["UPLOAD_DIR"])
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored = upload_dir / "probe.csv"
    shutil.copy(DEMO, stored)
    with SessionLocal() as db:
        ds = dataset_dao.create(
            db, user_id=user_id, file_name="演示数据_正常.csv",
            stored_name=stored.name, file_path=str(stored),
            file_size=stored.stat().st_size, is_sample=0,
        )
        ds.row_count = 1
        ds.sku_count = 1
        ds.warehouse_count = 1
        ds.valid_series_count = 1
        ds.date_start = "2020-01-01"
        ds.date_end = "2020-01-01"
        db.commit()
        dataset_id = ds.id

    handle = model_holder.load_active_model()
    print(f"[INFO] 模型已加载：{handle.version} / {handle.device}")

    # 🔴 关键：把预算压到 1 毫秒 —— 任何真实流水线都不可能在这个预算内跑完。
    #    ⚠️ `Settings` 是 **frozen dataclass**，不能直接改字段；
    #    用 `dataclasses.replace` 造一个新实例，并同时替换
    #    `config.settings` 与 `predict_service.settings`（后者是模块级引用）。
    import dataclasses

    original = app_config.settings
    tight = dataclasses.replace(original, task_budget_ms=1)
    app_config.settings = tight
    predict_service.settings = tight
    print(f"[INFO] TASK_BUDGET_MS: {original.task_budget_ms} → 1（压到极致，制造必然超时）")

    raised: BaseException | None = None
    payload: dict = {}
    t0 = time.perf_counter()
    with SessionLocal() as db:
        try:
            payload = predict_service.create_and_run(
                db, user_id=user_id, dataset_id=dataset_id, horizon=7, ip="1.2.3.4"
            )
        except BaseException as exc:  # noqa: BLE001
            raised = exc
    wall_ms = (time.perf_counter() - t0) * 1000
    app_config.settings = original
    predict_service.settings = original

    if raised is not None:
        print(f"[FAIL] 抛出了异常（应为软失败）：{type(raised).__name__}: {raised}")
        shutil.rmtree(_tmp, ignore_errors=True)
        return 1

    status = payload.get("status")
    error_code = payload.get("error_code")
    stage = payload.get("completed_stage")
    print(f"[INFO] wall={wall_ms:.0f} ms  status={status} "
          f"error_code={error_code} completed_stage={stage}")

    with SessionLocal() as db:
        row = predict_service.task_dao.get_by_id(db, payload.get("task_id") or 1)
        db_status = getattr(row, "status", None) if row else None
    print(f"[INFO] tasks.status = {db_status!r}")

    ok = True
    if status != "failed":
        print("[FAIL] 预算 1 ms 却没有软失败 —— 说明 deadline 检查点仍在流水线之后")
        ok = False
    if error_code != "E_TIMEOUT":
        print(f"[FAIL] error_code={error_code}，应为 E_TIMEOUT")
        ok = False
    if not stage:
        print("[FAIL] completed_stage 为空（前端无法说明「跑到哪一步被中断」）")
        ok = False
    elif stage in ("metrics", "features", "inference"):
        # 🔴 **最关键的判据**：中断必须发生在**早期阶段**。
        #    修复前唯一的检查点在两遍流水线跑完之后，
        #    所以 completed_stage 会是 metrics（甚至已完成），
        #    只是 duration_ms 超标 —— 那是「事后发现」，不是「中途中断」。
        print(f"[FAIL] completed_stage={stage!r} —— 中断发生在流水线末期，"
              f"说明 deadline 检查点仍在流水线之后（事后才发现）")
        ok = False
    if db_status != "failed":
        print(f"[FAIL] tasks.status={db_status!r}，应为 'failed'")
        ok = False

    if ok:
        print("[ OK ] C-06 成立：预算耗尽的瞬间即中断，且落成 E_TIMEOUT 软失败")
    shutil.rmtree(_tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    code = main()
    print("=" * 62)
    print("结论：", "60 秒硬约束生效" if code == 0 else "硬约束仍未生效")
    sys.exit(code)
