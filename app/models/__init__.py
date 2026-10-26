"""ORM 实体包（9 个，与 `code/db/schema.sql` 一一对应）。

| 实体 | 表 | 数据库设计文档编号 |
|---|---|---|
| `User` | `users` | `DB-T01` |
| `Dataset` | `datasets` | `DB-T02` |
| `Task` | `tasks` | `DB-T03` |
| `Prediction` | `predictions` | `DB-T04` |
| `Metric` | `metrics` | `DB-T05` |
| `ModelRegistry` | `model_registry` | `DB-T06` |
| `OperationLog` | `operation_logs` | `DB-T07` |
| `Event` | `events` | `DB-T08` |
| `RevokedToken` | `revoked_tokens` | `DB-T09` |

> 视图 `v_task_summary`（`VW-01`）**不建 ORM 实体**——它由 `schema.sql` 创建，
> 供历史列表与结果页头部只读查询使用（见 `app/dao/task_dao.py` 的只读查询）。

🔒 本包不 import algo（架构红线 3：分层依赖单向 `api → services → dao → models`）。
"""
from app.models.dataset import Dataset
from app.models.event import Event
from app.models.metric import Metric
from app.models.model_registry import ModelRegistry
from app.models.operation_log import OperationLog
from app.models.prediction import Prediction
from app.models.revoked_token import RevokedToken
from app.models.task import Task
from app.models.user import User

__all__ = [
    "User",
    "Dataset",
    "Task",
    "Prediction",
    "Metric",
    "ModelRegistry",
    "OperationLog",
    "Event",
    "RevokedToken",
]
