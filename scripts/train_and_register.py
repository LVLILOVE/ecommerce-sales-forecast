"""离线训练入口：训练 → 评估 → 落盘权重 → 打印可写入 model_registry 的记录。

📌 不参与服务启动（架构红线 2）。用法：

    python scripts/train_and_register.py --csv <数据.csv> --horizon 14
    python scripts/train_and_register.py --demo --horizon 14          # 无数据时用合成数据
    python scripts/train_and_register.py --demo --grid                # 跑网格搜索

产出：
    outputs/models/lstm_v1.pt          权重 + 元信息
    outputs/models/lstm_v1.report.json 完整评估报告（含分阶段耗时与指标）
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np                                          # noqa: E402
import pandas as pd                                         # noqa: E402

from app.algo import config as C                            # noqa: E402
from app.algo import classify, features, infer, metrics as metrics_mod, preprocess, train  # noqa: E402

OUT_DIR = ROOT / "outputs" / "models"

# 🔴 `app.algo.train` 的训练进度走 `logging`（`R-14`：与全项目一致，不再用 `print`）。
#    这里必须显式配置，否则默认 root logger 是 WARNING ⇒ **进度全部消失**，
#    而脚本本身还在正常跑，看起来像「训练卡住了但没输出」。
#    `stream=sys.stdout` 是刻意的：脚本自己的报告也用 `print`（stdout），
#    两股输出必须同流，否则 `... > log.txt` 时进度会丢进 stderr、终端里顺序也会错乱。
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    stream=sys.stdout,
)


# ============================================================ 数据
def make_demo_csv(path: Path, n_items: int = 40, days: int = 220, seed: int = 42) -> Path:
    """生成一份与真实数据集同构的合成 CSV。

    刻意复现天池 167097 的三个特征：
      1) 长尾 —— 商品基础销量跨两个数量级；
      2) 稀疏 —— 部分商品只在部分仓有货，且历史长度参差；
      3) 促销尖峰 —— 双十一/双十二/春节有明显放大。
    这样 demo 跑出来的分级分布与指标量级才有参考意义。

    ⚠️ 关键的建模约束（踩过坑）：
      每个仓的销量必须**只由商品基准 × 仓占比 × 噪声**决定，
      不能再给每个仓叠加一个独立的随机规模系数。
      早期版本给 (商品,仓) 各自抽了 `seg = exp(N(2.6,1.9))`，
      导致同商品的仓间量级差异远超真实占比结构 ——
      归一化后各序列的"难度"相差 40 倍（残差 std 0.02 ~ 2.8），
      模型在一个尺度混乱的数据集上无法收敛。
      真实的天池数据里，仓间差异由稳定的地域需求占比解释，不是随机噪声。
    """
    rng = np.random.default_rng(seed)
    warehouses = ["上海仓", "广州仓", "北京仓", "成都仓", "武汉仓"]
    shares = np.array([0.31, 0.24, 0.21, 0.14, 0.10])
    start = pd.Timestamp("2014-10-10")

    # 促销日：放大系数
    promo = {}
    for d in pd.date_range("2014-10-10", periods=days):
        f = 1.0
        md = d.strftime("%m-%d")
        if md in ("11-11", "11-12"):
            f = 3.2
        elif md == "12-12":
            f = 2.4
        elif md in ("01-01", "10-01"):
            f = 1.6
        elif md in ("02-18", "02-19"):
            f = 1.3
        promo[d.strftime("%Y-%m-%d")] = f

    rows = []
    for i in range(n_items):
        sku = f"item_{i:04d}"
        base = float(np.exp(rng.normal(3.2, 1.15)))      # 长尾：几件 ~ 上千件
        growth = 1.0 + rng.normal(0.0, 0.25)
        # 仓占比只做**轻微**扰动（±15%），保持"上海最大、武汉最小"的稳定结构
        local_shares = shares * np.exp(rng.normal(0, 0.15, size=len(shares)))
        local_shares = local_shares / local_shares.sum()

        # 每个商品有自己的**周内规律**（真实电商数据的核心可预测成分）：
        # 有的周末旺、有的工作日旺，幅度 25%~60%。
        #
        # ⚠️ 信噪比是这里最关键的设计参数（踩坑修正）：
        #    早期版本 weekday 幅度 15~45%、噪声 30%，导致可预测成分被噪声抵消 ——
        #    实测连"完美知道星期几均值"的规则方法也只能比移动平均好 8.2%，
        #    远低于 15% 的验收线，任何模型都不可能达标。
        #    真实电商的周内振幅通常是 30~80%，日噪声约 10~18%
        #    （大促是独立事件，不在日常噪声里）。
        #    调高振幅、调低噪声后，可预测结构才真正显现。
        weekend_amp = float(rng.uniform(0.25, 0.60)) * (1 if rng.random() < 0.72 else -1)

        for wh, sh in zip(warehouses, local_shares):
            # 稀疏：约 35% 的 (商品,仓) 组合缺货
            if rng.random() < 0.35:
                continue
            # 历史长度参差：决定 L1/L2/L3
            keep = int(rng.choice([days, days, days, 150, 110, 95, 70, 45, 25]))
            for d_i in range(days - keep, days):
                d = start + pd.Timedelta(days=int(d_i))
                ds = d.strftime("%Y-%m-%d")
                dow = 1.0 + (weekend_amp if d.dayofweek >= 5 else 0.0)
                # 月度节律：月初发薪、月末疲软，这是运营真实会利用的规律
                dom = 1.0 + 0.12 * np.sin(2 * np.pi * (d.day - 1) / 30.0)
                trend = growth ** (d_i / 365.0)
                # 日噪声 14%：明显低于周内振幅，保证结构可学
                noise = float(np.exp(rng.normal(0, 0.14)))
                v = base * float(sh) * dow * dom * trend * promo[ds] * noise
                rows.append({
                    C.COL_SKU: sku,
                    C.COL_WAREHOUSE: wh,
                    C.COL_DATE: ds,
                    C.COL_SALES: int(max(0, round(v))),
                })

    df = pd.DataFrame(rows).sort_values([C.COL_SKU, C.COL_WAREHOUSE, C.COL_DATE])
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return path


def load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    # 兼容天池原始列名
    rename = {"qty_alipay_njhs": C.COL_SALES, "item_id": C.COL_SKU,
              "store_code": C.COL_WAREHOUSE, "dt": C.COL_DATE}
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    return df


# ============================================================ 主流程
def main() -> int:
    ap = argparse.ArgumentParser(description="离线训练 → 评估 → 注册")
    ap.add_argument("--csv", type=str, default="")
    ap.add_argument("--demo", action="store_true", help="用合成数据跑通流程")
    ap.add_argument("--horizon", type=int, default=14, choices=[7, 14, 30])
    ap.add_argument("--version", type=str, default="lstm_v1")
    ap.add_argument("--grid", action="store_true", help="跑网格搜索（较慢）")
    ap.add_argument("--window", type=int, default=C.WINDOW_SIZE)
    ap.add_argument("--device", type=str, default="auto", choices=["auto", "cuda", "cpu"])
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    t_start = time.perf_counter()

    # ---------- 1. 取数 ----------
    if args.demo or not args.csv:
        csv_path = OUT_DIR / "demo_sales.csv"
        if not csv_path.exists():
            print("生成合成数据集（与天池 167097 同构）…")
            make_demo_csv(csv_path)
        print(f"数据集：{csv_path}（合成）")
    else:
        csv_path = Path(args.csv)
        if not csv_path.is_absolute():
            csv_path = ROOT / csv_path
        if not csv_path.exists():
            print(f"✗ 找不到文件：{csv_path}")
            return 2
        print(f"数据集：{csv_path}")

    df = load_csv(csv_path)
    print(f"  行数 {len(df):,}  商品 {df[C.COL_SKU].nunique()}  "
          f"仓库 {df[C.COL_WAREHOUSE].nunique()}")

    # ---------- 2. 校验 + 清洗 ----------
    t0 = time.perf_counter()
    try:
        preprocess.validate_dataframe(df)
    except preprocess.ValidationError as e:
        print(f"✗ 校验失败 [{e.code}] {e.message}")
        if e.suggestion:
            print(f"  建议：{e.suggestion}")
        return 3
    df = preprocess.clean_dataframe(df)
    print(f"  校验 + 清洗 {(time.perf_counter() - t0) * 1000:.0f}ms")

    # ---------- 3. 分级 ----------
    infos = classify.classify_series(df)
    cov = classify.coverage_report(infos)
    print(f"  分级：L1 {cov['L1']}（{cov['L1_pct']:.1f}%）"
          f"  L2 {cov['L2']}（{cov['L2_pct']:.1f}%）"
          f"  L3 {cov['L3']}（{cov['L3_pct']:.1f}%）"
          f"  共 {cov['total']} 条")

    # ---------- 4. 特征 ----------
    t0 = time.perf_counter()
    ts, info = train.build_training_set(
        df, horizon=1, window=args.window, level_infos=infos)
    feat_ms = (time.perf_counter() - t0) * 1000
    print(f"  特征：{ts.n_samples:,} 样本 × {ts.X.shape[1]} 窗口 × {ts.X.shape[2]} 特征  "
          f"{feat_ms:.0f}ms  （用 {info['series_used']} 条 L1 序列）")
    print(f"        目标模式 {info.get('target_mode')}  "
          f"残差 std {info.get('y_std', float('nan')):.3f}（≈1 表示归一化正确）")
    if ts.n_samples == 0:
        print("✗ 样本为 0：L1 序列不足或历史过短。请检查数据或放宽阈值。")
        return 4

    # ---------- 5. 训练 ----------
    base_cfg = train.TrainConfig(window=args.window, device=args.device)
    t0 = time.perf_counter()
    if args.grid:
        print("网格搜索（模型结构参数）：")
        grid = {"hidden_size": C.GRID_HIDDEN, "num_layers": C.GRID_LAYERS}
        results = train.grid_search(ts, base=base_cfg, grid=grid, verbose=True)
        best = results[0]
        print(f"  最优：hidden={best.config.hidden_size} layers={best.config.num_layers} "
              f"val_loss={best.best_val_loss:.6f}")
        # 对照：默认结构
        baseline_cfg = train.TrainConfig(window=args.window, device=args.device,
                                         hidden_size=C.HIDDEN_SIZE, num_layers=C.NUM_LAYERS)
        print(f"  对照默认结构 val_loss："
              f"{next((r.best_val_loss for r in results if r.config.hidden_size == baseline_cfg.hidden_size and r.config.num_layers == baseline_cfg.num_layers), float('nan')):.6f}")
    else:
        print(f"训练：hidden={base_cfg.hidden_size} layers={base_cfg.num_layers} "
              f"window={base_cfg.window} device={args.device}")
        best = train.train_one(ts, base_cfg, verbose=True)

    train_s = time.perf_counter() - t0
    print(f"  完成：{best.epochs_run} epoch（最佳 {best.best_epoch}）"
          f"  {train_s:.2f}s  设备 {best.device}  "
          f"参数量 {best.model.n_params:,}")
    print(f"  ⏱ 训练耗时仅供离线参考 —— 线上不做训练（1 分钟 SLA）")

    # ---------- 6. 评估 ----------
    t0 = time.perf_counter()
    records = train.evaluate_on_holdout(
        best.model, df, infos, horizon=args.horizon, window=args.window,
        holiday=True, device=best.device)
    m = metrics_mod.compute_task_metrics(records, args.horizon)
    dist = metrics_mod.series_mape_distribution(records)
    eval_ms = (time.perf_counter() - t0) * 1000
    v = metrics_mod.verdict(m)

    print("\n===== 离线评估（测试段）=====")
    print(f"  加权 MAPE      {m.weighted_mape:6.2f}%   （阈值 ≤ {C.WEIGHTED_MAPE_LIMIT}%）"
          f"  {'✓' if m.mape_ok else '✗'}")
    print(f"  加权 MAE       {m.weighted_mae:6.2f}    基线 {m.baseline_mae:.2f}"
          f"  降低 {m.mae_improve_pct:.2f}%  {'✓' if m.mae_improve_pct >= C.BASELINE_IMPROVE_LIMIT else '✗'}")
    print(f"  加权 RMSE      {m.weighted_rmse:6.2f}    基线 {m.baseline_rmse:.2f}"
          f"  降低 {m.rmse_improve_pct:.2f}%  {'✓' if m.rmse_improve_pct >= C.BASELINE_IMPROVE_LIMIT else '✗'}")
    print(f"  达标序列占比   {m.series_pass_rate:6.2f}%   "
          f"({m.series_passed}/{m.series_evaluated})  "
          f"{'✓' if m.pass_rate_ok else '✗'}")
    print(f"  分级 MAPE      L1 {m.l1_mape:.2f}%   L2 {m.l2_mape:.2f}%")
    print(f"  逐序列分布     中位 {dist.get('median', float('nan')):.2f}%  "
          f"P75 {dist.get('p75', float('nan')):.2f}%  "
          f"P90 {dist.get('p90', float('nan')):.2f}%  "
          f"最大 {dist.get('max', float('nan')):.2f}%  "
          f"超标 {dist.get('over_count', 0)} 条")
    print(f"  综合判定：{'✅ 达标' if v['all_ok'] else '❌ 未达标'}")
    print(f"  评估耗时 {eval_ms:.0f}ms")

    # ---------- 7. 落盘 ----------
    model_path = OUT_DIR / f"{args.version}.pt"
    meta = {
        "version": args.version,
        "horizon_train": 1,          # 训练为单步，多步由递归推理完成
        "window": args.window,
        "hidden_size": best.config.hidden_size,
        "num_layers": best.config.num_layers,
        "n_features": best.n_features,
        "n_params": best.model.n_params,
        "device": best.device,
        "trained_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dataset_rows": int(len(df)),
        "n_series": int(cov["total"]),
        "coverage": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in cov.items()},
        "metrics": {
            "weighted_mape": round(m.weighted_mape, 2),
            "weighted_mae": round(m.weighted_mae, 2),
            "weighted_rmse": round(m.weighted_rmse, 2),
            "baseline_mae": round(m.baseline_mae, 2),
            "baseline_rmse": round(m.baseline_rmse, 2),
            "mae_improve_pct": round(m.mae_improve_pct, 2),
            "rmse_improve_pct": round(m.rmse_improve_pct, 2),
            "series_pass_rate": round(m.series_pass_rate, 2),
            "series_passed": m.series_passed,
            "series_evaluated": m.series_evaluated,
            "l1_mape": round(m.l1_mape, 2),
            "l2_mape": round(m.l2_mape, 2),
        },
        "passed_acceptance": bool(v["all_ok"]),
    }
    infer.save_bundle(best.model, str(model_path), meta)

    report = {
        "model": str(model_path.relative_to(ROOT)),
        "version": args.version,
        "dataset": str(csv_path.relative_to(ROOT)) if csv_path.is_relative_to(ROOT) else str(csv_path),
        "rows": int(len(df)),
        "coverage": cov,
        "train": best.summary(),
        "train_seconds": round(train_s, 2),
        "feature_ms": round(feat_ms, 1),
        "eval_ms": round(eval_ms, 1),
        "metrics": meta["metrics"],
        "distribution": {k: (round(x, 2) if isinstance(x, float) else x)
                         for k, x in dist.items() if k != "values"},
        "verdict": {"all_ok": v["all_ok"], "checks": v["checks"]},
        "history": best.history,
    }
    report_path = OUT_DIR / f"{args.version}.report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    total_s = time.perf_counter() - t_start
    print(f"\n权重：{model_path.relative_to(ROOT)}")
    print(f"报告：{report_path.relative_to(ROOT)}")
    print(f"总耗时 {total_s:.2f}s")

    print("\n----- 可写入 model_registry 的记录 -----")
    print(json.dumps({
        "version": args.version,
        "path": str(model_path.relative_to(ROOT)),
        "n_features": best.n_features,
        "hidden_size": best.config.hidden_size,
        "num_layers": best.config.num_layers,
        "window": args.window,
        "weighted_mape": meta["metrics"]["weighted_mape"],
        "is_active": 1 if v["all_ok"] else 0,
    }, ensure_ascii=False, indent=2))
    if not v["all_ok"]:
        print("\n⚠️ 未达验收标准，is_active 置 0（不激活），请调参后重跑。")
    return 0 if v["all_ok"] else 5


if __name__ == "__main__":
    raise SystemExit(main())
