#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SQLite 在线备份 + 轮转 + 可恢复性自检。

为什么需要它：
    `docs/电商销量预测系统_部署运维手册_V1.0.md` §9 只给了「手工 `sqlite3 .backup`」
    这类步骤 —— 手工步骤在真实运维里等于「不会被执行」。而 SQLite 在 **WAL 模式**
    下直接 `cp app.db` 会拿到一个**缺 WAL 内容的半成品**（看起来是完整文件，
    实际丢了最近的事务），是最容易被误认为「备份好了」的坑。

本脚本做四件事：
    1) 用 **`sqlite3.Connection.backup()`**（官方在线备份 API）复制，
       WAL 下的未提交内容会被正确合并，**无需停服务**；
    2) **轮转**：只保留最近 N 份（默认 7），超出的按时间删除；
    3) **自检**：对备份文件跑 `PRAGMA integrity_check` 与关键表行数，
       与源库比对 —— 备份「存在」不等于「可用」；
    4) 输出一行可读摘要，便于塞进 crontab / 计划任务日志。

用法：
    python scripts/backup_db.py                      # 备份 data/app.db → backups/，保留 7 份
    python scripts/backup_db.py --keep 30            # 保留 30 份
    python scripts/backup_db.py --dry-run            # 只看会做什么，不写盘
    python scripts/backup_db.py --db data/app.db --out-dir D:/backup

定时执行（两种都行）：
    · Linux/macOS cron：  0 3 * * * cd <项目目录> && python scripts/backup_db.py >> logs/backup.log 2>&1
    · Windows 计划任务：  程序 python，参数 scripts/backup_db.py，起始于 <项目目录>

退出码：0 成功；1 源库缺失 / 备份或自检失败；2 参数错误。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = Path(os.environ.get("DB_PATH", ROOT / "data" / "app.db"))
DEFAULT_OUT = ROOT / "backups"

# 备份后要核对行数的表（存在才核对，缺表不算失败 —— 兼容不同阶段的历史库）
KEY_TABLES = ("users", "datasets", "tasks", "predictions", "metrics", "operation_logs")


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in KEY_TABLES:
        try:
            out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error:
            continue
    return out


def _purge_sidecars(db_file: Path) -> None:
    """删除 `-wal` / `-shm` 伴生文件（SQLite 在 WAL 模式下会顺手创建）。"""
    for suffix in ("-wal", "-shm"):
        try:
            Path(str(db_file) + suffix).unlink(missing_ok=True)
        except OSError:
            pass


def _unlink_with_retry(p: Path, attempts: int = 3, delay: float = 0.3) -> bool:
    """删除文件，带重试。

    ⚠️ Windows 上「刚关闭连接立刻删」经常失败（文件句柄释放有延迟），
    真实表现是「日志说清理了、文件还在」。轮转必须容忍这一瞬间。
    """
    import time

    for i in range(attempts):
        try:
            p.unlink(missing_ok=True)
            return True
        except OSError:
            if i == attempts - 1:
                return False
            time.sleep(delay)
    return False


def backup(db: Path, out_dir: Path, keep: int, dry_run: bool) -> int:
    if not db.exists():
        print(f"[FAIL] 源库不存在：{db}")
        print("       提示：DB_PATH 环境变量或 --db 可指定位置；首次启动会自动建库。")
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = out_dir / f"{db.stem}-{stamp}.db"

    print(f"[INFO] 源库 : {db}  ({db.stat().st_size / 1024:.1f} KB)")
    print(f"[INFO] 目标 : {dest}")

    if dry_run:
        print("[DRY-RUN] 不写盘。将执行：online backup → integrity_check → 轮转保留 %d 份" % keep)
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 1) 在线备份 ----------
    try:
        src = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)          # 官方 API：正确处理 WAL
        # 备份库会继承源库的 journal_mode（本项目是 WAL），于是读一次就生成
        # -wal / -shm 伴生文件，既污染备份目录又让轮转漏删。备份是一次性快照，
        # 切成 DELETE 模式即可根除。
        dst.execute("PRAGMA journal_mode=DELETE")
        dst.close()
        src.close()
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 备份失败：{type(exc).__name__}: {exc}")
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        return 1

    # ---------- 2) 自检：完整性与行数 ----------
    try:
        chk = sqlite3.connect(f"file:{Path(dest).as_posix()}?mode=ro", uri=True)
        integrity = chk.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            print(f"[FAIL] 备份文件 integrity_check = {integrity!r}，已删除该备份")
            chk.close()
            dest.unlink(missing_ok=True)
            return 1
        dst_counts = _counts(chk)
        chk.close()

        src = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
        src_counts = _counts(src)
        src.close()
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 自检失败：{type(exc).__name__}: {exc}")
        return 1

    # 只读打开仍可能生成空伴生文件，一并清掉（避免它们挡住后续轮转删除）
    _purge_sidecars(dest)

    diff = {t: (src_counts.get(t), dst_counts.get(t)) for t in src_counts
            if src_counts.get(t) != dst_counts.get(t)}
    if diff:
        print(f"[FAIL] 行数不一致（源, 备份）：{diff}")
        return 1

    # ---------- 3) 轮转 ----------
    olds = sorted(out_dir.glob(f"{db.stem}-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed, failed = [], []
    for p in olds[keep:]:
        if _unlink_with_retry(p):
            _purge_sidecars(p)
            removed.append(p.name)
        else:
            failed.append(p.name)

    # 顺手清掉历史遗留的伴生文件（早期版本可能留下）
    for p in list(out_dir.glob(f"{db.stem}-*.db-wal")) + list(out_dir.glob(f"{db.stem}-*.db-shm")):
        _unlink_with_retry(p)

    # ---------- 4) 摘要 ----------
    rows = ", ".join(f"{k}={v}" for k, v in dst_counts.items()) or "（无业务表）"
    print(f"[OK] 备份完成 {dest.name}  {dest.stat().st_size / 1024:.1f} KB")
    print(f"[OK] integrity_check = ok；行数一致：{rows}")
    print(f"[OK] 现存备份 {min(len(olds), keep)} 份（保留策略 {keep}）"
          + (f"；已清理 {len(removed)} 份旧备份" if removed else "")
          + (f"；⚠️ {len(failed)} 份删除失败（文件被占用）：{', '.join(failed)}" if failed else ""))
    print("[TIP] 恢复：停止服务 → 用备份文件替换 data/app.db（同时删除 .db-wal / .db-shm）→ 启动")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="SQLite 在线备份 + 轮转 + 自检")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"源库路径（默认 {DEFAULT_DB}）")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT, help=f"备份目录（默认 {DEFAULT_OUT}）")
    ap.add_argument("--keep", type=int, default=7, help="保留份数（默认 7）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写盘")
    args = ap.parse_args()

    if args.keep < 1:
        print("[FAIL] --keep 必须 ≥ 1")
        return 2

    try:
        return backup(args.db, args.out_dir, args.keep, args.dry_run)
    except KeyboardInterrupt:
        return 1


if __name__ == "__main__":
    sys.exit(main())
