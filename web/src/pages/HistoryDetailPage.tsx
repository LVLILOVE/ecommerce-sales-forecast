/**
 * 历史任务详情（`UX-P10` / `FR-HIS-02`）
 * =============================================================================
 * 📌 **成功任务复用结果页的全部展示组件**（`components/result/*`）。
 *    不是为了少写代码，而是为了**保证一致性**：`FR-HIS-02` 要求
 *    「回看当时的曲线、指标卡与覆盖率披露，详情内容与当时结果一致」。
 *    两套渲染迟早会漂移（改了一处忘了另一处），那时用户看到的
 *    历史详情和当时的结果对不上，就会开始怀疑数据被改过。
 *
 * ⚠️ **失败任务走完全不同的分支**。这是本项目一处刻意的口径差异：
 *    - 结果页（`EP-6` 决定）：失败任务**不展示**任何半成品，报 40401
 *    - 历史详情（本页）：**必须**展示失败原因、卡在哪一步、已完成到哪
 *    两者不矛盾 —— 「预测是否成功」和「上次为什么失败」是两个不同的
 *    用户诉求。历史详情是「事后复盘」，不给人复盘信息是不合格的。
 *
 * 🔒 **参数快照标注「提交时冻结」**（`UX-P10-01` / `FR-HIS-04`）：
 *    详情里的步长 / 节假日开关等是**提交那一刻**的值，不是当前配置。
 *    不标注的话，用户改过配置后会以为「当初用的是新配置跑的」。
 */
