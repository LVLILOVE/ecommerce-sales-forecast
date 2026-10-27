"""P0-1 端到端机械复现：推理异常 → `_fail(ip=ip)` TypeError → 500 + 僵尸任务。

复现步骤（真实走一遍 `create_and_run`，不是模拟）：
1. 临时库 + 临时上传目录（不动真实 `data/`）；
2. 建用户，把 `demo/演示数据_正常.csv` 放进上传目录并落一条 `datasets` 记录；
3. 加载真实模型 `outputs/models/lstm_v1.pt`；
4. monkeypatch `app.algo.infer.run_inference` 抛 `RuntimeError`
   （模拟 torch 内部异常 / numpy 形状不匹配等**非 OOM** 异常）；
5. 调 `predict_service.create_and_run(..., ip="1.2.3.4")`；
6. 观测两件事：
   - 是否抛 `TypeError`（抛了 ⇒ 线上是 HTTP 500，而非契约承诺的 code=0/status=failed）
   - `tasks.status` 是不是停在 `'running'`（僵尸任务）

判定：
- 现状 → `[FAIL] TypeError` + `status='running'`
- 修复后 → `[ OK ] code=0 / status=failed / tasks.status='failed'`
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

_tmp = Path(tempfile.mkdtemp(prefix="p0_1_probe_"))
os.environ["DB_PATH"] = str(_tmp / "probe.db")
os.environ["UPLOAD_DIR"] = str(_tmp / "uploads")
os.environ["EXPORT_DIR"] = str(_tmp / "exports")
os.environ["APP_ENV"] = "test"

from app.core.db import SessionLocal, init_db  # noqa: E402
from app.core import model_holder  # noqa: E402
from app.dao import dataset_dao, user_dao  # noqa: E402
from app.models.dataset import Dataset  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import predict_service  # noqa: E402

DEMO = BASE / "demo" / "演示数据_正常.csv"


def _probe() -> int:
    init_db()

    # ---- 1. 用户 ----
    with SessionLocal() as db:
        user = user_dao.create(
            db, email="probe@example.com", password_hash="x", nickname="probe"
        )
        db.commit()
        user_id = user.id

    # ---- 2. 数据集（真实 CSV 落盘）----
    upload_dir = Path(os.environ["UPLOAD_DIR"])
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored = upload_dir / "probe_dataset.csv"
    shutil.copy(DEMO, stored)
    with SessionLocal() as db:
        ds = dataset_dao.create(
            db,
            user_id=user_id,
            file_name="演示数据_正常.csv",
            stored_name=stored.name,
            file_path=str(stored),
            file_size=stored.stat().st_size,
            is_sample=0,
        )
        ds.row_count = 1
        ds.sku_count = 1
        ds.warehouse_count = 1
        ds.valid_series_count = 1
        ds.date_start = "2020-01-01"
        ds.date_end = "2020-01-01"
        db.commit()
        dataset_id = ds.id

    # ---- 3. 真实模型就绪 ----
    handle = model_holder.load_active_model()
    print(f"[INFO] 模型已加载：{handle.version} / {handle.device}")

    # ---- 4. 让推理抛非 OOM 异常 ----
    def _boom(*_a, **_k):
        raise RuntimeError("probe-injected failure")

    import app.algo.infer as infer_mod

    infer_mod.run_inference = _boom  # type: ignore[assignment]

    # ---- 5. 执行 ----
    raised: BaseException | None = None
    payload: dict = {}
    with SessionLocal() as db:
        try:
            payload = predict_service.create_and_run(
                db,
                user_id=user_id,
                dataset_id=dataset_id,
                horizon=7,
                ip="1.2.3.4",
            )
        except BaseException as exc:  # noqa: BLE001
            raised = exc
            print(f"[FAIL] create_and_run 抛出异常：{type(exc).__name__}: {exc}")
            traceback.print_exc(limit=3)

    # ---- 6. 观测任务状态 ----
    with SessionLocal() as db:
        row = predict_service.task_dao.get_by_id(db, 1)
        status = getattr(row, "status", None) if row else None
        print(f"[INFO] tasks.status = {status!r}")

    ok = True
    if raised is not None:
        ok = False
    else:
        # 🔴 `create_and_run` 返回的是**内层载荷**（`code` 是路由层封信封时加的），
        #    所以这里看 `status` / `error_code`，不是 `code`。
        print(f"[INFO] 返回 status={payload.get('status')} "
              f"error_code={payload.get('error_code')} "
              f"error_msg={payload.get('error_msg')}")
        if payload.get("status") != "failed":
            ok = False
        elif payload.get("error_code") != "E_INFER_FAIL":
            ok = False
    if status != "failed":
        ok = False

    if ok:
        print("[ OK ] 软失败契约成立：code=0 + status=failed + tasks.status='failed'")
    else:
        print("[FAIL] 软失败契约被击穿"
              + ("（僵尸任务）" if status == "running" else ""))
    shutil.rmtree(_tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    code = _probe()
    print("=" * 62)
    print("复现结论：", "缺陷仍存在" if code == 1 else "已修复")
    sys.exit(code)
