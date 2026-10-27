/**
 * 结果页·折线对比图（`FR-VIS-01` / `UX-P08-05`）
 * =============================================================================
 * 🔒 **三条线的语义**（这是全项目最容易被画错的一张图）：
 *
 *   | 线 | 段 | 数据来源 | 含义 |
 *   |---|---|---|---|
 *   | 历史销量 | 实线 | `series[].history_sales` | 数据集里**已发生**的真实销量 |
 *   | 模型预测 | 虚线 | `series[].predicted` | LSTM 对**未来** horizon 天的预测 |
 *   | 移动平均基线 | 点线 | `series[].baseline` | 同窗口移动平均，与模型**同预测点** |
 *
 *   两段在时间轴上**首尾相接**：历史最后一天接预测第一天，中间不空一天。
 *   若中间留白，图上会出现「预测凭空晚了一天」的错觉 —— 而那正是
 *   `predict_service._horizon_dates` 里 `last + Timedelta(days=i+1)` 的口径
 *   （预测第一天就是数据最后一天的**次日**）。这个 off-by-one 是真实踩过的坑。
 *
 * ⚠️ **不画「真值线」**：`actual` 在在线预测里恒为 null（真值尚未发生，
 *    见 `models/prediction.py::actual_value` 注释）。这里刻意**不**用
 *    「预测值冒充真值」或「画一条预测=真值的对角线」来凑三条线。
 *
 * 🔒 **L3 不画数值预测段**（`UX-P08-06` / `UX-AC-10`）：
 *    L3 的 `predicted_value` 是历史均值占位，画出来会让用户以为
 *    「这批货每天卖一样多」。正确做法是**留空 + 文字说明**。
 */
import { lazy, Suspense, useMemo } from 'react';
import type { EChartsOption } from 'echarts';

import { Skeleton } from '../Skeleton';
import type { SeriesCurve } from '../../types';

type Props = {
  series: SeriesCurve[];
  /** 当前选中的序列 key（单选模式 = 完整三线图） */
  activeKey: string | null;
  /**
   * 多选叠加（`FR-VIS-09`，`FE-19`）。
   *
   * 📌 **≥2 条时进入叠加模式**，此时只画各序列的**预测段**：
   *    基线/历史段若也按 N 条画，线数会翻三倍（3N 条），50 条上限下必然糊成一团。
   *    单选（1 条）时仍走原来的完整三线图，`UX-P08-05` 的
   *    「历史 + 预测 + 预测起点 + 基线」一项都不少。
   */
  activeKeys?: string[];
  height?: number;
};

/**
 * 🔴 **顶层 `import EChart` 已改为懒加载**（`EP-8` 修复的包体回归）。
 *
 * 与 `DistributionChart` 同源的问题：`ResultPage` 静态导入本组件 →
 * `EChart` → `import * as echarts`（~1.2 MB）全落进主包。
 * 实测主包 **343 kB → 1.5 MB**，`EP-3` 的优化成果被完全抵消。
 *
 * 📌 沿用 `HomePage`对 `HeroChart` 的既有写法，非新发明。
 */
const EChart = lazy(() => import('../EChart'));

