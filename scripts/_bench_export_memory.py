"""导出内存 A/B 基准（`_` 前缀：辅助脚本，不受 verify_ep9 登记约束）。

对比两条路径在**同一份数据**上的峰值内存：

  A) 旧实现：`StringIO` 收口全部行 → `.encode()` → 整体 bytes
     （复现 2026-10-26 前的 `_build_csv`，见评审报告 R-01）
  B) 新实现：`result_service.iter_export_chunks()` 逐批产出、消费即丢

用法：
    python scripts/_bench_export_memory.py [行数，默认 500000]

⚠️ 用独立临时库（`DB_PATH`），不碰 `data/app.db`；关闭外键以便直接灌数。
"""
from __future__ import annotations

import io
import csv
import os
import shutil
import sys
import tempfile
import time
import tracemalloc
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ROWS = int(sys.argv[1]) if len(sys.argv) > 1 else 200_000
TMP = ROOT / "data" / "tmp" / "bench_export"

# 🔒 必须先设环境再 import app（config 在导入期读 env）
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
os.environ["DB_PATH"] = str(TMP / "bench.db")
os.environ["UPLOAD_DIR"] = str(TMP / "uploads")
os.environ["EXPORT_DIR"] = str(TMP / "exports")
os.environ.setdefault("JWT_SECRET", "bench-secret-" + "x" * 32)

from sqlalchemy import text  # noqa: E402

from app.core.db import SessionLocal, init_db  # noqa: E402
from app.dao import prediction_dao  # noqa: E402
from app.services import result_service as rs  # noqa: E402


def seed(n: int) -> None:
    init_db(run_seed=False)
    stmt = text(
        "INSERT INTO predictions (task_id, sku_id, warehouse_id, forecast_date,"
        " predicted_value, actual_value, baseline_value, granularity_level)"
        " VALUES (:t, :s, :w, :d, :p, :a, :b, :g)"
    )
    # 🔒 表上有 UNIQUE(task_id, warehouse_id, sku_id, forecast_date)：
    #    必须保证 (序列, 日期) 组合唯一 —— 每序列 400 天，序列号决定 sku/warehouse。
    days_per_series = 400
    base = date(2026, 1, 1)

    def row_of(i: int) -> dict:
        series, day = divmod(i, days_per_series)
        return {
            "t": 1,
            "s": f"SKU_{series % 200:04d}",
            "w": f"WH_{series // 200}",
            "d": (base + timedelta(days=day)).isoformat(),
            "p": 100.0 + (i % 37),
            "a": None if i % 3 == 0 else 90.0 + (i % 41),
            "b": 95.0,
            "g": "L1",
        }

    batch = 20_000
    with SessionLocal() as db:
        db.execute(text("PRAGMA foreign_keys=OFF"))
        for start in range(0, n, batch):
            db.execute(stmt, [row_of(i) for i in range(start, min(start + batch, n))])
        db.commit()
    with SessionLocal() as db:
        got = prediction_dao.count_by_task(db, 1)
    print(f"  已灌数据: {got:,} 行（目标 {n:,}）")


def run_old() -> tuple[float, int, int]:
    """复现旧实现：全量收口 + encode 两份。"""
    tracemalloc.start()
    t0 = time.perf_counter()

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(rs.EXPORT_HEADER)
    with SessionLocal() as db:
        for row in prediction_dao.iter_all_for_export(db, 1):
            writer.writerow(rs._export_cells(row))
    content = buf.getvalue().encode("utf-8-sig")

    dt = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return dt, peak, len(content)


def run_new() -> tuple[float, int, int]:
    """新实现：生成器逐批产出，消费即丢（模拟 StreamingResponse）。"""
    spec = rs.ExportSpec(fmt="csv", name="bench.csv", mime="text/csv", task_id=1, dump=False)
    tracemalloc.start()
    t0 = time.perf_counter()

    nbytes = 0
    chunks = 0
    for chunk in rs.iter_export_chunks(spec):
        nbytes += len(chunk)
        chunks += 1

    dt = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return dt, peak, nbytes, chunks


def main() -> int:
    print(f"=== 导出内存 A/B（{ROWS:,} 行）===")
    seed(ROWS)

    dt_a, peak_a, size_a = run_old()
    print(f"\n  A) 旧实现（StringIO + encode）")
    print(f"     峰值内存 : {peak_a / 1024 / 1024:8.1f} MB")
    print(f"     耗时     : {dt_a:8.2f} s")
    print(f"     产物大小 : {size_a / 1024 / 1024:8.1f} MB")

    dt_b, peak_b, size_b, chunks = run_new()
    print(f"\n  B) 新实现（生成器流式）")
    print(f"     峰值内存 : {peak_b / 1024 / 1024:8.1f} MB")
    print(f"     耗时     : {dt_b:8.2f} s")
    print(f"     产物大小 : {size_b / 1024 / 1024:8.1f} MB（{chunks} 块）")

    print(f"\n  判定:")
    print(f"     内存比   : A/B = {peak_a / max(peak_b, 1):.1f}×  "
          f"（↓ {100 - peak_b / peak_a * 100:.1f}%）")
    print(f"     字节一致 : {'✅' if size_a == size_b else f'❌ {size_a} vs {size_b}'}")
    print(f"     BOM 在首块: {'（见下）'}")

    # 首块必须是 BOM
    first = next(iter(rs.iter_export_chunks(
        rs.ExportSpec(fmt="csv", name="x.csv", mime="text/csv", task_id=1, dump=False))))
    print(f"     首块前 3 字节: {first[:3]!r}  "
          f"{'✅ UTF-8 BOM' if first[:3] == b'\xef\xbb\xbf' else '❌'}")

    shutil.rmtree(TMP, ignore_errors=True)
    return 0 if size_a == size_b else 1


if __name__ == "__main__":
    raise SystemExit(main())