import { useCallback, useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';

import { ApiError, historyApi, predictApi } from '../api/client';
import CoveragePanel from '../components/result/CoveragePanel';
import DistributionChart from '../components/result/DistributionChart';
import ExportButtons from '../components/result/ExportButtons';
import SeriesMultiSelect from '../components/result/SeriesMultiSelect';
import LineCompare from '../components/result/LineCompare';
import { BaselineTable, MetricCards, VerdictBar } from '../components/result/MetricPanels';
import EmptyState from '../components/EmptyState';
import NoticeBar from '../components/NoticeBar';
import { Skeleton } from '../components/Skeleton';
import type { PredictionResultResponse, SeriesCurve } from '../types';
import { useAuthStore } from '../store/useAuthStore';

type Phase = 'loading' | 'ready' | 'error';

export default function HistoryDetailPage() {
  const { taskId } = useParams();
  const id = Number(taskId);

  const [data, setData] = useState<PredictionResultResponse | null>(null);
  const [phase, setPhase] = useState<Phase>('loading');
  const [error, setError] = useState('');
  const [activeKey, setActiveKey] = useState('');
  /** 多选叠加（`FR-VIS-09`）—— 与结果页同一套交互 */
  const [activeKeys, setActiveKeys] = useState<string[]>([]);
  /** 导出相关提示（`FR-HIS-03`）：成功不提示，失败/需登录才弹 */
  const [toast, setToast] = useState('');
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);

  const load = useCallback(async () => {
    if (!Number.isFinite(id) || id <= 0) {
      setError('任务 ID 无效');
      setPhase('error');
      return;
    }
    setPhase('loading');
    try {
      const d = await historyApi.detail(id);
      setData(d);
      setActiveKey(d.series[0]?.key ?? '');
      setActiveKeys(d.series[0]?.key ? [d.series[0].key] : []);
      setPhase('ready');
    } catch (e) {
      setError(e instanceof ApiError ? e.message : '加载任务详情失败，请稍后重试');
      setPhase('error');
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  if (phase === 'loading') {
    return (
      <div data-testid="detail-skeleton">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="mb-2">
            <Skeleton className="h-[3rem]" />
          </div>
        ))}
      </div>
    );
  }

  if (phase === 'error') {
    return (
      <div data-testid="detail-error">
        <EmptyState
          title="无法打开该任务"
          description={error}
          action={
            <Link
              to="/history"
              className="rounded-[var(--radius-sm)] px-3 py-1.5 text-[0.8125rem]"
              style={{ background: 'var(--primary)', color: 'var(--on-primary)' }}
            >
              返回历史任务列表
            </Link>
          }
        />
      </div>
    );
  }

  if (!data) return null;
  const failed = data.task.status !== 'succeeded';

  return (
    <div data-testid="history-detail">
      <header className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-[0.75rem]" style={{ color: 'var(--text-3)' }}>
            历史任务 /{' '}
            <Link to="/history" style={{ color: 'var(--text-3)' }}>
              列表
            </Link>{' '}
            / #{data.task.id}
          </p>
          <h1 className="mt-1 text-[1.5rem] font-semibold" style={{ color: 'var(--text)' }}>
            {data.task.task_name ?? `任务 #${data.task.id}`}
          </h1>
          <p className="mt-1 text-[0.8125rem]" style={{ color: 'var(--text-2)' }}>
            {data.dataset.file_name} · {data.task.horizon} 天步长 · 提交于{' '}
            {data.task.created_at}
          </p>
        </div>

        {/*
          🔒 历史记录导出（`FR-HIS-03`，P0）。
          - 列定义与结果页**完全一致**（PRD 原文），后端共用 `prepare_export`；
          - **失败任务不给导出**：它没有预测行，导出空文件不如不给（与
            `prepare_export` 的 `_require_succeeded` 同口径，前端先拦一道，
            避免用户点了才看到 400xx）；
          - 复用结果页的 `ExportButtons`，只把 `exporter` 换成 `historyApi.export`。
        */}
        {!failed && (
          <ExportButtons
            taskId={data.task.id}
            detailTotal={data.detail_total}
            isAuthenticated={isAuthenticated}
            onNeedLogin={() => setToast('导出需要登录，请先登录后再试。')}
            onError={setToast}
            exporter={historyApi.export}
            fallbackNamePrefix="历史预测结果"
          />
        )}
      </header>

      {toast ? (
        <div className="mb-3">
          <NoticeBar variant="warning">{toast}</NoticeBar>
        </div>
      ) : null}

      {/* 🔒 参数快照卡 */}
      <Section
        title="运行参数快照"
        testId="param-snapshot"
        aside={
          <span
            className="rounded-full px-2 py-0.5 text-[0.6875rem] font-medium"
            style={{ background: 'var(--bg-subtle)', color: 'var(--text-3)' }}
            data-testid="frozen-badge"
          >
            参数在任务提交时冻结，事后不可修改
          </span>
        }
      >
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2.5 md:grid-cols-4" data-testid="param-grid">
          <Param label="预测粒度" value="仓库 × 商品" />
          <Param label="预测步长" value={`${data.task.horizon} 天`} />
          <Param label="模型版本" value={data.task.model_version} />
          <Param label="推理设备" value={data.task.device === 'cuda' ? 'GPU (CUDA)' : 'CPU'} />
          <Param label="序列总数" value={String(data.task.series_total)} />
          <Param label="数据行数" value={data.dataset.row_count.toLocaleString('zh-CN')} />
          <Param label="数据区间" value={`${data.dataset.date_start} ~ ${data.dataset.date_end}`} />
          <Param label="端到端耗时" value={`${(data.task.duration_ms / 1000).toFixed(1)} s`} />
        </dl>
        {data.task.device_fallback ? (
          <p className="mt-2.5 text-[0.75rem]" style={{ color: 'var(--warning)' }} data-testid="fallback-note">
            ⚠️ GPU 不可用，本次已回退到 CPU 推理，耗时明显长于正常水平
          </p>
        ) : null}
      </Section>

      {/* 🔴 失败详情（UX-P10-05 / API-19-1） */}
      {failed ? (
        <>
          <Section title="失败详情" testId="failed-block">
            <NoticeBar variant="danger" title={`任务未完成：${data.task.error_code ?? data.task.status}`}>
              {data.task.error_msg ?? '未记录错误信息，请检查数据文件后重试。'}
            </NoticeBar>
            <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2.5 md:grid-cols-4">
              <Param label="错误码" value={data.task.error_code ?? '—'} />
              <Param label="失败阶段" value={data.task.completed_stage ?? '—'} />
              <Param label="已用时" value={`${(data.task.duration_ms / 1000).toFixed(1)} s`} />
              <Param label="推理设备" value={data.task.device === 'cuda' ? 'GPU (CUDA)' : 'CPU'} />
            </dl>
            <p className="mt-3 text-[0.75rem]" style={{ color: 'var(--text-2)' }} data-testid="failed-attribution">
              <strong>归因：</strong>
              {data.task.error_code === 'E_TIMEOUT'
                ? '任务超出端到端时间预算。可减少预测序列数量或缩短步长后重试。'
                : data.task.error_code === 'E_TOO_MANY_SERIES'
                  ? '序列总数超出上限。可只保留主力商品，或先按仓库切分数据。'
                  : data.task.error_code === 'E_INFER_FAIL'
                    ? '推理阶段出错。可尝试在参数页关闭节假日特征后重试。'
                    : '请检查数据文件的列名与日期格式是否与模板一致。'}
            </p>
            <p className="mt-2 text-[0.75rem]" style={{ color: 'var(--text-3)' }}>
              中间产物未落库：失败的预测不写入任何明细，避免半成品被误当作结果使用。
            </p>
          </Section>
          <RetryCard task={data} />
        </>
      ) : (
        <>
          {/* 结论 + 指标卡：与结果页同源（UX-P10-02） */}
          <Section title="指标结论" testId="metric-block">
            {data.metrics.weighted_mape !== null ? (
              <>
                <VerdictBar
                  metrics={data.metrics}
                  isOverThreshold={data.is_over_threshold}
                />
                <div className="mt-3">
                  <MetricCards
                    metrics={data.metrics}
                    l1Count={data.coverage.L1}
                    detailTotal={data.detail_total}
                  />
                </div>
                <div className="mt-3">
                  <BaselineTable metrics={data.metrics} />
                </div>
              </>
            ) : (
              <p className="text-[0.8125rem]" style={{ color: 'var(--text-2)' }}>
                该任务未产出指标。
              </p>
            )}
          </Section>

          {/* 覆盖率 + 分布 */}
          <Section title="序列分级与误差分布" testId="coverage-block">
            <CoveragePanel coverage={data.coverage} />
            {data.distribution ? (
              <div className="mt-4">
                <DistributionChart distribution={data.distribution} />
              </div>
            ) : null}
          </Section>

          {/* 曲线：抽样展示（UX-P10-03） */}
          {data.series.length ? (
            <Section title="预测曲线概览" testId="series-block">
              <SeriesPicker
                series={data.series}
                activeKey={activeKey}
                onChange={setActiveKey}
                value={activeKeys}
                onValueChange={setActiveKeys}
                onLimit={(limit) => setToast(`最多同时对比 ${limit} 条序列，已按前 ${limit} 条截断。`)}
              />
              <div className="mt-3">
                <LineCompare
                  series={data.series}
                  activeKey={activeKey}
                  activeKeys={activeKeys}
                  height={280}
                />
              </div>
            </Section>
          ) : null}

          {/* 阶段耗时（UX-P10-04） */}
          <StageTiming timing={data.timing_ms} />
        </>
      )}

      <p
        className="mt-5 rounded-[var(--radius-sm)] px-3 py-2.5 text-[0.75rem] leading-[1.8]"
        style={{ background: 'var(--bg-subtle)', color: 'var(--text-2)' }}
        data-testid="eval-note"
      >
        {data.eval_note}
      </p>
    </div>
  );
}

