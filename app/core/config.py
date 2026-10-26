"""
=============================================================================
应用配置（Settings + 路径常量）
-----------------------------------------------------------------------------
版本     : V1.0（EP-0）
依据文档 : 《开发技术文档 V1.0》§2.4 环境变量清单
-----------------------------------------------------------------------------
关键约定：
  1. 所有路径以 BASE_DIR 为基准拼接，禁止硬编码绝对路径（方案 §2.4 🔒）。
  2. 不依赖 python-dotenv / pydantic-settings —— 二者均不在依赖清单（§1.2），
     故这里内置一个极简 .env 加载器，避免引入未登记的依赖。
  3. 环境变量优先级：系统环境变量 > .env 文件 > 默认值。
  4. BE-05：JWT_SECRET 缺失时必须启动失败。该校验由 EP-2（认证）在启动自检
     中调用 ensure_secret_configured()，本模块只提供能力、不擅自抛错。
=============================================================================
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 应用版本（与 FastAPI(app, version=...) 保持同源）
APP_VERSION = "1.0.0"

# 项目根目录：app/core/config.py → parents[2] = 销量预测/
BASE_DIR = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> dict[str, str]:
    """极简 .env 解析器。

    规则：忽略空行与 `#` 注释；`KEY=VALUE`；支持单/双引号包裹；不做变量插值。
    """
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            result[key] = value
    return result


_dotenv = _load_dotenv(BASE_DIR / ".env")


def _get(key: str, default: str = "") -> str:
    """读取环境变量：系统环境优先，其次 .env，最后默认值。"""
    return os.environ.get(key, _dotenv.get(key, default))


def _get_int(key: str, default: int) -> int:
    try:
        return int(_get(key, str(default)))
    except (TypeError, ValueError):
        return default


def _get_bool(key: str, default: bool) -> bool:
    """读取布尔环境变量。

    `1/true/yes/on`（不分大小写）为真；空串或未设置回落到 `default`。
    刻意**不**把任意非空串当假 —— 「拼错的值静默变成 false」比直接报错更难查。
    """
    raw = _get(key, "")
    if raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _resolve(base: Path, value: str) -> Path:
    """把相对路径解析为基于 BASE_DIR 的绝对路径；已是绝对路径则原样返回。"""
    p = Path(value)
    return p if p.is_absolute() else (base / p)


@dataclass(frozen=True)
class Settings:
    """运行期配置（读取自环境变量 / .env）。

    字段与《开发技术文档》§2.4 的变量表一一对应。
    """

    app_env: str
    app_port: int
    base_dir: Path
    db_path: Path
    upload_dir: Path
    export_dir: Path
    model_dir: Path
    active_model: str
    jwt_secret: str
    jwt_expire_minutes: int
    login_max_fail: int
    login_lock_seconds: int
    # ---- 注册限流（`R-07`，2026-10-26）------------------------------------
    # 公开部署后 `/auth/register` 是**唯一不需要任何凭证**的写入口，
    # 不限流就能被脚本批量注册刷库；且 `register()` 里 bcrypt 在入库前执行，
    # 连「邮箱已存在」的失败请求都要付一次 ~250 ms 的哈希成本（CPU DoS）。
    register_max_attempts: int
    register_max_success: int
    register_window_seconds: int
    max_file_size_mb: int
    max_rows: int
    max_series: int
    task_budget_ms: int
    export_ttl_days: int
    export_tmp_ttl_seconds: int
    guest_data_ttl_hours: int
    demo_csv: Path
    cors_origins: tuple[str, ...]
    torch_device: str
    # ---- 对外发布相关（2026-10-15 新增，见《对外发布与多用户访问方案 V1.0》）----
    allow_guest: bool
    serve_frontend: bool
    frontend_dir: Path

    @classmethod
    def from_env(cls, base_dir: Path = BASE_DIR) -> "Settings":
        model_dir = _resolve(base_dir, _get("MODEL_DIR", "outputs/models"))
        return cls(
            app_env=_get("APP_ENV", "development"),
            app_port=_get_int("APP_PORT", 8000),
            base_dir=base_dir,
            db_path=_resolve(base_dir, _get("DB_PATH", "data/app.db")),
            upload_dir=_resolve(base_dir, _get("UPLOAD_DIR", "data/uploads")),
            export_dir=_resolve(base_dir, _get("EXPORT_DIR", "data/exports")),
            model_dir=model_dir,
            active_model=_get("ACTIVE_MODEL", "lstm_v1"),
            jwt_secret=_get("JWT_SECRET", ""),
            jwt_expire_minutes=_get_int("JWT_EXPIRE_MINUTES", 720),
            login_max_fail=_get_int("LOGIN_MAX_FAIL", 3),
            login_lock_seconds=_get_int("LOGIN_LOCK_SECONDS", 60),
            # 30 次尝试 / 小时 / IP：正常用户注册 1 次，`verify_auth` 一轮约 10 次，
            # 都留有余量；对脚本刷库与「每次请求烧一次 bcrypt」的 CPU 消耗足够致命。
            register_max_attempts=_get_int("REGISTER_MAX_ATTEMPTS", 30),
            # 10 个**成功**注册 / 小时 / IP，计数落 `operation_logs` ⇒ 重启不清零。
            register_max_success=_get_int("REGISTER_MAX_SUCCESS", 10),
            register_window_seconds=_get_int("REGISTER_WINDOW_SECONDS", 3600),
            max_file_size_mb=_get_int("MAX_FILE_SIZE_MB", 20),
            max_rows=_get_int("MAX_ROWS", 500_000),
            max_series=_get_int("MAX_SERIES", 1000),
            task_budget_ms=_get_int("TASK_BUDGET_MS", 60_000),
            export_ttl_days=_get_int("EXPORT_TTL_DAYS", 7),
            # JT-11：错误明细临时文件的 TTL = 1 小时，独立于 7 天的正式导出产物。
            export_tmp_ttl_seconds=_get_int("EXPORT_TMP_TTL_SECONDS", 3_600),
            # JT-10：游客（user_id = 0）数据集与任务优先清理，TTL 24 小时。
            guest_data_ttl_hours=_get_int("GUEST_DATA_TTL_HOURS", 24),
            # API-07-1：内置示例数据的共享文件（不按用户复制，只建指向它的记录）。
            # 用模型目录的同级文件，与 API-07-1 的 `outputs/models/demo_sales.csv` 一致。
            demo_csv=model_dir / "demo_sales.csv",
            cors_origins=tuple(
                o.strip()
                for o in _get("CORS_ORIGINS", "http://localhost:5173").split(",")
                if o.strip()
            ),
            torch_device=_get("TORCH_DEVICE", "auto"),
            # 🔒 默认 `true`：保持 `FR-USR-08` 的「免登录试跑」链路与既有校验脚本不变。
            #    公开部署置 `false` ⇒ 整条游客链改为要求登录（见 `deps._guest_fallback`）。
            allow_guest=_get_bool("ALLOW_GUEST", True),
            # 单端口形态：由 FastAPI 直接托管前端产物（Nginx 部署时保持 false）
            serve_frontend=_get_bool("SERVE_FRONTEND", False),
            # ⚠️ 独立配置项而非硬编码 `web/dist`：部署时产物可能被放在别处，
            #    且部分部署通道会把名为 `dist` 的目录当作构建产物排除。
            frontend_dir=_resolve(base_dir, _get("FRONTEND_DIR", "web/dist")),
        )

    def ensure_secret_configured(self) -> None:
        """BE-05：JWT_SECRET 缺失或过短时启动失败（EP-2 在启动自检中调用）。"""
        if len(self.jwt_secret) < 32:
            raise RuntimeError(
                "JWT_SECRET 未配置或长度不足 32 字符。"
                "请复制 .env.example 为 .env 并设置随机密钥（BE-05）。"
            )


# 模块级单例：进程内共享，`from app.core.config import settings` 即可使用。
settings = Settings.from_env()
