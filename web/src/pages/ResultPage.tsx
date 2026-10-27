/**
 * UX-P08 · 预测结果页（**EP-6 核心交付页** ★）
 * =============================================================================
 * 依据：《UI/UX 设计文档 V1.0》§8.4 `UX-P08-01` ~ `UX-P08-13`
 *       《开发技术文档 V1.0》§5.6 `API-14` / `API-15` / `API-16`
 *       《项目开发实施方案 V1.0》§7.7 `WS-6-01` ~ `WS-6-09` / `DOD-EP6`
 *
 * 区块顺序按 `UX-P08-01`「判定结论条置于第一张卡」：
 *   ① 结论判定条 → ② 指标卡 ×4 → ③ 覆盖率面板 → ④ 折线三线对比
 *   → ⑤ 分布直方图 → ⑥ 明细分页表 → ⑦ 导出
 *
 * 🔒 **本页不计算任何指标**（`UX-P08-13`）：全部数字来自 `API-14`，
 *    与 `outputs/eval_report.json` 同源（`EP-5` 落库的 `metrics` 表）。
 *    一旦在前端「顺手四舍五入后再判定」，就会出现显示值与判定不一致。
 *
 * 🔒 **不加 `RequireAuth`**（延续 `EP-5` 决策）：`FR-USR-08` 要求游客链路
 *    跑到结果页。导出（`API-16`）由后端 `CurrentUser` 把关，前端只做
 *    「禁用 + 提示登录」，不隐藏按钮（理由见 `components/result/ExportButtons.tsx`）。
 *
 * ⚠️ **失败任务的空态**（`UX-P08-12`）：失败/未完成任务不落盘半成品，
 *    `API-14` 返回 40401。本页据此展示「半成品不落盘」说明 + 两个出口
 *    （调整参数重跑 / 返回历史任务），**不是**一个空表格。
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';

import { ApiError, predictApi } from '../api/client';
import Card from '../components/Card';
import NoticeBar from '../components/NoticeBar';
import StepBadges from '../components/StepBadges';
import CoveragePanel from '../components/result/CoveragePanel';
import DetailTable from '../components/result/DetailTable';
import DistributionChart from '../components/result/DistributionChart';
import ExportButtons from '../components/result/ExportButtons';
import LineCompare from '../components/result/LineCompare';
import { BaselineTable, MetricCards, VerdictBar } from '../components/result/MetricPanels';
import ResultAside from '../components/result/ResultAside';
import SeriesMultiSelect from '../components/result/SeriesMultiSelect';
import { useAsideContent } from '../layouts/aside-slot';
import { useAuthStore } from '../store/useAuthStore';
import { ERR } from '../types';
import type { PredictionDetailQuery, PredictionResultResponse } from '../types';

export default function ResultPage() {
  const { taskId: rawId } = useParams<{ taskId: string }>();
  const taskId = rawId && /^\d+$/.test(rawId) ? Number(rawId) : null;

  const [data, setData] = useState<PredictionResultResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeKey, setActiveKey] = useState<string | null>(null);
  /** 多选叠加（`FR-VIS-09`）：与 `activeKey` 并存 —— 单选时两者同值 */
  const [activeKeys, setActiveKeys] = useState<string[]>([]);
  const [toast, setToast] = useState<string | null>(null);

  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);

  useEffect(() => {
    if (taskId === null) {
      setLoading(false);
      return;
    }
    let alive = true;
    setLoading(true);
    predictApi
      .result(taskId)
      .then((d) => {
        if (!alive) return;
        setData(d);
        // 默认选中第一条曲线；序列为空时保持 null（图表组件会显示空态）
        setActiveKey(d.series?.[0]?.key ?? null);
        setActiveKeys(d.series?.[0]?.key ? [d.series[0].key] : []);
        setError(null);
      })
      .catch((e: unknown) => {
        if (!alive) return;
        if (e instanceof ApiError && e.code === ERR.TASK_NOT_FOUND) {
          setError('NOT_FOUND');
        } else if (e instanceof ApiError && e.code === ERR.UNAUTHORIZED) {
          setError('登录状态已失效，请重新登录后查看该任务');
        } else {
          // ⚠️ 兜底非空：空文案会让「字符串真值性」判别把错误当成"没出错"
          setError(e instanceof Error && e.message ? e.message : '结果加载失败');
        }
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [taskId]);

  const fetchDetail = useCallback(
    (id: number, query: PredictionDetailQuery) => predictApi.detail(id, query),
    [],
  );

  const aside = useMemo(
    () => (data ? <ResultAside data={data} /> : null),
    [data],
  );
  useAsideContent(aside);

  if (taskId === null) {
    return (
      <div data-testid="result-page" data-phase="invalid">
        <NoticeBar variant="danger" title="任务地址无效">
          任务 ID 必须是数字。请从 <Link to="/upload">上传数据</Link> 重新发起一次预测。
        </NoticeBar>
      </div>
    );
  }

  // ---- 空态：任务不存在 / 失败 / 无权 -------------------------------------
  /**
   * 🔴 判别必须显式与 `null` 比较（`D-06`）。
   *
   * 本页 `error` 是 `string | null`，且用 `''` 无意义 —— 若写成 `if (error)`，
   * 一旦拿到空串（`API-14` 返回 HTML 时曾出现过），错误态会被判成「没出错」，
   * 页面就永远停在下面的 loading 分支。
   */
  if (error !== null) {
    const notFound = error === 'NOT_FOUND';
    return (
      <div data-testid="result-page" data-phase="empty">
        <PageHead />
        <div className="mb-3">
          <StepBadges current="result" />
        </div>
        <NoticeBar variant={notFound ? 'warning' : 'danger'} title={notFound ? '该任务没有结果' : '结果加载失败'}>
          {notFound ? (
            <>
              失败或尚未完成的任务<strong>不落盘半成品</strong>，因此没有可展示的结果。
              请重新上传数据后重跑，或返回历史任务查看失败原因。
            </>
          ) : (
            error
          )}
        </NoticeBar>
        <div className="mt-4 flex flex-wrap gap-2">
          {/*
            🔴 **不要拼 `/params?datasetId=`**（`D-03`）：
              ① 本分支下 `data` 恒为 `null`（`setData` 只在成功路径执行），
                 `data?.dataset.id` 永远取不到值 → 链接必然缺参；
              ② 参数名也曾写成 `dataset_id`，而 `ParamsPage` 只认 camelCase 的
                 `datasetId`（`params.get('datasetId')`）。
              两者叠加 ⇒ 点「调整参数重跑」100% 落到参数页的「缺少 datasetId 参数」报错态。
              此刻我们确实拿不到 datasetId（`API-14` 的失败响应只带 task_id），
              故如实指向上传页，不假装能直达参数页。
          */}
          <Link
            to="/upload"
            className="rounded-[var(--radius-sm)] px-3.5 py-2 text-[0.8125rem] font-semibold"
            style={{ background: 'var(--primary)', color: 'var(--on-primary)' }}
            data-testid="empty-rerun"
          >
            重新上传数据重跑
          </Link>
          <Link
            to="/history"
            className="rounded-[var(--radius-sm)] border px-3.5 py-2 text-[0.8125rem] font-semibold"
            style={{ borderColor: 'var(--border-field)', color: 'var(--text)' }}
            data-testid="empty-history"
          >
            返回历史任务
          </Link>
        </div>
      </div>
    );
  }

  if (loading || !data) {
    return (
      <div data-testid="result-page" data-phase="loading">
        <PageHead />
        <div className="mb-3">
          <StepBadges current="result" />
        </div>
        <p className="text-[0.8125rem]" style={{ color: 'var(--text-3)' }} data-testid="result-loading">
          正在加载预测结果…
        </p>
      </div>
    );
  }

  const series = data.series ?? [];

  return (
    <div data-testid="result-page" data-phase="ready">
      <PageHead
        taskName={data.task.task_name}
        horizon={data.task.horizon}
        device={data.task.device}
        fallback={data.task.device_fallback}
      />
      <div className="mb-3">
        <StepBadges current="result" />
      </div>

      {toast && (
        <div className="mb-3">
          <NoticeBar variant="warning" title="提示">
            {toast}
          </NoticeBar>
        </div>
      )}

      {/* ① 结论判定条（UX-P08-01：第一张卡） */}
      <div className="flex flex-col gap-4">
        <VerdictBar metrics={data.metrics} isOverThreshold={data.is_over_threshold} />

        {/* 🔒 超标警告：不可折叠（FR-VIS-06 / UX-AC-09）
            刻意不用 <details>：可折叠 = 用户可以视而不见，
            而超标是「不能拿去备货」的硬提示，必须一直挂在视野里。 */}
        {data.is_over_threshold && (
          <NoticeBar variant="danger" title={`加权 MAPE ${data.metrics.weighted_mape?.toFixed(2) ?? '—'}% 超过 20% 阈值`}>
            本次结果的加权 MAPE 为 {data.metrics.weighted_mape?.toFixed(2) ?? '—'}%，未达到 ≤ 20% 的硬指标要求。
            常见原因：序列波动大、营销节点密集，或数据末尾存在异常值。
            建议缩短预测步长、剔除异常日期后重跑，或仅将本结果作为参考而非备货依据。
          </NoticeBar>
        )}

        {/* ② 指标卡 ×4（UX-P08-02） */}
        <Card title="核心指标" testId="metrics-card-block">
          <MetricCards
            metrics={data.metrics}
            l1Count={data.coverage.L1}
            detailTotal={data.detail_total}
          />
        </Card>

        {/* ③ 覆盖率面板（UX-P08-04，📌 必展示） */}
        <Card title="序列分级覆盖率" testId="coverage-card">
          <CoveragePanel coverage={data.coverage} />
        </Card>

        {/* ④ 折线三线对比（UX-P08-05） */}
        <Card
          title="销量趋势对比"
          testId="curve-card"
          badge={
            data.truncated ? (
              <span
                className="rounded-full px-2 py-[2px] text-[0.6875rem]"
                style={{ background: 'var(--warning-soft)', color: 'var(--on-warning-soft)' }}
                data-testid="series-truncated"
              >
                仅展示前 {series.length} 条
              </span>
            ) : null
          }
        >
          {series.length > 1 && (
            <div className="mb-3">
              <label className="flex flex-col gap-1">
                <span className="text-[0.75rem]" style={{ color: 'var(--text-2)' }}>
                  选择序列（共 {series.length} 条，可搜索多选）
                </span>
                {/*
                  🔒 `FR-VIS-09`：多 SKU / 多仓库曲线的**叠加**与筛选，
                     要求「下拉多选 + 搜索」。原实现只有一个单选 `<select>`，
                     与本条需求不符（技术文档 §6.5.2 还自称已实现 —— 属文档失真）。
                */}
                <SeriesMultiSelect
                  series={series}
                  value={activeKeys}
                  onChange={(keys) => {
                    setActiveKeys(keys);
                    setActiveKey(keys[keys.length - 1] ?? null);
                  }}
                  onLimit={(limit) =>
                    setToast(`最多同时对比 ${limit} 条序列，已按前 ${limit} 条截断。`)
                  }
                />
              </label>
            </div>
          )}
          <LineCompare series={series} activeKey={activeKey} activeKeys={activeKeys} />
          {data.truncated && (
            <p className="mt-2 text-[0.75rem]" style={{ color: 'var(--text-3)' }} data-testid="series-truncated-note">
              共 {data.coverage.total} 条序列，接口默认内联前 50 条以控制响应体积；
              完整数据请在下方明细表中按商品 / 仓库筛选查看，或导出 CSV / Excel。
            </p>
          )}
        </Card>

        {/* ⑤ 分布直方图（FR-VIS-08） */}
        <Card title="逐序列误差分布" testId="distribution-card">
          <DistributionChart distribution={data.distribution} />
        </Card>

        {/* 基线对照表（UX-P08-03） */}
        <Card title="与移动平均基线对照" testId="baseline-card">
          <BaselineTable metrics={data.metrics} />
        </Card>

        {/* ⑥ 明细分页表（FR-VIS-03 / UX-P08-07） */}
        <Card title="结果明细" testId="detail-card">
          <DetailTable
            taskId={taskId}
            detailTotal={data.detail_total}
            fetcher={fetchDetail}
            onError={setToast}
          />
        </Card>

        {/* ⑦ 评估前提声明（FR-MOD-13 / UX-P08-10，🔒 必须原样展示） */}
        <NoticeBar variant="info" title="模型适用性说明">
          <span data-testid="eval-note">{data.eval_note}</span>
        </NoticeBar>

        {/* 导航出口 */}
        <div className="flex flex-wrap gap-2">
          <Link
            to={`/running/${taskId}`}
            className="rounded-[var(--radius-sm)] border px-3.5 py-2 text-[0.8125rem] font-semibold"
            style={{ borderColor: 'var(--border-field)', color: 'var(--text-2)' }}
          >
            返回执行详情
          </Link>
          <Link
            to="/history"
            className="rounded-[var(--radius-sm)] border px-3.5 py-2 text-[0.8125rem] font-semibold"
            style={{ borderColor: 'var(--border-field)', color: 'var(--text-2)' }}
          >
            历史任务
          </Link>
          <Link
            to="/upload"
            className="rounded-[var(--radius-sm)] px-3.5 py-2 text-[0.8125rem] font-semibold"
            style={{ background: 'none', color: 'var(--primary)' }}
          >
            再预测一份数据
          </Link>
        </div>
      </div>

      {/* 导出：放在卡片流之外但视觉上属于结果区块 */}
      <div className="mt-4 flex justify-end">
        <ExportButtons
          taskId={taskId}
          detailTotal={data.detail_total}
          isAuthenticated={isAuthenticated}
          onNeedLogin={() =>
            setToast('导出需要登录。游客可以查看完整结果，登录后即可导出 CSV / Excel。')
          }
          onError={setToast}
        />
      </div>
    </div>
  );
}

/** 页头：面包屑 + 标题 + 任务徽标 */
function PageHead(props?: {
  taskName?: string | null;
  horizon?: number;
  device?: string;
  fallback?: number;
}) {
  return (
    <div className="mb-4">
      <div className="text-[0.75rem]" style={{ color: 'var(--text-3)' }}>
        工作台 / 新建预测 / 预测结果
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-2">
        <h1 className="text-[1.5rem]">预测结果</h1>
        {props?.horizon !== undefined && (
          <span
            className="rounded-full px-2 py-[2px] text-[0.6875rem]"
            style={{ background: 'var(--primary-soft)', color: 'var(--on-primary-soft)' }}
            data-testid="result-horizon-badge"
          >
            未来 {props.horizon} 天
          </span>
        )}
        {props?.fallback ? (
          <span
            className="rounded-full px-2 py-[2px] text-[0.6875rem]"
            style={{ background: 'var(--warning-soft)', color: 'var(--on-warning-soft)' }}
            data-testid="result-fallback-badge"
          >
            GPU→CPU 已回退
          </span>
        ) : null}
      </div>
      {props?.taskName ? (
        <p className="mt-1 text-[0.8125rem]" style={{ color: 'var(--text-2)' }}>
          {props.taskName}
        </p>
      ) : null}
    </div>
  );
}