export default function LineCompare({
  series,
  activeKey,
  activeKeys,
  height = 300,
}: Props) {
  const active = series.find((s) => s.key === activeKey) ?? series[0];

  // ---- 叠加模式（`FR-VIS-09`）：≥2 条时只看预测段 ----
  const picks = useMemo(() => {
    if (!activeKeys || activeKeys.length < 2) return [];
    const wanted = new Set(activeKeys);
    return series.filter((s) => wanted.has(s.key));
  }, [series, activeKeys]);
  const overlay = picks.length >= 2;

  // 🔒 数据提取放在组件作用域（而非 useMemo 内）：下方「查看数据表」的无障碍
  //    表格要用同一份 histDates / predicted / baseline。若只在 useMemo 里算，
  //    表格就引用不到 —— 这也是 TS 报 `Cannot find name 'histDates'` 的原因。
  const isL3 = active?.granularity_level === 'L3';
  const histDates = useMemo(() => active?.history_dates ?? [], [active]);
  const histSales = useMemo(() => active?.history_sales ?? [], [active]);
  const fcDates = useMemo(() => active?.forecast_dates ?? [], [active]);
  const predicted = useMemo(() => active?.predicted ?? [], [active]);
  const baseline = useMemo(() => active?.baseline ?? [], [active]);

  const option = useMemo<EChartsOption>(() => {
    // ===================== 叠加模式（`FR-VIS-09`）=====================
    if (overlay) {
      // 日期轴取并集并排序：各序列 horizon 通常相同，但 L3 只有 1 个点，
      // 用并集才不会把某条序列的末期点挤掉。
      const axisSet = new Set<string>();
      picks.forEach((p) => (p.forecast_dates ?? []).forEach((d) => axisSet.add(d)));
      const axis = Array.from(axisSet).sort();
      // 颜色令牌只有 5 个：超过 5 条时会循环，图例与 tooltip 仍能区分
      const palette = ['var(--c1)', 'var(--c2)', 'var(--c3)', 'var(--c4)', 'var(--c5)'];
      return {
        color: palette,
        legend: {
          top: 0,
          type: 'scroll' as const,
          itemWidth: 14,
          itemHeight: 8,
          textStyle: { fontSize: 11 },
        },
        tooltip: { trigger: 'axis', axisPointer: { type: 'line' } },
        grid: { left: 8, right: 16, top: 44, bottom: 8, containLabel: true },
        xAxis: {
          type: 'category',
          data: axis,
          boundaryGap: false,
          axisLabel: { fontSize: 11, hideOverlap: true },
        },
        yAxis: { type: 'value', axisLabel: { fontSize: 11 }, splitLine: { lineStyle: { type: 'dashed' } } },
        series: picks.map((p) => {
          const idx = new Map((p.forecast_dates ?? []).map((d, i) => [d, i] as const));
          const isL3 = p.granularity_level === 'L3';
          return {
            name: `${p.sku_id} · ${p.warehouse_id}`,
            type: 'line' as const,
            // L3 无数值预测 → 全 null（不拿历史均值假装是预测值）
            data: axis.map((d) => {
              if (isL3) return null;
              const i = idx.get(d);
              return i === undefined ? null : p.predicted[i] ?? null;
            }),
            showSymbol: axis.length <= 14,
            symbolSize: 4,
            lineStyle: { width: 1.6 },
            connectNulls: false,
          };
        }),
      };
    }

    if (!active) return {};

    // 两段首尾相接：日期轴 = 历史 + 预测；数值轴按需留 null 让线断开
    const dates = [...histDates, ...fcDates];
    const historySeries: (number | null)[] = [...histSales];
    // 🔒 L3：预测段全 null，图上不画数值预测（UX-P08-06）
    const predSeries: (number | null)[] = [
      ...histDates.map(() => null),
      ...(isL3 ? fcDates.map(() => null) : predicted),
    ];
    const baseSeries: (number | null)[] = [
      ...histDates.map(() => null),
      ...(isL3 ? fcDates.map(() => null) : baseline),
    ];

    const allValues = [...histSales, ...predicted, ...baseline].filter(
      (v): v is number => typeof v === 'number' && Number.isFinite(v),
    );
    const maxV = allValues.length ? Math.max(...allValues) : 1;
    const minV = allValues.length ? Math.min(...allValues) : 0;
    const pad = (maxV - minV) * 0.12 || 1;

    // 预测起点 = 历史段长度（历史最后一天的下一天即预测第一天）
    const splitIndex = histDates.length;

    return {
      grid: { left: 8, right: 16, top: 34, bottom: 8, containLabel: true },
      legend: {
        top: 0,
        itemWidth: 18,
        itemHeight: 8,
        textStyle: { fontSize: 12 },
        data: ['历史销量', '模型预测', '移动平均基线'],
      },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'line' },
      },
      xAxis: {
        type: 'category',
        data: dates,
        boundaryGap: false,
        axisLabel: { fontSize: 11, hideOverlap: true },
        axisTick: { alignWithLabel: true },
      },
      yAxis: {
        type: 'value',
        min: Math.max(0, minV - pad),
        max: maxV + pad,
        axisLabel: { fontSize: 11 },
        splitLine: { lineStyle: { type: 'dashed' } },
      },
      // 预测起点标注（UX-P08-05 要求「标注预测起点」）
      markLine: splitIndex > 0
        ? {
            silent: true,
            symbol: 'none',
            label: {
              formatter: '预测起点',
              fontSize: 11,
              position: 'insideEndTop' as const,
            },
            lineStyle: { type: 'dashed' as const, opacity: 0.7 },
            data: [{ xAxis: splitIndex - 0.5 }],
          }
        : undefined,
      series: [
        {
          name: '历史销量',
          type: 'line' as const,
          data: historySeries,
          showSymbol: false,
          lineStyle: { width: 2 },
          itemStyle: { color: 'var(--c1)' },
          z: 3,
        },
        {
          name: '模型预测',
          type: 'line' as const,
          data: predSeries,
          showSymbol: true,
          symbolSize: 5,
          lineStyle: { width: 2, type: 'dashed' as const },
          itemStyle: { color: 'var(--c2)' },
          z: 4,
        },
        {
          name: '移动平均基线',
          type: 'line' as const,
          data: baseSeries,
          showSymbol: false,
          lineStyle: { width: 1.5, type: 'dotted' as const },
          itemStyle: { color: 'var(--text-3)' },
          z: 2,
        },
      ],
    };
  }, [active, histDates, histSales, fcDates, predicted, baseline, isL3, overlay, picks]);

  // ===================== 叠加模式的 UI（`FR-VIS-09`）=====================
  // 📌 放在 `if (!active)` 之前：叠加时 `active` 可能是 undefined（
  //    用户先多选、再取消掉单选项），用 active 判空会把合法请求挡掉。
  if (overlay) {
    const l3Count = picks.filter((p) => p.granularity_level === 'L3').length;
    return (
      <div data-testid="line-compare">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <p className="text-[0.8125rem]" style={{ color: 'var(--text-2)' }} data-testid="line-series-label">
            叠加对比 {picks.length} 条序列
            <span
              className="ml-2 rounded-full px-2 py-0.5 text-[0.6875rem]"
              style={{ background: 'var(--primary-soft)', color: 'var(--primary)' }}
            >
              多序列
            </span>
          </p>
          <p className="text-[0.75rem]" style={{ color: 'var(--text-3)' }}>
            仅显示预测段；实线为各序列预测值，颜色循环使用（共 5 色）
          </p>
        </div>

        {l3Count > 0 && (
          <p
            className="mb-2 rounded-[var(--radius-sm)] border px-3 py-2 text-[0.75rem]"
            style={{ borderColor: 'var(--warning)', background: 'var(--warning-soft)', color: 'var(--on-warning-soft)' }}
            data-testid="line-l3-note"
            role="note"
          >
            其中 {l3Count} 条为 L3（历史数据不足），
            <strong>不输出数值预测</strong>，在本图中不画线 —— 它们的降级说明见下方明细表。
          </p>
        )}

        <Suspense fallback={<Skeleton className="h-[18.75rem]" />}>
          <EChart
            option={option}
            height={height}
            testId="line-chart"
            ariaLabel={`${picks.length} 条序列的预测值叠加对比折线图`}
          />
        </Suspense>

        {/* 🔒 UX-GOV-10 无障碍替代：叠加模式下用「汇总表」而不是逐日明细 */}
        <details className="mt-3" data-testid="line-data-table">
          <summary className="cursor-pointer text-[0.8125rem] font-medium" style={{ color: 'var(--primary)' }}>
            查看序列汇总表（无障碍替代）
          </summary>
          <div className="mt-2 max-h-56 overflow-auto rounded-[var(--radius-sm)] border" style={{ borderColor: 'var(--border)' }}>
            <table className="w-full border-collapse text-[0.75rem]">
              <caption className="sr-only">叠加对比的 {picks.length} 条序列汇总</caption>
              <thead className="sticky top-0" style={{ background: 'var(--surface-2)' }}>
                <tr style={{ color: 'var(--text-2)' }}>
                  <th scope="col" className="border-b px-2 py-1.5 text-left font-medium">商品</th>
                  <th scope="col" className="border-b px-2 py-1.5 text-left font-medium">仓库</th>
                  <th scope="col" className="border-b px-2 py-1.5 text-left font-medium">分级</th>
                  <th scope="col" className="border-b px-2 py-1.5 text-right font-medium">预测点数</th>
                  <th scope="col" className="border-b px-2 py-1.5 text-right font-medium">首日预测</th>
                </tr>
              </thead>
              <tbody>
                {picks.map((p) => (
                  <tr key={p.key}>
                    <td className="border-b px-2 py-1">{p.sku_id}</td>
                    <td className="border-b px-2 py-1">{p.warehouse_id}</td>
                    <td className="border-b px-2 py-1">{p.granularity_level}</td>
                    <td className="border-b px-2 py-1 text-right tabular-nums">
                      {p.granularity_level === 'L3' ? 0 : (p.predicted ?? []).length}
                    </td>
                    <td className="border-b px-2 py-1 text-right tabular-nums">
                      {p.granularity_level === 'L3' ? '—' : p.predicted?.[0] ?? '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      </div>
    );
  }

  if (!active) {
    return (
      <p className="py-8 text-center text-[0.8125rem]" style={{ color: 'var(--text-3)' }}>
        暂无可展示的曲线数据
      </p>
    );
  }

  return (
    <div data-testid="line-compare">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <p className="text-[0.8125rem]" style={{ color: 'var(--text-2)' }} data-testid="line-series-label">
          {active.sku_id} · {active.warehouse_id}
          <span
            className="ml-2 rounded-full px-2 py-0.5 text-[0.6875rem]"
            style={{
              background: active.granularity_level === 'L1' ? 'var(--primary-soft)' : 'var(--warning-soft)',
              color: active.granularity_level === 'L1' ? 'var(--primary)' : 'var(--warning)',
            }}
          >
            {active.granularity_level}
          </span>
        </p>
        <p className="text-[0.75rem]" style={{ color: 'var(--text-3)' }}>
          实线为已发生销量，虚线为未来预测，点线为同窗口移动平均基线
        </p>
      </div>

      {active.granularity_level === 'L3' && (
        <p
          className="mb-2 rounded-[var(--radius-sm)] border px-3 py-2 text-[0.75rem]"
          style={{ borderColor: 'var(--warning)', background: 'var(--warning-soft)', color: 'var(--on-warning-soft)' }}
          data-testid="line-l3-note"
          role="note"
        >
          该序列历史数据不足，<strong>无法输出数值预测</strong>（此处以「—」表示，不以历史均值伪装为预测值）。
        </p>
      )}

      {/* 骨架高度与 chart 的 height 对齐，避免懒加载完成时布局跳动 */}
      <Suspense fallback={<Skeleton className="h-[18.75rem]" />}>
        <EChart
          option={option}
          height={height}
          testId="line-chart"
          ariaLabel={`${active.sku_id} 在 ${active.warehouse_id} 的销量历史与预测对比折线图`}
        />
      </Suspense>

      {/* 🔒 UX-GOV-10：图表的无障碍替代 —— 数据表（FE-18） */}
      <details className="mt-3" data-testid="line-data-table">
        <summary
          className="cursor-pointer text-[0.8125rem] font-medium"
          style={{ color: 'var(--primary)' }}
        >
          查看数据表（无障碍替代）
        </summary>
        <div className="mt-2 max-h-56 overflow-auto rounded-[var(--radius-sm)] border" style={{ borderColor: 'var(--border)' }}>
          <table className="w-full border-collapse text-[0.75rem]">
            <caption className="sr-only">{active.sku_id} 的历史与预测数值</caption>
            <thead className="sticky top-0" style={{ background: 'var(--surface-2)' }}>
              <tr style={{ color: 'var(--text-2)' }}>
                <th scope="col" className="border-b px-2 py-1.5 text-left font-medium">日期</th>
                <th scope="col" className="border-b px-2 py-1.5 text-right font-medium">历史销量</th>
                <th scope="col" className="border-b px-2 py-1.5 text-right font-medium">模型预测</th>
                <th scope="col" className="border-b px-2 py-1.5 text-right font-medium">移动平均基线</th>
              </tr>
            </thead>
            <tbody>
              {histDates.map((d, i) => (
                <tr key={`h-${d}`}>
                  <td className="border-b px-2 py-1 tabular-nums" style={{ color: 'var(--text-2)' }}>{d}</td>
                  <td className="border-b px-2 py-1 text-right tabular-nums">{histSales[i]}</td>
                  <td className="border-b px-2 py-1 text-right" style={{ color: 'var(--text-3)' }}>—</td>
                  <td className="border-b px-2 py-1 text-right" style={{ color: 'var(--text-3)' }}>—</td>
                </tr>
              ))}
              {fcDates.map((d, i) => (
                <tr key={`f-${d}`}>
                  <td className="border-b px-2 py-1 tabular-nums" style={{ color: 'var(--text-2)' }}>{d}</td>
                  <td className="border-b px-2 py-1 text-right" style={{ color: 'var(--text-3)' }}>—</td>
                  <td className="border-b px-2 py-1 text-right tabular-nums">
                    {active.granularity_level === 'L3' ? '—' : predicted[i] ?? '—'}
                  </td>
                  <td className="border-b px-2 py-1 text-right tabular-nums">
                    {active.granularity_level === 'L3' ? '—' : baseline[i] ?? '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}