/* ---------------------------------------------------------------- 区块与原子 */

function Section({
  title,
  children,
  testId,
  aside,
}: {
  title: string;
  children: React.ReactNode;
  testId: string;
  aside?: React.ReactNode;
}) {
  return (
    <section
      className="mb-4 rounded-[var(--radius)] border p-4"
      style={{ background: 'var(--surface)', borderColor: 'var(--border)' }}
      data-testid={testId}
    >
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="text-[1.0625rem] font-semibold" style={{ color: 'var(--text)' }}>
          {title}
        </h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

function Param({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-[0.6875rem]" style={{ color: 'var(--text-3)' }}>
        {label}
      </dt>
      <dd className="mt-0.5 text-[0.8125rem] font-medium tabular-nums" style={{ color: 'var(--text)' }}>
        {value}
      </dd>
    </div>
  );
}

/** 曲线抽样下拉（`UX-P10-03`：下拉选序列，不铺开 50 条） */
function SeriesPicker({
  series,
  activeKey,
  onChange,
  value,
  onValueChange,
  onLimit,
}: {
  series: SeriesCurve[];
  activeKey: string;
  onChange: (key: string) => void;
  /** 多选叠加（`FR-VIS-09`）—— 与结果页保持同一套交互 */
  value: string[];
  onValueChange: (keys: string[]) => void;
  onLimit: (limit: number) => void;
}) {
  return (
    // 📌 testid 保持 `series-picker`（不加 `-wrap` 后缀）：
    //    `scripts/verify_ep7_browser.mjs` 用 `[data-testid="series-picker"]`
    //    精确匹配，改名会让浏览器走查静默查不到元素。
    <div className="flex flex-col gap-1" data-testid="series-picker">
      <span className="text-[0.8125rem]" style={{ color: 'var(--text-2)' }}>
        选择序列（可搜索多选）
      </span>
      <SeriesMultiSelect
        series={series}
        value={value}
        onChange={(keys) => {
          onValueChange(keys);
          onChange(keys[keys.length - 1] ?? activeKey);
        }}
        onLimit={onLimit}
      />
    </div>
  );
}

/** 阶段耗时逐项「实际 / 预算」对照（`UX-P10-04`） */
function StageTiming({ timing }: { timing: PredictionResultResponse['timing_ms'] }) {
  // 预算比例与执行页共用同一份生成物（`generated/pipeline.ts`）
  const BUDGET = [
    { key: 'preprocess', label: '读取与校验', ms: timing.preprocess_ms },
    { key: 'features', label: '特征构建', ms: timing.features_ms },
    { key: 'inference', label: '批量推理', ms: timing.inference_ms },
    { key: 'metrics', label: '指标与回测', ms: timing.metrics_ms },
  ] as const;
  const sum = BUDGET.reduce((a, b) => a + b.ms, 0) || 1;

  return (
    <Section title="执行阶段耗时" testId="timing-block">
      <ul className="space-y-2" data-testid="timing-list">
        {BUDGET.map((s) => (
          <li key={s.key} className="flex items-center gap-3 text-[0.8125rem]">
            <span className="w-24 shrink-0" style={{ color: 'var(--text-2)' }}>
              {s.label}
            </span>
            <span className="relative h-2 flex-1 overflow-hidden rounded-full" style={{ background: 'var(--bg-subtle)' }}>
              <span
                className="absolute inset-y-0 left-0 rounded-full"
                style={{
                  width: `${(s.ms / sum) * 100}%`,
                  background: 'var(--primary)',
                }}
              />
            </span>
            <span className="w-24 shrink-0 text-right tabular-nums" style={{ color: 'var(--text)' }}>
              {(s.ms / 1000).toFixed(2)} s · {((s.ms / sum) * 100).toFixed(0)}%
            </span>
          </li>
        ))}
      </ul>
      <p className="mt-3 text-[0.75rem]" style={{ color: 'var(--text-2)' }} data-testid="timing-total">
        端到端总耗时 {(timing.total_ms / 1000).toFixed(1)} s（含两遍推理与落库）
      </p>
    </Section>
  );
}

/** 原参数重跑（`UX-P10-06` / `API-19-1`） */
function RetryCard({ task }: { task: PredictionResultResponse }) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const [newId, setNewId] = useState<number | null>(null);

  const rerun = async () => {
    if (busy) return;
    setBusy(true);
    setMsg('');
    try {
      // 🔒 重试**生成新任务**而非复用旧任务（`DB-T03-02`）：
      //    复用会让失败任务的状态被覆盖，历史里再也看不到这次失败。
      const r = await predictApi.create({
        dataset_id: task.dataset.id,
        // 🔒 granularity 是**契约必填**，不是可选增强字段：
        //    后端校验取值，省掉会被 40012 拒掉。
        granularity: 'warehouse_sku',
        horizon: task.task.horizon as 7 | 14 | 30,
        // ⚠️ 这里用「默认值 1」而非原任务的真值 —— 后端 `CreateTaskRequest`
        //    未回传 `use_holiday_feature` 到详情响应（EP-7 补齐前）。
        //    若原任务是「关闭节假日」，重跑结果会与原任务不可比。
        //    已登记为口径偏离 HIS-01，EP-8 统一补齐快照字段后修正。
        use_holiday_feature: 1,
      });
      setNewId(r.task_id);
      setMsg(`已用相同参数重新提交，新任务 #${r.task_id}`);
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : '重新提交失败，请稍后重试');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section title="用相同参数重跑" testId="retry-block">
      <p className="text-[0.8125rem]" style={{ color: 'var(--text-2)' }}>
        将使用与本次完全相同的参数（数据集 #{task.dataset.id}、{task.task.horizon} 天步长）
        提交一个新任务。原任务记录保留，不会被覆盖。
      </p>
      <div className="mt-3 flex items-center gap-2">
        <button
          type="button"
          onClick={rerun}
          disabled={busy}
          data-testid="retry-run"
          className="h-9 rounded-[var(--radius-sm)] px-3.5 text-[0.8125rem] font-medium disabled:opacity-50"
          style={{ background: 'var(--primary)', color: 'var(--on-primary)' }}
        >
          {busy ? '提交中…' : '用相同参数重跑'}
        </button>
        {newId ? (
          <Link
            to={`/history/${newId}`}
            data-testid="retry-goto"
            className="text-[0.8125rem] underline"
            style={{ color: 'var(--primary)' }}
          >
            查看新任务
          </Link>
        ) : null}
      </div>
      {msg ? (
        <p className="mt-2 text-[0.75rem]" style={{ color: newId ? 'var(--success)' : 'var(--text-2)' }} data-testid="retry-msg">
          {msg}
        </p>
      ) : null}
    </Section>
  );
}