"""
=============================================================================
EP-4 数据集上传与校验 · 端到端校验
-----------------------------------------------------------------------------
用法（在工作区根目录执行）：
    C:\\ProgramData\\anaconda3\\envs\\deploy\\python.exe scripts/verify_ep4_api.py

覆盖 `DOD-EP4` 全部条目 + 若干「文档写了但极易漏」的边界：

  ①②③④⑤  五类错误各自可复现，且提示含「错在哪一列/哪一行 + 怎么改」
  上限      20 MB / 500,000 行 / 1,000 序列 三类上限各自被拦截并可操作
  断档      「待确认」状态 → 选择插值后继续（gap_filled=1）→ 放弃则作废
  游客      🔒 未登录可完成上传 → 归属 user_id = 0 → 可继续读详情/预览
  明细      🔒 错误明细 CSV 可下载；与正式导出**分目录**；过期即 40402
  中文列名  JT-01：被拒且提示指出需使用英文列名
  契约      API-07 幂等、API-09 分级预判与 eval_report 同源、API-10 搜索
  安全      🔒 落盘名是 UUID（不含用户原始文件名）；BE-41 两个分支顺序
  架构      🔒 services 不 import algo.train；api 不 import dao
  顺序守卫  /datasets/sample|template|gap 必须注册在 /{dataset_id} 之前

设计约定
-----------------------------------------------------------------------------
1. 🔒 **用独立临时库与独立数据目录，不碰 `data/app.db`**
   校验过程会造出一批失败数据集、断档数据集与临时错误明细，直接打在开发库上
   会污染演示数据。故脚本**最先**把 DB_PATH / UPLOAD_DIR / EXPORT_DIR 指向
   `data/tmp/verify_ep4/`，再 import 应用（config 优先读系统环境变量）。
2. 退出码 0 = 全通过；非 0 = 存在未通过项（可用于 CI / 提交前钩子）。
3. 失败时保留临时目录，便于用 sqlite3 复查现场。
=============================================================================
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import sqlite3
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# ⚠️ 必须在 import 应用之前设置环境变量（config 在模块导入时即读取）
# ---------------------------------------------------------------------------
TMP_DIR = ROOT / "data" / "tmp" / "verify_ep4"
if TMP_DIR.exists():
    shutil.rmtree(TMP_DIR, ignore_errors=True)
FIXTURES = TMP_DIR / "fixtures"
FIXTURES.mkdir(parents=True, exist_ok=True)

os.environ["DB_PATH"] = str(TMP_DIR / "app.db")
os.environ["UPLOAD_DIR"] = str(TMP_DIR / "uploads")
os.environ["EXPORT_DIR"] = str(TMP_DIR / "exports")
os.environ["JWT_SECRET"] = "verify-ep4-secret-0123456789abcdefghijklmn"
os.environ["APP_ENV"] = "test"
os.environ["LOGIN_MAX_FAIL"] = "3"

sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    from sqlalchemy import text as sa_text
except Exception as exc:  # noqa: BLE001
    print(f"[FAIL] 缺少依赖（fastapi / httpx / sqlalchemy）：{exc}")
    sys.exit(1)

from app.core import config as app_config                       # noqa: E402
from app.core.db import EXPORT_TMP_DIR, UPLOAD_DIR, SessionLocal  # noqa: E402
from app.main import app                                        # noqa: E402

settings = app_config.settings

client = TestClient(app)

passed = 0
failed = 0
notes: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    global passed, failed
    if cond:
        passed += 1
        print(f"[ OK ] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name} {detail}")
    return bool(cond)


def section(title: str) -> None:
    print("")
    print("=" * 74)
    print(title)
    print("=" * 74)


# ===========================================================================
# 夹具（fixtures）
# ===========================================================================
HDR = "sku_id,warehouse_id,date,sales"


def _write(name: str, body: str, *, bom: bool = False) -> Path:
    p = FIXTURES / name
    data = body.encode("utf-8-sig" if bom else "utf-8")
    p.write_bytes(data)
    return p


def _series_rows(sku: str, wh: str, start_day: int, days: int, base: int = 10,
                 skip: tuple[int, ...] = ()) -> list[str]:
    """生成一条连续序列的 CSV 行；`skip` 里的日偏移会被跳过（造断档用）。"""
    from datetime import date, timedelta

    d0 = date(2015, 1, 1) + timedelta(days=start_day)
    out = []
    for i in range(days):
        if i in skip:
            continue
        d = d0 + timedelta(days=i)
        out.append(f"{sku},{wh},{d.isoformat()},{base + (i % 7)}")
    return out


def build_fixtures() -> dict[str, Path]:
    fx: dict[str, Path] = {}

    # 正常文件：2 条序列 × 100 天（足够 L1）
    rows = _series_rows("item_0001", "上海仓", 0, 100, 20)
    rows += _series_rows("item_0002", "成都仓", 0, 100, 12)
    fx["ok"] = _write("ok.csv", "\n".join([HDR, *rows]) + "\n")

    # 缺 warehouse_id（🔒 仓库列缺失即拒）
    fx["missing_warehouse"] = _write(
        "missing_warehouse.csv",
        "sku_id,date,sales\nitem_0001,2015-01-01,10\nitem_0001,2015-01-02,12\n",
    )
    # 缺 date
    fx["missing_date"] = _write(
        "missing_date.csv",
        "sku_id,warehouse_id,sales\nitem_0001,上海仓,10\n",
    )
    # JT-01：中文列名
    fx["chinese_header"] = _write(
        "chinese_header.csv",
        "商品编码,仓库编码,日期,销量\nitem_0001,上海仓,2015-01-01,10\n",
    )
    # 日期异常（3 种典型错法）
    fx["bad_date"] = _write(
        "bad_date.csv",
        "\n".join(
            [
                HDR,
                "item_0001,上海仓,2015-01-01,10",
                "item_0001,上海仓,2015/13/45,11",
                "item_0001,上海仓,未知,12",
                "item_0001,上海仓,,13",
                "item_0001,上海仓,2015-01-05,14",
            ]
        )
        + "\n",
    )
    # 负值 + 非数值
    fx["negative"] = _write(
        "negative.csv",
        "\n".join(
            [
                HDR,
                "item_0001,上海仓,2015-01-01,10",
                "item_0001,上海仓,2015-01-02,-12",
                "item_0001,上海仓,2015-01-03,暂无",
                "item_0001,上海仓,2015-01-04,-3.5",
                "item_0001,上海仓,2015-01-05,15",
            ]
        )
        + "\n",
    )
    # 断档：2 处（8 天 / 3 天），与 PRD 提示样例同形
    rows = _series_rows("item_0000", "上海仓", 0, 40, 11, skip=tuple(range(5, 13)))
    rows += _series_rows("item_0033", "成都仓", 0, 40, 7, skip=tuple(range(20, 23)))
    fx["gap"] = _write("gap.csv", "\n".join([HDR, *rows]) + "\n")

    # 序列超限：1001 条序列各 1 行
    lines = [HDR]
    for i in range(1001):
        lines.append(f"item_{i:04d},上海仓,2015-01-01,{i % 20 + 1}")
    fx["too_many_series"] = _write("too_many_series.csv", "\n".join(lines) + "\n")

    # 空文件（0 字节）与仅表头
    fx["empty"] = _write("empty.csv", "")
    fx["header_only"] = _write("header_only.csv", HDR + "\n")

    # 扩展名非 .csv
    fx["bad_ext"] = _write("sales.txt", HDR + "\nitem_0001,上海仓,2015-01-01,10\n")

    # 大小超限（21 MB）
    big = FIXTURES / "too_big.csv"
    with big.open("w", encoding="utf-8", newline="") as fh:
        fh.write(HDR + "\n")
        filler = "item_0001,上海仓,2015-01-01," + "1" * 200 + "\n"
        while fh.tell() < 21 * 1024 * 1024:
            fh.write(filler)
    fx["too_big"] = big

    # 行数超限（500,001 行数据，体积仍 < 20 MB，专测「行数」这条上限）
    many = FIXTURES / "too_many_rows.csv"
    with many.open("w", encoding="utf-8", newline="") as fh:
        fh.write(HDR + "\n")
        for i in range(500_001):
            fh.write(f"item_{i % 20:04d},上海仓,2015-01-01,{i % 9 + 1}\n")
    fx["too_many_rows"] = many

    # GBK 编码的正常文件（中文仓库名）
    gbk = FIXTURES / "gbk.csv"
    gbk.write_bytes(
        ("\n".join([HDR, *_series_rows("item_0001", "上海仓", 0, 30, 5)]) + "\n").encode("gb18030")
    )
    fx["gbk"] = gbk

    return fx


def upload(path: Path, *, name: str | None = None, token: str | None = None,
           gap_action: str | None = None) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    data = {"gap_action": gap_action} if gap_action else None
    with path.open("rb") as fh:
        res = client.post(
            "/api/v1/datasets",
            files={"file": (name or path.name, fh, "text/csv")},
            data=data,
            headers=headers,
        )
    return res.status_code, res.json()


def register(email: str, password: str = "Abcd1234") -> None:
    client.post("/api/v1/auth/register", json={"email": email, "password": password})


def login(email: str, password: str = "Abcd1234") -> str:
    res = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    body = res.json()
    assert body["code"] == 0, body
    return body["data"]["access_token"]


# ===========================================================================
# 主流程
# ===========================================================================
def main() -> int:  # noqa: C901 - 校验脚本，线性铺开比拆函数更好读
    with client:  # 触发 lifespan（建库 + ensure_dirs，DEP-7）
        fx = build_fixtures()

        # -------------------------------------------------------------------
        section("0. 前置：目录与种子")
        # -------------------------------------------------------------------
        check("启动后 data/exports/tmp/ 已存在（DEP-7 / JT-11）", EXPORT_TMP_DIR.exists(),
              f"实际：{EXPORT_TMP_DIR}")
        check("启动后 data/uploads/ 已存在（WS-4-01）", UPLOAD_DIR.exists())

        # -------------------------------------------------------------------
        section("1. 五类结构化错误各自可复现（DOD-EP4 ①）")
        # -------------------------------------------------------------------
        _, body = upload(fx["missing_warehouse"])
        check("① 缺 warehouse_id → 40001（🔒 仓库列缺失即拒，FR-UPL-05）",
              body["code"] == 40001, str(body))
        data = body.get("data", {})
        check("① 定位载荷含缺失列清单", data.get("missing_columns") == ["warehouse_id"],
              str(data.get("missing_columns")))
        check("① fail_stage 为第 3 步「表头完整性」", data.get("fail_stage") == "表头完整性",
              str(data.get("fail_stage")))
        check("① 建议非空且点明英文列名（NFR-USE-03 / JT-01）",
              "sku_id" in data.get("suggestion", "")
              and "warehouse_id" in data.get("suggestion", ""),
              str(data.get("suggestion")))
        check("① 缺列场景不写行级明细文件（无行可导出）",
              data.get("dataset_id") is not None
              and not list(EXPORT_TMP_DIR.glob(f"dataset_{data['dataset_id']}_*.csv")),
              str(data.get("dataset_id")))

        _, body = upload(fx["missing_date"])
        check("① 缺 date → 40001", body["code"] == 40001, str(body))

        _, body = upload(fx["chinese_header"])
        check("① JT-01：中文列名文件被拒 → 40001", body["code"] == 40001, str(body))
        sug = body.get("data", {}).get("suggestion", "")
        check("① JT-01：提示指出需使用英文列名 sku_id/warehouse_id/date/sales",
              all(k in sug for k in ("sku_id", "warehouse_id", "date", "sales")), sug)
        check("① JT-01：提示点出检测到的中文列名",
              "商品编码" in sug or "中文列名" in sug, sug)

        _, body = upload(fx["bad_date"])
        check("② 日期异常 → 40002", body["code"] == 40002, str(body))
        unparsable = body.get("data", {}).get("unparsable_rows") or []
        check("② 返回不可解析的行号与原始值（FR-UPL-10）",
              len(unparsable) == 3
              and all(k in unparsable[0] for k in ("row_no", "raw_value", "column")),
              str(unparsable))
        check("② 行号是真实 CSV 行号（表头为第 1 行）",
              [r["row_no"] for r in unparsable] == [3, 4, 5],
              str([r["row_no"] for r in unparsable]))
        check("② 原始值被原样带出（2015/13/45 / 未知 / 空串）",
              [r["raw_value"] for r in unparsable] == ["2015/13/45", "未知", ""],
              str([r["raw_value"] for r in unparsable]))

        _, body = upload(fx["negative"])
        check("③ 负值 → 40003", body["code"] == 40003, str(body))
        negs = body.get("data", {}).get("negative_rows") or []
        check("③ 返回负值行号与值，且不自动取绝对值（FR-UPL-11）",
              [r["row_no"] for r in negs] == [3, 5] and negs[0]["value"] == -12.0, str(negs))
        nn = body.get("data", {}).get("unparsable_rows") or []
        check("③ 销量非数值/空值同归 40003（第 5 步「销量非负」）",
              [r["row_no"] for r in nn] == [4], str(nn))
        check("③ 建议明确「不会自动取绝对值、也不会自动补 0」",
              "不会自动取绝对值" in body.get("data", {}).get("suggestion", ""),
              body.get("data", {}).get("suggestion", ""))

        _, body = upload(fx["gap"])
        check("④ 时间断档 → 40005（FR-UPL-12）", body["code"] == 40005, str(body))
        ranges = body.get("data", {}).get("gap_ranges") or []
        check("④ 断档区间含 sku/仓库/起止/缺失天数",
              len(ranges) == 2
              and all(k in ranges[0] for k in
                      ("sku_id", "warehouse_id", "gap_start", "gap_end", "gap_days")),
              str(ranges))
        check("④ 缺失天数与构造一致（8 天 / 3 天）",
              sorted(r["gap_days"] for r in ranges) == [3, 8], str(ranges))
        check("④ 建议给出二选一且声明「未选择前不会继续」（FR-UPL-13）",
              "自动插值" in body.get("data", {}).get("suggestion", "")
              and "放弃" in body.get("data", {}).get("suggestion", ""),
              body.get("data", {}).get("suggestion", ""))

        _, body = upload(fx["too_many_series"])
        check("⑤ 序列超限 → 40004（FR-UPL-14）", body["code"] == 40004, str(body))
        check("⑤ 返回当前序列数与上限（1001 / 1000）",
              body.get("data", {}).get("series_count") == 1001
              and body.get("data", {}).get("series_limit") == 1000,
              str(body.get("data")))
        check("⑤ 建议提示筛选商品并给出超出条数",
              "1,000" in body.get("data", {}).get("suggestion", "")
              and "超出 1 条" in body.get("data", {}).get("suggestion", ""),
              body.get("data", {}).get("suggestion", ""))

        # -------------------------------------------------------------------
        section("2. 三类上限各自被拦截（DOD-EP4 ②）")
        # -------------------------------------------------------------------
        _, body = upload(fx["bad_ext"])
        check("扩展名非 .csv → 40009", body["code"] == 40009, str(body))
        check("扩展名校验停在第 1 步且未落盘（裁定 A：不建行）",
              body.get("data", {}).get("fail_stage") == "扩展名", str(body.get("data")))

        _, body = upload(fx["empty"])
        check("空文件（0 字节）→ 40010", body["code"] == 40010, str(body))

        _, body = upload(fx["header_only"])
        check("仅表头无数据行 → 40010", body["code"] == 40010, str(body))

        t0 = time.time()
        _, body = upload(fx["too_big"])
        dt = time.time() - t0
        check("大小超限（21 MB）→ 40007，且在流式过程中即中断（FR-UPL-03）",
              body["code"] == 40007, str(body))
        check(f"大小超限响应耗时 < 3 s（实测 {dt:.2f}s，未把 21 MB 全读完）", dt < 3.0, f"{dt:.2f}s")
        check("超限文件未留在 uploads 目录（半成品已清理）",
              not [p for p in UPLOAD_DIR.rglob("*.csv") if p.stat().st_size > 20 * 1024 * 1024])

        t0 = time.time()
        _, body = upload(fx["too_many_rows"])
        dt = time.time() - t0
        check("行数超限（500,001 行）→ 40008", body["code"] == 40008, str(body))
        check(f"行数检查是快速失败（实测 {dt:.2f}s < 5 s）", dt < 5.0, f"{dt:.2f}s")

        # -------------------------------------------------------------------
        section("3. 正常上传：落盘 / 归属 / 统计 / 审计")
        # -------------------------------------------------------------------
        register("ep4-user-a@example.com")
        token_a = login("ep4-user-a@example.com")

        _, body = upload(fx["ok"], name="我的历史销量.csv", token=token_a)
        check("正常文件上传成功（code = 0）", body["code"] == 0, str(body)[:400])
        payload = body.get("data") or {}
        ds = payload.get("dataset") or {}
        ds_ok_id = ds.get("id")
        check("响应含 dataset / preview / series_count / gap_warning",
              all(k in payload for k in
                  ("dataset", "preview", "series_count", "series_limit", "gap_warning")),
              str(list(payload.keys())))
        check("统计正确：200 行 / 2 商品 / 2 仓库 / 2 序列",
              (ds.get("row_count"), ds.get("sku_count"), ds.get("warehouse_count"),
               ds.get("valid_series_count")) == (200, 2, 2, 2), str(ds))
        check("起止日期正确（2015-01-01 ~ 2015-04-10）",
              (ds.get("date_start"), ds.get("date_end")) == ("2015-01-01", "2015-04-10"),
              f"{ds.get('date_start')} ~ {ds.get('date_end')}")
        check("预览默认前 20 行且 total 为总行数",
              len(payload["preview"]["rows"]) == 20 and payload["preview"]["total"] == 200,
              str(payload["preview"]["total"]))
        check("预览列顺序与源文件一致",
              payload["preview"]["columns"] == ["sku_id", "warehouse_id", "date", "sales"],
              str(payload["preview"]["columns"]))
        check("预览中 sales 是数值而非字符串",
              isinstance(payload["preview"]["rows"][0]["sales"], int),
              str(type(payload["preview"]["rows"][0]["sales"])))
        check("无断档时 gap_warning.has_gap = false",
              payload["gap_warning"]["has_gap"] is False, str(payload["gap_warning"]))
        check("series_limit = 1000", payload["series_limit"] == 1000)

        # 🔒 落盘名必须是 UUID，不得包含用户原始文件名
        # ⚠️ 用户 id 不能硬编码：seed.sql 的管理员种子账号占了 id = 1，
        #    故第一个注册用户是 id = 2（EP-4 实测踩到）。
        con = sqlite3.connect(settings.db_path)
        user_a_id = _user_id(con, "ep4-user-a@example.com")
        ds_dir = UPLOAD_DIR / f"u{user_a_id}"
        stored = [p for p in ds_dir.glob("*.csv")] if ds_dir.exists() else []
        check("🔒 按 u{user_id} 分目录（NFR-SEC-07 / WS-4-01）", ds_dir.exists(), str(ds_dir))
        check("🔒 落盘名是纯 UUID（不含用户原始文件名）",
              any(_is_uuid_stem(p.stem) for p in stored)
              and not any("我的历史销量" in p.name for p in stored),
              str([p.name for p in stored]))
        check("原始文件名只进 file_name 字段（展示用）",
              ds.get("file_name") == "我的历史销量.csv", str(ds.get("file_name")))

        # 数据库侧
        row = con.execute(
            "SELECT user_id, stored_name, file_path, status, row_count, gap_filled "
            "FROM datasets WHERE id = ?", (ds_ok_id,)
        ).fetchone()
        check("datasets 行已落库且归属为上传者（user_id 正确）",
              row is not None and row[0] == _user_id(con, "ep4-user-a@example.com"), str(row))
        check("file_path 存相对工作区的路径（数据库设计要求）",
              row is not None and not Path(row[2]).is_absolute(), str(row[2] if row else None))
        check("通过校验的数据集 status = 1、gap_filled = 0",
              row[3] == 1 and row[5] == 0, str(row))
        logs = con.execute(
            "SELECT action, result FROM operation_logs WHERE target_id = ?", (str(ds_ok_id),)
        ).fetchall()
        check("审计留痕：operation_logs 有 action='upload' 记录（FR-SYS-03）",
              any(a == "upload" for a, _ in logs), str(logs))

        # -------------------------------------------------------------------
        section("4. GBK 编码兼容（国内 Excel 另存常见）")
        # -------------------------------------------------------------------
        _, body = upload(fx["gbk"], token=token_a)
        check("GB18030 编码的中文仓库名文件可正常解析", body["code"] == 0, str(body)[:300])
        check("GBK 文件的中文仓库名被正确解码",
              (body.get("data") or {}).get("dataset", {}).get("warehouse_count") == 1,
              str((body.get("data") or {}).get("dataset")))

        # -------------------------------------------------------------------
        section("5. 断档：待确认 → 选择插值 → 继续（DOD-EP4 ③）")
        # -------------------------------------------------------------------
        _, body = upload(fx["gap"], token=token_a)
        gap_id = (body.get("data") or {}).get("dataset_id")
        check("断档时返回 dataset_id（裁定 A：已建「待确认」行）", gap_id is not None, str(body)[:300])
        row = sqlite3.connect(settings.db_path).execute(
            "SELECT status, gap_count, gap_filled FROM datasets WHERE id = ?", (gap_id,)
        ).fetchone()
        check("待确认行：status = 1、gap_count > 0、gap_filled = 0",
              row is not None and row[0] == 1 and row[1] == 2 and row[2] == 0, str(row))

        res = client.post("/api/v1/datasets/gap",
                          json={"dataset_id": gap_id, "action": "abort"},
                          headers={"Authorization": f"Bearer {token_a}"})
        abort_body = res.json()
        check("放弃上传：接口返回成功且无 data",
              abort_body["code"] == 0 and abort_body.get("data") is None, str(abort_body)[:300])
        row = sqlite3.connect(settings.db_path).execute(
            "SELECT status, gap_filled FROM datasets WHERE id = ?", (gap_id,)
        ).fetchone()
        check("放弃上传后数据集被置为失效（status = -1）",
              row is not None and row[0] == -1, str(row))

        # 再传一次，这次选插值
        _, body = upload(fx["gap"], token=token_a)
        gap_id2 = (body.get("data") or {}).get("dataset_id")
        before_rows = sqlite3.connect(settings.db_path).execute(
            "SELECT row_count FROM datasets WHERE id = ?", (gap_id2,)
        ).fetchone()[0]
        res = client.post("/api/v1/datasets/gap",
                          json={"dataset_id": gap_id2, "action": "interpolate"},
                          headers={"Authorization": f"Bearer {token_a}"})
        ibody = res.json()
        check("选择插值：接口返回成功", ibody["code"] == 0, str(ibody)[:300])
        idata = ibody.get("data") or {}
        check("插值后 gap_warning.interpolated = true 且 has_gap = false",
              idata.get("gap_warning", {}).get("interpolated") is True
              and idata.get("gap_warning", {}).get("has_gap") is False,
              str(idata.get("gap_warning")))
        row = sqlite3.connect(settings.db_path).execute(
            "SELECT status, gap_filled, row_count, stored_name, file_path "
            "FROM datasets WHERE id = ?", (gap_id2,)
        ).fetchone()
        check("插值后 gap_filled = 1（WS-4-05）", row[1] == 1, str(row))
        check(f"插值补齐了缺失日期（{before_rows} → {row[2]} 行）", row[2] > before_rows, str(row))
        check("插值产物另存为 *_filled.csv，未覆盖原始文件（FR-SYS-08）",
              str(row[3]).endswith("_filled.csv"), str(row[3]))
        check("插值后数据集仍可用（status = 1）", row[0] == 1, str(row))

        # 插值后的数据再过一次断档检测应为 0
        res = client.get(f"/api/v1/datasets/{gap_id2}",
                         headers={"Authorization": f"Bearer {token_a}"})
        check("插值后 API-09 可正常读取（分级预判可用）",
              res.json()["code"] == 0, str(res.json())[:300])
        res = client.post("/api/v1/datasets/gap",
                          json={"dataset_id": gap_id2, "action": "interpolate"},
                          headers={"Authorization": f"Bearer {token_a}"})
        check("重复处置已解决的断档 → 40012（无待确认断档）",
              res.json()["code"] == 40012, str(res.json())[:200])

        # 无断档的数据集调 /gap
        res = client.post("/api/v1/datasets/gap",
                          json={"dataset_id": ds_ok_id, "action": "interpolate"},
                          headers={"Authorization": f"Bearer {token_a}"})
        check("对无断档数据集调用 /gap → 40012", res.json()["code"] == 40012, str(res.json())[:200])

        # API-06 的 gap_action 参数（文档化的第二条路径）
        _, body = upload(fx["gap"], token=token_a, gap_action="interpolate")
        check("API-06 带 gap_action=interpolate 时一次通过（文档化的快捷路径）",
              body["code"] == 0
              and (body.get("data") or {}).get("gap_warning", {}).get("interpolated") is True,
              str(body)[:300])
        _, body = upload(fx["gap"], token=token_a, gap_action="abort")
        check("API-06 带 gap_action=abort 时按放弃处理并返回 40005",
              body["code"] == 40005, str(body)[:300])

        # -------------------------------------------------------------------
        section("6. 🔒 游客态（DOD-EP4 ④ / FR-USR-08 / JT-10）")
        # -------------------------------------------------------------------
        _, body = upload(fx["ok"], name="guest_sales.csv")
        check("🔒 未登录可直接上传（无需 Token）", body["code"] == 0, str(body)[:300])
        guest_ds = (body.get("data") or {}).get("dataset") or {}
        check("🔒 游客数据集归属 user_id = 0（哨兵）",
              _dataset_user(guest_ds.get("id")) == 0, str(guest_ds.get("id")))

        res = client.get(f"/api/v1/datasets/{guest_ds.get('id')}")
        check("🔒 游客（无 Token）可读自己的数据集详情（裁定 B）",
              res.json()["code"] == 0, str(res.json())[:200])
        res = client.get(f"/api/v1/datasets/{guest_ds.get('id')}/preview?limit=5")
        check("🔒 游客可读数据预览（裁定 B）",
              res.json()["code"] == 0 and len(res.json()["data"]["rows"]) == 5,
              str(res.json())[:200])

        res = client.get("/api/v1/datasets/sample")
        sample_a = res.json()
        check("API-07 游客可加载示例数据", sample_a["code"] == 0, str(sample_a)[:200])
        sample_id_guest = sample_a["data"]["dataset"]["id"]
        res = client.get("/api/v1/datasets/sample")
        check("🔒 API-07 幂等：重复调用返回同一条记录（API-07-1）",
              res.json()["data"]["dataset"]["id"] == sample_id_guest, str(res.json())[:200])
        check("示例数据集 is_sample = 1", sample_a["data"]["dataset"]["is_sample"] == 1)

        res = client.get("/api/v1/datasets/sample",
                         headers={"Authorization": f"Bearer {token_a}"})
        sample_id_user = res.json()["data"]["dataset"]["id"]
        check("🔒 幂等键是 (user_id, is_sample)：登录用户拿到**另一条**记录（API-07-1）",
              sample_id_user != sample_id_guest,
              f"guest={sample_id_guest} user={sample_id_user}")

        # -------------------------------------------------------------------
        section("7. API-09 分级预判 / API-10 预览 / API-08 模板")
        # -------------------------------------------------------------------
        res = client.get(f"/api/v1/datasets/{sample_id_user}",
                         headers={"Authorization": f"Bearer {token_a}"})
        detail = res.json()["data"]
        total_n = detail["level_preview"]["total"]
        con = sqlite3.connect(settings.db_path)
        sample_row = con.execute(
            "SELECT row_count, sku_count, warehouse_count, valid_series_count, date_start, date_end "
            "FROM datasets WHERE id = ?", (sample_id_user,)
        ).fetchone()
        check("示例数据统计与文档一致：14,305 行 / 40 商品 / 5 仓库 / 113 序列",
              sample_row[:4] == (14305, 40, 5, 113), str(sample_row))
        check("API-09 分级预判三项之和 = total（UX-AC-06）",
              detail["level_preview"]["L1"] + detail["level_preview"]["L2"]
              + detail["level_preview"]["L3"] == total_n, str(detail["level_preview"]))
        check("分级预判 total = 数据集的序列数（113）", total_n == 113, str(total_n))
        report = json.loads((ROOT / "outputs" / "eval_report.json").read_text(encoding="utf-8"))
        check("🔒 分级预判与 eval_report.json 的 coverage 同源（不写死数值）",
              (detail["level_preview"]["L1"], detail["level_preview"]["L2"],
               detail["level_preview"]["L3"])
              == (report["coverage"]["L1"], report["coverage"]["L2"], report["coverage"]["L3"]),
              f"{detail['level_preview']} vs {report['coverage']}")
        expected_ms = round(report["timing_ms"]["total_ms"] * total_n / report["coverage"]["total"])
        check("🔒 estimated_ms 由 eval_report 的 timing_ms 折算（GIT-7 防「两套数字」）",
              detail["estimated_ms"] == expected_ms,
              f"{detail['estimated_ms']} vs {expected_ms}")
        check("113 序列不触发 over_budget_risk（阈值 800）",
              detail["over_budget_risk"] is False)

        res = client.get(f"/api/v1/datasets/{sample_id_user}/preview?limit=3",
                         headers={"Authorization": f"Bearer {token_a}"})
        pv = res.json()["data"]
        check("API-10 limit 生效且 total 为数据集总行数（API-10-1）",
              len(pv["rows"]) == 3 and pv["total"] == 14305, f"{len(pv['rows'])} / {pv['total']}")
        res = client.get(f"/api/v1/datasets/{sample_id_user}/preview",
                         params={"limit": 5, "keyword": "item_0000"},
                         headers={"Authorization": f"Bearer {token_a}"})
        rows = res.json()["data"]["rows"]
        check("API-10 keyword 可按商品搜索（API-10-2）",
              res.json()["code"] == 0 and rows and all("item_0000" in r["sku_id"] for r in rows),
              str(rows[:2]))
        res = client.get(f"/api/v1/datasets/{sample_id_user}/preview",
                         params={"limit": 5, "keyword": "上海仓"},
                         headers={"Authorization": f"Bearer {token_a}"})
        rows = res.json()["data"]["rows"]
        check("API-10 keyword 可按仓库搜索",
              bool(rows) and all(r["warehouse_id"] == "上海仓" for r in rows), str(rows[:2]))

        res = client.get("/api/v1/datasets/template")
        tpl = res.content
        check("API-08 模板为 text/csv 且带附件头", "text/csv" in res.headers["content-type"]
              and "attachment" in res.headers.get("content-disposition", ""),
              res.headers.get("content-disposition", ""))
        check("🔒 模板带 UTF-8 BOM（Excel 打开不乱码，NFR-COMP-05 / API-08-1）",
              tpl.startswith(b"\xef\xbb\xbf"), str(tpl[:6]))
        check("模板 = 表头 + 2 行示例（FR-UPL-08）",
              len([ln for ln in tpl.decode("utf-8-sig").strip().splitlines()]) == 3,
              tpl.decode("utf-8-sig"))
        tpl_path = _write("from_template.csv", tpl.decode("utf-8-sig"))
        _, body = upload(tpl_path, token=token_a)
        check("🔒 模板示例数据直接提交即可通过校验（模板可用性最低标准）",
              body["code"] == 0, str(body)[:300])

        # -------------------------------------------------------------------
        section("8. API-11 错误明细（DOD-EP4 ⑤ / JT-11）")
        # -------------------------------------------------------------------
        _, body = upload(fx["negative"], token=token_a)
        neg_id = (body.get("data") or {}).get("dataset_id")
        tmp_files = list(EXPORT_TMP_DIR.glob(f"dataset_{neg_id}_*.csv"))
        check("失败场景生成了错误明细临时文件", len(tmp_files) == 1, str(tmp_files))
        check("🔒 错误明细落在 data/exports/tmp/（与正式导出分目录，JT-11）",
              tmp_files and tmp_files[0].parent == EXPORT_TMP_DIR,
              str(tmp_files[0].parent if tmp_files else None))
        check("🔒 文件名符合 JT-11 命名 dataset_{dataset_id}_{upload_id}.csv",
              tmp_files and _match_jt11(tmp_files[0].name, neg_id), str(tmp_files))

        res = client.get(f"/api/v1/datasets/{neg_id}/errors",
                         headers={"Authorization": f"Bearer {token_a}"})
        check("API-11 可下载错误明细（FR-UPL-06）", res.status_code == 200
              and "text/csv" in res.headers["content-type"], f"HTTP {res.status_code}")
        text = res.content.decode("utf-8-sig")
        lines = text.strip().splitlines()
        check("🔒 错误明细保留原始列并追加 error_reason（FR-UPL-06）",
              lines[0].split(",") == ["sku_id", "warehouse_id", "date", "sales",
                                      "row_no", "error_reason"], str(lines[0]))
        check("🔒 错误明细仅含不合格行（3 行：2 负值 + 1 非数值）", len(lines) == 4
              and all("销量" in ln for ln in lines[1:]), str(lines))
        check("错误明细带 BOM（Excel 打开不乱码）", res.content.startswith(b"\xef\xbb\xbf"))

        res = client.get("/api/v1/datasets/%d/errors" % ds_ok_id,
                         headers={"Authorization": f"Bearer {token_a}"})
        check("校验通过的数据集无错误明细 → 40402",
              res.json()["code"] == 40402, str(res.json())[:200])

        # 过期清理：把 mtime 拨回 2 小时前
        old = time.time() - 2 * 3600
        for f in tmp_files:
            os.utime(f, (old, old))
        res = client.get(f"/api/v1/datasets/{neg_id}/errors",
                         headers={"Authorization": f"Bearer {token_a}"})
        check("🔒 超过 1 小时 TTL 的错误明细不可下载（JT-11）",
              res.json()["code"] == 40402, str(res.json())[:200])
        check("过期文件已被清理", not list(EXPORT_TMP_DIR.glob(f"dataset_{neg_id}_*.csv")))

        # -------------------------------------------------------------------
        # 8b. §3.8 数据清理（TTL 三档，DB-12 三条自保线）
        # -------------------------------------------------------------------
        from app.core.db import EXPORT_DIR                       # noqa: E402
        from app.services import maintenance_service             # noqa: E402

        section("8b. §3.8 数据清理（TTL 三档，DB-12 三条自保线）")

        fresh = EXPORT_TMP_DIR / "dataset_999_fresh.csv"
        stale = EXPORT_TMP_DIR / "dataset_999_stale.csv"
        fresh.write_text("x", encoding="utf-8")
        stale.write_text("x", encoding="utf-8")
        old_ts = time.time() - 2 * 3600
        os.utime(stale, (old_ts, old_ts))
        # 契约：三个清理函数都**返回同一个报告对象**（不是 None）
        # ⚠️ 早期实现返回 None，`rep = cleanup_exports(CleanupReport())` 会拿到
        #    None 再炸在属性上 —— 这条断言就是防它回归。
        _probe = maintenance_service.CleanupReport()
        rep = maintenance_service.cleanup_exports(_probe)
        check("契约：cleanup_exports 返回同一个报告对象（不是 None）",
              rep is _probe and rep is not None, type(rep).__name__)
        check("错误明细 TTL：> 1 小时的文件被清、新文件保留（JT-11）",
              rep.tmp_error_files >= 1 and not stale.exists() and fresh.exists(),
              f"removed={rep.tmp_error_files} stale={stale.exists()} fresh={fresh.exists()}")

        # 正式导出目录里的文件不该被「1 小时」口径误删
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        export_keep = EXPORT_DIR / "predictions_batch.csv"
        export_keep.write_text("x", encoding="utf-8")
        os.utime(export_keep, (old_ts, old_ts))
        rep = maintenance_service.CleanupReport()
        rep = maintenance_service.cleanup_exports(rep)
        check("🔒 tmp/ 与正式导出分口径：2 小时前的导出产物不被 1 小时 TTL 误删（JT-11）",
              export_keep.exists(), "被误删")

        # 游客数据 TTL：造一条 30 小时前的游客数据集 + 落盘文件
        _, body = upload(fx["ok"], name="guest_old.csv")
        old_guest_ds = ((body.get("data") or {}).get("dataset") or {}).get("id")
        # 再上传一条**全新**的游客数据：它必须活下来（TTL 未到）
        _, body_fresh = upload(fx["ok"], name="guest_fresh.csv")
        fresh_guest_ds = ((body_fresh.get("data") or {}).get("dataset") or {}).get("id")
        con = sqlite3.connect(settings.db_path)
        con.execute(
            "UPDATE datasets SET created_at = datetime('now','localtime','-30 hours') WHERE id = ?",
            (old_guest_ds,),
        )
        con.commit()
        guest_file = con.execute(
            "SELECT stored_name FROM datasets WHERE id = ?", (old_guest_ds,)
        ).fetchone()[0]
        guest_path = UPLOAD_DIR / "u0" / guest_file
        fresh_guest_file = con.execute(
            "SELECT stored_name FROM datasets WHERE id = ?", (fresh_guest_ds,)
        ).fetchone()[0]
        fresh_guest_path = UPLOAD_DIR / "u0" / fresh_guest_file
        check("前置：游客上传文件已落盘", guest_path.exists() and fresh_guest_path.exists(),
              f"{guest_path.exists()} / {fresh_guest_path.exists()}")

        db = SessionLocal()
        try:
            rep = maintenance_service.cleanup_all(db)
            db.commit()
        finally:
            db.close()
        check("游客数据 TTL：> 24 小时的游客数据集被清（JT-10）",
              rep.guest_datasets >= 1
              and con.execute("SELECT COUNT(*) FROM datasets WHERE id = ?",
                              (old_guest_ds,)).fetchone()[0] == 0,
              str(rep.as_dict()))
        check("🔒 DB-12 第 2 条：清理游客数据时同时清 data/uploads/u0/（不留无主 CSV）",
              not guest_path.exists(), str(guest_path))
        check("🔒 未过期（< 24h）的游客数据与落盘文件必须保留 —— TTL 不是「一律清空」",
              con.execute("SELECT COUNT(*) FROM datasets WHERE id = ?",
                          (fresh_guest_ds,)).fetchone()[0] == 1
              and fresh_guest_path.exists(),
              f"file_exists={fresh_guest_path.exists()} path={fresh_guest_path}")
        check("🔒 清理不越界：登录用户的正常数据集未被删",
              con.execute("SELECT COUNT(*) FROM datasets WHERE id = ?",
                          (ds_ok_id,)).fetchone()[0] == 1)
        check("🔒 DB-12 第 1 条：清理只按时间与路径前缀判断（源码无业务字段入口）",
              "created_at < :cutoff" in (
                  ROOT / "app" / "services" / "maintenance_service.py"
              ).read_text(encoding="utf-8"))
        try:
            fresh.unlink()
            export_keep.unlink()
        except OSError:  # pragma: no cover
            pass

        # -------------------------------------------------------------------
        section("9. 🔒 数据隔离：BE-41 两个分支（顺序不可交换）")
        # -------------------------------------------------------------------
        register("ep4-user-b@example.com")
        token_b = login("ep4-user-b@example.com")
        res = client.get(f"/api/v1/datasets/{ds_ok_id}",
                         headers={"Authorization": f"Bearer {token_b}"})
        check("🔒 用户 B 读用户 A 的数据集 → 40301（NFR-SEC-02）",
              res.json()["code"] == 40301, str(res.json())[:200])
        res = client.get(f"/api/v1/datasets/{ds_ok_id}/preview",
                         headers={"Authorization": f"Bearer {token_b}"})
        check("🔒 用户 B 读用户 A 的预览 → 40301", res.json()["code"] == 40301)
        res = client.get(f"/api/v1/datasets/{guest_ds.get('id')}",
                         headers={"Authorization": f"Bearer {token_b}"})
        # 🔴 **口径变更（2026-10-27，C-07）**：本条原先断言「放行」。
        #    那等于把「任意注册用户可读任意游客上传的真实销售数据」写进了验收标准 ——
        #    攻击者只需注册一个普通账号即可遍历 dataset_id 抓取全部匿名流量。
        #    `FR-USR-08` 说的是「游客可加载**示例数据**看完整结果」，
        #    从未要求「游客上传的数据对所有人开放」；紧跟其上的
        #    「用户 B 读用户 A 的数据 → 40301」也证明了隔离口径应当一致。
        #    现在：只有 `is_sample=1` 的**系统示例数据**才无条件共享，
        #    游客上传的数据仅对游客身份可见（见 dataset_service.assert_dataset_readable）。
        check("🔒 用户 B 读**游客上传**的数据集 → 40301（收紧 C-07，原先误为放行）",
              res.json()["code"] == 40301, str(res.json())[:200])
        # 系统示例数据仍必须对任何调用者开放（`API-07` 共用一份，FR-USR-08）
        sample_any = client.get("/api/v1/datasets/sample",
                                headers={"Authorization": f"Bearer {token_b}"})
        check("🔒 用户 B 可加载系统示例数据 → 放行（FR-USR-08，示例是公共资源）",
              sample_any.json()["code"] == 0, str(sample_any.json())[:200])
        res = client.get("/api/v1/datasets/999999",
                         headers={"Authorization": f"Bearer {token_a}"})
        check("不存在的数据集 → 40402", res.json()["code"] == 40402, str(res.json())[:200])

        # -------------------------------------------------------------------
        section("10. 路由注册顺序守卫（BE-39 同型的坑）")
        # -------------------------------------------------------------------
        paths = app.openapi()["paths"]
        ordered = list(paths.keys())
        for literal in ("/api/v1/datasets/sample", "/api/v1/datasets/template"):
            check(f"{literal} 注册在 /api/v1/datasets/{{dataset_id}} 之前",
                  ordered.index(literal) < ordered.index("/api/v1/datasets/{dataset_id}"),
                  str(ordered))
        res = client.get("/api/v1/datasets/sample")
        check("字面量路径未被当作 dataset_id 解析（GET /datasets/sample 返回 code=0）",
              res.json()["code"] == 0, str(res.json())[:200])

        # -------------------------------------------------------------------
        section("11. 架构红线自检")
        # -------------------------------------------------------------------
        svc = (
            (ROOT / "app" / "services" / "dataset_service.py").read_text(encoding="utf-8")
        )
        check("🔒 services 不 import algo.train（WS-5-02 前置）",
              not any(m.startswith("app.algo.train") or m == "algo.train"
                      for m in _imported_modules(svc)),
              str(_imported_modules(svc)))
        api_src = (ROOT / "app" / "api" / "v1" / "datasets.py").read_text(encoding="utf-8")
        check("🔒 api 层不直接 import dao（BE-04 分层）",
              not any(m.startswith(("app.dao", "app.dao.")) for m in _imported_modules(api_src)),
              str(_imported_modules(api_src)))
        check("🔒 service 不 import fastapi（职责边界 BE-14）",
              not any(m == "fastapi" or m.startswith("fastapi.") for m in _imported_modules(svc)),
              str(_imported_modules(svc)))
        check("🔒 7 步顺序取自 algo.config（未另抄字面量列表）",
              "VALIDATE_ORDER: tuple[str, ...] = algo_config.VALIDATE_ORDER" in svc)

    # -----------------------------------------------------------------------
    section("结论")
    # -----------------------------------------------------------------------
    print(f"通过 {passed} 项，失败 {failed} 项")
    if notes:
        print("备注：")
        for n in notes:
            print("  -", n)
    print("=" * 74)
    if failed == 0:
        print("结论：全部通过")
    else:
        print(f"结论：存在 {failed} 项未通过（临时目录保留在 {TMP_DIR}）")
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _imported_modules(source: str) -> set[str]:
    """用 AST 取出源码里**真实存在的 import**（模块路径集合）。

    🔒 刻意不用正则扫文本：EP-1 / EP-3 / EP-4 三次踩到同一个坑 ——
       注释与 docstring 里写「本模块不 import fastapi」「禁止 algo.train」，
       正则会把这段说明当成真正的 import 判为违规。AST 只看语法结构，
       天然免疫（这是本项目第 4 次同型教训，已写进实施方案附录 C）。
    """
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:                      # 相对 import（同一包内）
                continue
            base = node.module or ""
            modules.add(base)
            for alias in node.names:
                modules.add(f"{base}.{alias.name}" if base else alias.name)
    return modules


def _is_uuid_stem(stem: str) -> bool:
    try:
        uuid.UUID(stem)
        return True
    except ValueError:
        # 允许 *_filled.csv 这类「UUID + 后缀标记」的派生文件
        return stem.endswith("_filled") and _is_uuid_stem(stem[: -len("_filled")])


def _match_jt11(name: str, dataset_id: int) -> bool:
    prefix = f"dataset_{dataset_id}_"
    if not name.startswith(prefix) or not name.endswith(".csv"):
        return False
    return _is_uuid_stem(name[len(prefix): -len(".csv")])


def _user_id(con: sqlite3.Connection, email: str) -> int:
    got = con.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    return int(got[0]) if got else -1


def _dataset_user(dataset_id: int | None) -> int:
    if dataset_id is None:
        return -1
    db = SessionLocal()
    try:
        got = db.execute(
            sa_text("SELECT user_id FROM datasets WHERE id = :i"), {"i": dataset_id}
        ).fetchone()
        return int(got[0]) if got else -1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
