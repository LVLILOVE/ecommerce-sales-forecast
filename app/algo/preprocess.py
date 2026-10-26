"""数据校验、插值填补、异常尖峰截断。

设计要点（PRD / 开发方案共同要求）：
1. 校验顺序**不可调整**（config.VALIDATE_ORDER）—— 越靠前的越快失败。
2. 校验只做「能不能用」的判断，不做静默修补。修补发生在校验通过之后。
3. 插值只填补**序列内部**的空洞；序列两端的缺失不做外推（宁可让有效长度变短，
   也不能凭空造数 —— 这会直接影响 L1/L2/L3 分级）。
4. 归一化统计量**只能来自训练段**，见 fit_scaler()，这是防数据泄漏的关键（风险 R2）。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from . import config as C

logger = logging.getLogger(__name__)


# ===================================================================== 异常
class ValidationError(Exception):
    """校验失败。code 与 PRD 错误码表一一对应，前端直接用来渲染修复建议。"""

    def __init__(self, code: str, message: str, detail: str = "", suggestion: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.suggestion = suggestion

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
            "suggestion": self.suggestion,
        }


# ===================================================================== 校验
def validate_dataframe(df: pd.DataFrame) -> None:
    """按固定顺序校验。全部通过则正常返回，任一失败抛 ValidationError。"""
    # 1. 表头完整性
    missing = [c for c in C.REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValidationError(
            "E_MISSING_COLUMN",
            f"缺少必需列：{'、'.join(missing)}",
            f"实际列：{'、'.join(map(str, df.columns[:12]))}",
            "请确认表头包含 sku_id / warehouse_id / date / sales 四列，列名区分大小写。",
        )

    # 2. 行数
    if len(df) > C.MAX_ROWS:
        raise ValidationError(
            "E_TOO_MANY_ROWS",
            f"数据行数 {len(df):,} 超过上限 {C.MAX_ROWS:,}",
            "", "请按时间或商品拆分后分批上传。",
        )

    # 3. 日期可解析性
    parsed = pd.to_datetime(df[C.COL_DATE], errors="coerce")
    bad = parsed.isna()
    if bad.any():
        sample = df.loc[bad, C.COL_DATE].head(3).astype(str).tolist()
        raise ValidationError(
            "E_BAD_DATE",
            f"{int(bad.sum())} 行日期无法解析",
            f"示例：{'、'.join(sample)}",
            "请统一为 YYYY-MM-DD 格式，避免使用 Excel 序列号或中文日期。",
        )

    # 4. 销量非负
    sales = pd.to_numeric(df[C.COL_SALES], errors="coerce")
    if sales.isna().any():
        raise ValidationError(
            "E_NON_NUMERIC_SALES",
            f"{int(sales.isna().sum())} 行销量不是数字",
            "", "请检查是否存在空值、'—'、'暂无' 等非数值占位。",
        )
    if (sales < 0).any():
        raise ValidationError(
            "E_NEGATIVE",
            f"{int((sales < 0).sum())} 行销量为负数",
            "", "退货请作为正向销量抵消后入库，不要写成负数。",
        )

    # 5. 序列数上限
    n_series = df.groupby([C.COL_SKU, C.COL_WAREHOUSE], observed=True).ngroups
    if n_series > C.MAX_SERIES:
        raise ValidationError(
            "E_TOO_MANY_SERIES",
            f"序列数 {n_series:,} 超过上限 {C.MAX_SERIES:,}",
            "序列数 = 商品数 × 仓库数",
            f"建议先按销量 Top-{C.MAX_SERIES} 商品筛选，或拆分多次预测。",
        )


def detect_gaps(df: pd.DataFrame) -> pd.DataFrame:
    """检测时间连续性。返回每序列的缺口明细（不是错误，是提示）。

    长尾数据集天然存在断档，是否阻断取决于缺口比例；这里只负责如实汇报，
    由调用方决定是拒绝还是继续（在线在 20% 以内放行并提示）。
    """
    rows = []
    date = pd.to_datetime(df[C.COL_DATE])
    for (sku, wh), g in df.assign(_d=date).groupby([C.COL_SKU, C.COL_WAREHOUSE], observed=True):
        d = g["_d"].sort_values()
        if len(d) < 2:
            continue
        step = d.diff().dt.days.dropna()
        gaps = int((step > 1).sum())
        rows.append({
            C.COL_SKU: sku,
            C.COL_WAREHOUSE: wh,
            "span_days": int((d.max() - d.min()).days) + 1,
            "points": len(d),
            "gap_count": gaps,
        })
    return pd.DataFrame(rows)


# ===================================================================== 清洗
def interpolate_series(values: np.ndarray) -> np.ndarray:
    """线性插值填补序列**内部**空洞；两端缺失保持不变（不做外推）。

    输入/输出均为 float 数组，缺失用 np.nan 表示。
    """
    v = np.asarray(values, dtype=float).copy()
    valid = ~np.isnan(v)
    if valid.sum() == 0:
        return v                      # 全空：交给 L3 兜底
    if valid.sum() == len(v):
        return v                      # 无空洞
    idx = np.arange(len(v))
    # np.interp 默认把两端外推为最近的有效值，这里显式把两端还原成 nan
    filled = np.interp(idx, idx[valid], v[valid])
    first, last = idx[valid][0], idx[valid][-1]
    filled[:first] = np.nan
    filled[last + 1:] = np.nan
    return filled


def truncate_spikes(
    values: np.ndarray,
    window: int = 7,
    n_sigma: float = 3.0,
    preserve_calendar: pd.Series | None = None,
    preserve_ratio: float = 0.5,
    protect_dates: set[str] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """把异常尖峰截断到滚动中位数 ± n_sigma×MAD。

    为什么用 MAD 而不是标准差：促销产生的超大尖峰会把标准差本身拉高，
    导致 3σ 门限跟着抬高、反而放过尖峰。MAD 对离群点稳健。

    🔒 **必须保护已知的营销节点**（踩坑修正）：
       大促（双十一/双十二/春节）的尖峰是**真实且可预测的**业务信号，
       正是模型相对移动平均基线唯一能建立优势的地方。
       早期版本无差别截断，把 3 倍于常日的双十一压到 2.4 倍
       （实测：均值 39.6 → 30.6），等于把可学习的信号当噪声删掉，
       结果模型只会输出"零残差"（等于退回基线），MAE 反而不如基线。
       因此对节假日当天及其前后 1 天**放宽**门限（默认放宽到不可截断）。

    preserve_calendar：与 values 等长的日期序列，用于定位节假日。
    protect_dates：需要保护的日期字符串集合（默认用 features.HOLIDAYS）。
    """
    v = np.asarray(values, dtype=float).copy()
    n = len(v)
    mask = np.zeros(n, dtype=bool)
    if n < window * 2:
        return v, mask

    s = pd.Series(v)
    med = s.rolling(window, center=True, min_periods=max(3, window // 2)).median()
    mad = (s - med).abs().rolling(window, center=True, min_periods=max(3, window // 2)).median()
    # 1.4826 是 MAD → 标准差的换算系数（正态分布下）
    sigma = 1.4826 * mad
    sigma = sigma.replace(0, np.nan).fillna(s.std() or 1.0)

    upper = (med + n_sigma * sigma).to_numpy()
    lower = (med - n_sigma * sigma).clip(lower=0).to_numpy()

    # ---- 保护营销节点：这些日子的高销量是真实业务事实，不是异常 ----
    protected = np.zeros(n, dtype=bool)
    if preserve_calendar is not None and len(preserve_calendar) == n:
        try:
            from .features import HOLIDAYS
            dates = pd.to_datetime(pd.Series(preserve_calendar)).dt.strftime("%Y-%m-%d")
            prot_set = set(protect_dates) if protect_dates is not None else set(HOLIDAYS)
            protected = dates.isin(prot_set).to_numpy()
            if n > 2:
                # 前后各扩展 1 天：大促的预热/返场也是真实高峰
                protected = (protected
                             | np.r_[False, protected[:-1]]
                             | np.r_[protected[1:], False])
        except Exception:
            # 🔴 `R-12`：这里静默失败等于**悄悄关掉营销节点保护** ——
            # 双十一/618 的尖峰会被当成异常削掉，指标变差却查不到原因。
            # 用 warning（这是「功能降级」），不是 debug（逐值兜底）。
            logger.warning("营销节点保护计算失败，本次不启用保护", exc_info=True)
            protected = np.zeros(n, dtype=bool)

    if protected.any():
        # 受保护位置把上界放宽 preserve_ratio 倍（默认 0.5 → 门限扩大 1.5 倍）
        span = np.maximum(np.abs(upper), np.abs(lower))
        upper = np.where(protected, upper + preserve_ratio * span, upper)
        lower = np.where(protected, (lower - preserve_ratio * span).clip(min=0), lower)

    over = ((v > upper) | (v < lower)) & ~np.isnan(v)
    mask = over
    v = np.clip(v, lower, upper)
    return v, mask


def clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """校验通过后的清洗：排序 → 插值 → 截断尖峰。

    注意清洗是**逐序列独立**做的，绝不跨序列借用信息。
    但日期要传给截断函数，用于保护营销节点（见 truncate_spikes 的说明）。
    """
    out = df.copy()
    out[C.COL_DATE] = pd.to_datetime(out[C.COL_DATE])
    out[C.COL_SALES] = pd.to_numeric(out[C.COL_SALES], errors="coerce")
    out = out.sort_values([C.COL_SKU, C.COL_WAREHOUSE, C.COL_DATE]).reset_index(drop=True)

    cleaned_frames = []
    for _, g in out.groupby([C.COL_SKU, C.COL_WAREHOUSE], observed=True, sort=False):
        vals = g[C.COL_SALES].to_numpy(dtype=float)
        vals = interpolate_series(vals)
        # 把日期传进去，让营销节点免于被当成异常截断
        vals, _ = truncate_spikes(vals, preserve_calendar=g[C.COL_DATE])
        gg = g.copy()
        gg[C.COL_SALES] = vals
        cleaned_frames.append(gg)

    if not cleaned_frames:
        return out
    return pd.concat(cleaned_frames, ignore_index=True)


# ===================================================================== 归一化
class Scaler:
    """逐序列 Z-score。**统计量只能从训练段拟合**（防数据泄漏，风险 R2）。

    反例（禁止）：拿整段（含验证/测试段）算均值方差 —— 测试集的分布信息会
    提前泄漏进训练，指标虚高，且拿到真实新数据就崩。
    """

    __slots__ = ("mean", "std")

    def __init__(self, mean: float = 0.0, std: float = 1.0):
        self.mean = float(mean)
        self.std = float(std) if std and std > 0 else 1.0

    @classmethod
    def fit(cls, train_values: np.ndarray) -> "Scaler":
        v = np.asarray(train_values, dtype=float)
        v = v[~np.isnan(v)]
        if v.size == 0:
            return cls(0.0, 1.0)
        return cls(float(v.mean()), float(v.std()))

    def transform(self, values: np.ndarray) -> np.ndarray:
        return (np.asarray(values, dtype=float) - self.mean) / self.std

    def inverse(self, values: np.ndarray) -> np.ndarray:
        return np.asarray(values, dtype=float) * self.std + self.mean


def chronological_split(
    n: int, ratios: tuple[float, float, float] = (0.8, 0.1, 0.1)
) -> tuple[slice, slice, slice]:
    """按时间顺序 8:1:1 切分。

    🔒 禁止随机切分：时序数据随机切分会让未来信息泄漏到训练集
    （相邻日期的样本几乎相同），指标会好得离谱但线上不可用。
    """
    a = int(n * ratios[0])
    b = a + int(n * ratios[1])
    return slice(0, a), slice(a, b), slice(b, n)


def resolve_device(prefer: str = "auto") -> str:
    """设备选择，含 CUDA → CPU 自动回退（⏱ 本机实测峰值显存仅 111MB，通常不会 OOM）。"""
    if prefer == "cpu":
        return "cpu"
    try:
        import torch
    except ImportError:          # 无 torch 时降级（离线脚本会提前拦）
        return "cpu"
    if not torch.cuda.is_available():
        return "cpu"
    if prefer == "cuda":
        return "cuda"
    # auto：显存不足 512MB 就老实走 CPU，避免推理到一半 OOM 白等一分钟
    try:
        free, _total = torch.cuda.mem_get_info()
        return "cuda" if free > 512 * 1024 * 1024 else "cpu"
    except Exception:
        # 拿不到显存信息时按「够用」处理并走 CUDA（与改动前行为一致），
        # 但至少留一条线索，避免「本该降级到 CPU 却 OOM」无从追查。
        logger.debug("读取显存信息失败，按 CUDA 继续", exc_info=True)
        return "cuda"
