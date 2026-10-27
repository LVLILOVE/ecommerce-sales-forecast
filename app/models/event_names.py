"""埋点事件名常量（`PRD §5.1` 的 16 个 `EV-*`）。

为什么要有这个文件（2026-10-27 复评修复 `FR-ADM-01`）
-----------------------------------------------------------------------------
`event_dao.record_safe()` 早就写好了、`events` 表也建了、管理端看板的
「埋点概览」（`API-22`）也早就接了 `count_by_name()` ——
但**全仓没有一处调用 `record_safe`**，16 个事件名只存在于 PRD 的表格里。
结果是看板上那个「埋点概览」永远返回空字典，而验收时会直接问
「PRD 说 16 个事件，数据呢？」

🔒 **存编号而不是英文名**：`event_name` 列存 `EV-PRED-02` 这种编号。
理由：
1. 编号是 PRD 表格的**主键**，看板按编号展示可以直接和 PRD 对照；
2. 编号稳定 —— 事后想改英文名（`prediction_complete` → `task_succeeded`）
   不会让历史数据分裂成两个 key。

`EVENT_LABELS` 提供给人看的中文说明，管理端展示时用。
"""
from __future__ import annotations

from typing import Final

# ---- 认证（`EV-AUTH-*`）----------------------------------------------------
EV_AUTH_01_REGISTER: Final = "EV-AUTH-01"
EV_AUTH_02_LOGIN: Final = "EV-AUTH-02"
EV_AUTH_03_LOGIN_FAIL: Final = "EV-AUTH-03"

# ---- 数据集（`EV-DAT-*`）---------------------------------------------------
EV_DAT_01_UPLOAD: Final = "EV-DAT-01"
EV_DAT_02_VALIDATE_FAIL: Final = "EV-DAT-02"
EV_DAT_03_SAMPLE_LOAD: Final = "EV-DAT-03"

# ---- 预测（`EV-PRED-*`）----------------------------------------------------
EV_PRED_01_SUBMIT: Final = "EV-PRED-01"
EV_PRED_02_COMPLETE: Final = "EV-PRED-02"
EV_PRED_03_FAIL: Final = "EV-PRED-03"
EV_PRED_04_TIMEOUT: Final = "EV-PRED-04"
EV_PRED_05_DEVICE_FALLBACK: Final = "EV-PRED-05"

# ---- 可视化（`EV-VIS-*`）---------------------------------------------------
EV_VIS_01_RESULT_VIEW: Final = "EV-VIS-01"
EV_VIS_02_RESULT_EXPORT: Final = "EV-VIS-02"
EV_VIS_03_COVERAGE_VIEW: Final = "EV-VIS-03"

# ---- 历史（`EV-HIS-*`）-----------------------------------------------------
EV_HIS_01_HISTORY_VIEW: Final = "EV-HIS-01"
EV_HIS_02_HISTORY_COMPARE: Final = "EV-HIS-02"

#: 全部 16 个事件（顺序与 `PRD §5.1` 表格一致）。用于自检与文档生成。
ALL_EVENTS: Final[tuple[str, ...]] = (
    EV_AUTH_01_REGISTER, EV_AUTH_02_LOGIN, EV_AUTH_03_LOGIN_FAIL,
    EV_DAT_01_UPLOAD, EV_DAT_02_VALIDATE_FAIL, EV_DAT_03_SAMPLE_LOAD,
    EV_PRED_01_SUBMIT, EV_PRED_02_COMPLETE, EV_PRED_03_FAIL,
    EV_PRED_04_TIMEOUT, EV_PRED_05_DEVICE_FALLBACK,
    EV_VIS_01_RESULT_VIEW, EV_VIS_02_RESULT_EXPORT, EV_VIS_03_COVERAGE_VIEW,
    EV_HIS_01_HISTORY_VIEW, EV_HIS_02_HISTORY_COMPARE,
)

#: 编号 → 中文说明（管理端「埋点概览」展示用）
EVENT_LABELS: Final[dict[str, str]] = {
    EV_AUTH_01_REGISTER: "注册成功",
    EV_AUTH_02_LOGIN: "登录成功",
    EV_AUTH_03_LOGIN_FAIL: "登录失败",
    EV_DAT_01_UPLOAD: "文件上传完成",
    EV_DAT_02_VALIDATE_FAIL: "数据校验失败",
    EV_DAT_03_SAMPLE_LOAD: "加载示例数据",
    EV_PRED_01_SUBMIT: "提交预测任务",
    EV_PRED_02_COMPLETE: "任务成功结束",
    EV_PRED_03_FAIL: "任务失败",
    EV_PRED_04_TIMEOUT: "超时软失败",
    EV_PRED_05_DEVICE_FALLBACK: "CUDA→CPU 回退",
    EV_VIS_01_RESULT_VIEW: "结果页曝光",
    EV_VIS_02_RESULT_EXPORT: "结果导出",
    EV_VIS_03_COVERAGE_VIEW: "覆盖率面板曝光",
    EV_HIS_01_HISTORY_VIEW: "历史列表访问",
    EV_HIS_02_HISTORY_COMPARE: "跨任务对比",
}

__all__ = [
    "EV_AUTH_01_REGISTER", "EV_AUTH_02_LOGIN", "EV_AUTH_03_LOGIN_FAIL",
    "EV_DAT_01_UPLOAD", "EV_DAT_02_VALIDATE_FAIL", "EV_DAT_03_SAMPLE_LOAD",
    "EV_PRED_01_SUBMIT", "EV_PRED_02_COMPLETE", "EV_PRED_03_FAIL",
    "EV_PRED_04_TIMEOUT", "EV_PRED_05_DEVICE_FALLBACK",
    "EV_VIS_01_RESULT_VIEW", "EV_VIS_02_RESULT_EXPORT", "EV_VIS_03_COVERAGE_VIEW",
    "EV_HIS_01_HISTORY_VIEW", "EV_HIS_02_HISTORY_COMPARE",
    "ALL_EVENTS", "EVENT_LABELS",
]
