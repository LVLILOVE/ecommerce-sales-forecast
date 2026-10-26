"""pytest 全局夹具。

🔒 **最小依赖原则**：这里只把 `app` 需要的路径/密钥指向**临时目录**，
   不建库、不加载模型、不起服务。因此整套单测是「毫秒级 + 零外部状态」，
   可以在 CI 的干净 runner 上直接跑。

⚠️ 环境变量必须在**导入 `app.*` 之前**设置：`app.core.config` 是在导入期读
   `os.environ` 的（与运行期行为一致），导入后再设已经晚了。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sp-unit-"))

# `setdefault`：允许外部（CI / 本地）覆盖，但默认保证有一份可用值
os.environ.setdefault("DB_PATH", str(_TMP / "unit.db"))
os.environ.setdefault("UPLOAD_DIR", str(_TMP / "uploads"))
os.environ.setdefault("EXPORT_DIR", str(_TMP / "exports"))
os.environ.setdefault("JWT_SECRET", "unit-test-secret-0123456789abcdefghij")
os.environ.setdefault("APP_ENV", "test")


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """收尾清理临时目录（失败不影响测试结论）。"""
    import shutil

    shutil.rmtree(_TMP, ignore_errors=True)
