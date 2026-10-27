"""
=============================================================================
数据库连接层（engine / SessionLocal / Base / init_db）
-----------------------------------------------------------------------------
版本     : V1.0（EP-1）
依据文档 : 《开发技术文档 V1.0》§3.9 SQLite 配置与并发处理（BE-12 / BE-13）
           《数据库设计文档 V1.0》§1.2 连接参数、§1.2.2 WAL 取舍
-----------------------------------------------------------------------------
设计要点：
  1. 每个连接都必须设置的 PRAGMA（BE-12）：journal_mode=WAL / foreign_keys=ON /
     busy_timeout / synchronous=NORMAL。其中 **foreign_keys 是最容易漏的一行**：
     SQLite 出于历史兼容默认关闭外键检查，不开启则 schema.sql 里的外键与
     RESTRICT 行为完全失效（BE-13）。
  2. 建表以 `code/db/schema.sql` 为唯一真源（数据库设计文档 §5.14 的仲裁规则：
     「若文档与 schema.sql 不一致，以 schema.sql 为准」）。故 init_db() 直接
     执行该脚本，而不是用 Base.metadata.create_all()——后者无法创建视图与触发器，
     也不会插入游客哨兵行（它是 user_id = 0 的外键前提）。
  3. `check_same_thread=False`：FastAPI 在线程池中执行同步路由，连接会跨线程。
  4. 不 import algo：本模块属基础设施层，与算法层零耦合（架构红线 3）。
=============================================================================
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core import config

# ---------------------------------------------------------------------------
# 路径常量（全部经 BASE_DIR 推导，禁止硬编码绝对路径）
# ---------------------------------------------------------------------------
BASE_DIR: Path = config.settings.base_dir
DB_PATH: Path = config.settings.db_path
UPLOAD_DIR: Path = config.settings.upload_dir
EXPORT_DIR: Path = config.settings.export_dir
EXPORT_TMP_DIR: Path = EXPORT_DIR / "tmp"

SCHEMA_SQL: Path = BASE_DIR / "code" / "db" / "schema.sql"
SEED_SQL: Path = BASE_DIR / "code" / "db" / "seed.sql"

# 当前 schema 版本（与 scripts/migrate_0001_ch01_role.py 的目标版本一致）
DB_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{DB_PATH.as_posix()}",
    connect_args={"check_same_thread": False},   # FastAPI 多线程访问必需（BE-12）
    echo=False,
    future=True,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _record) -> None:
    """连接级 PRAGMA —— 每个新连接都要执行（BE-12 / BE-13）。

    注意：`journal_mode=WAL` 是**持久化**的（写入库头，一次设置长期有效），
    而 `foreign_keys` / `busy_timeout` / `synchronous` 是**连接级**的，
    每次新连接都必须重设 —— 这正是把它挂在 connect 事件而非启动钩子上的原因。
    """
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")        # NFR-REL-04：并发读写
    cur.execute("PRAGMA foreign_keys=ON")         # 🔒 SQLite 默认关闭外键！
    cur.execute("PRAGMA busy_timeout=15000")      # 写锁等待 15 秒而非立即报错
    # ⚠️ 为什么从 5 秒提到 15 秒（2026-10-27）：
    #   `PRAGMA busy_timeout` 只对「一开始就要写」的事务生效。WAL 下同一时刻
    #   只能有一个 writer，3 个预测任务并发结束时（`verify_ep9_api` 的 B-19
    #   就是这么测的）后两个必须排队。单个任务要写 791 行 `predictions` +
    #   `metrics` + 审计 + 埋点，最坏的排队量已经超过 5 秒。
    #   ⏱️ 实测：5 秒时 B-19 报 `database is locked`；15 秒后 3/3 全部成功。
    #   🔴 但**不要**靠调大超时掩盖问题 —— 真正让锁持有变长的是
    #   「推理前 flush 了 INSERT 却不提交」，那条已在 `predict_service`
    #   的 `EV-PRED-01` 处单独修掉（写完立刻 commit）。
    cur.execute("PRAGMA synchronous=NORMAL")      # WAL 下兼顾安全与性能
    cur.close()


SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,   # 提交后对象仍可读，避免响应序列化时触发延迟加载
    class_=Session,
    future=True,
)


class Base(DeclarativeBase):
    """全部 ORM 实体的基类。

    🔒 时间字段统一使用 `TEXT`（`Mapped[str]`），**不使用** SQLAlchemy 的
    `DateTime`——依据 `NFR-COMP-04`「全部日期按本地时区处理，不引入 UTC 转换」
    与数据库设计文档 §1.3。
    """


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------

def ensure_dirs() -> None:
    """创建运行期目录（幂等）。

    全部位于工作区内，绝不写系统盘（方案 §3.3 红线 6）。
    """
    for d in (DB_PATH.parent, UPLOAD_DIR, EXPORT_DIR, EXPORT_TMP_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _executescript(path: Path) -> None:
    """用 sqlite3 原生驱动执行整段脚本（SQLAlchemy 的 execute 不支持多语句）。"""
    if not path.exists():
        raise FileNotFoundError(f"找不到 SQL 脚本：{path}")
    con = sqlite3.connect(DB_PATH)
    try:
        con.executescript(path.read_text(encoding="utf-8"))
        con.commit()
    finally:
        con.close()


def init_db(run_seed: bool = True) -> None:
    """建库建表（幂等）+ PRAGMA 设置 + 可选种子数据（BE-12）。

    可重复调用：`schema.sql` 全部使用 `CREATE ... IF NOT EXISTS` 与
    `INSERT OR IGNORE`，`seed.sql` 同理。

    :param run_seed: 是否同时执行 `seed.sql`（管理员种子账号 + 模型版本注册）。
                     默认 True，使 app 自愈——幂等脚本的重跑成本是毫秒级。
    """
    ensure_dirs()
    _executescript(SCHEMA_SQL)
    if run_seed:
        _executescript(SEED_SQL)
    _ensure_wal()


def _ensure_wal() -> None:
    """确认库处于 WAL 模式（持久化设置，新库需显式开启一次）。"""
    con = sqlite3.connect(DB_PATH)
    try:
        mode = con.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise RuntimeError(f"无法开启 WAL 模式，当前 journal_mode={mode}")
    finally:
        con.close()


def get_session() -> Iterator[Session]:
    """给离线脚本用的会话上下文（在线路径请用 `core.deps.get_db`）。"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
