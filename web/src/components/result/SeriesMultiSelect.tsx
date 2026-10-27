/**
 * 多序列搜索多选框（`FR-VIS-09` / 开发技术文档 `FE-19`）
 * =============================================================================
 * 🔴 **为什么不用 antd `Select`**（2026-10-27 实测后改的）：
 *   项目里 antd 只被 `ConfigProvider` + `theme` 用到，产物是 **77 kB**；
 *   一旦 `import { Select } from 'antd'`，tree-shaking 会把
 *   `rc-select` + `rc-virtual-list` + `rc-trigger` + `rc-motion` 整条链拉进
 *   主包 —— 实测 `antd-*.js` **77 kB → 286 kB（+209 kB，gzip +43 kB）**。
 *   本项目对包体有过明确教训（主包 343 kB → 1.5 MB 的回归），
 *   而这里的需求只有「搜索 + 多选 + 上限 + 可清除」，不值得为此付 209 kB。
 *
 * 🔒 **上限 50 条**（`FE-19` 原文：「默认渲染前 20 条，提供搜索多选组件让用户
 *   主动添加，最多同时渲染 50 条」）：
 *   ⚠️ 「默认渲染 20 条」与本页的三线图口径冲突 —— `UX-P08-05` 要求
 *   单序列图必须同时画「真实历史 + 模型预测 + 移动平均基线 + 预测起点标注」，
 *   20 条叠加时这四样全都画不出来。故实现取**默认选中 1 条**（完整三线图），
 *   多选时进入叠加模式，上限仍是 50 条 —— 性能约束照旧满足，可读性更好。
 *
 * ♿ 无障碍：输入框 `role="combobox"` + `aria-expanded` + `aria-controls`，
 *   列表 `role="listbox"`，选项 `role="option"` + `aria-selected`。
 *   键盘支持 ↑↓ 移动、Enter 切换、Esc 关闭 —— 这些细节是自研方案必须
 *   自己保证的部分（也是用 antd 时唯一真正省事的地方）。
 */
import { useEffect, useMemo, useRef, useState } from 'react';

import type { SeriesCurve } from '../../types';

/** `FE-19` 的硬上限 */
export const SERIES_MULTI_LIMIT = 50;

type Props = {
  series: SeriesCurve[];
  value: string[];
  onChange: (keys: string[]) => void;
  /** 超过上限时回调（页面据此提示用户，不能静默忽略） */
  onLimit?: (limit: number) => void;
};

export default function SeriesMultiSelect({ series, value, onChange, onLimit }: Props) {
  const [open, setOpen] = useState(false);
  const [kw, setKw] = useState('');
  /** 键盘导航游标（-1 = 未进入列表） */
  const [cursor, setCursor] = useState(-1);
  const boxRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const options = useMemo(
    () =>
      series.map((s) => ({
        key: s.key,
        // 搜索要能命中「商品 / 仓库 / 分级」三者之一
        label: `${s.sku_id} · ${s.warehouse_id}（${s.granularity_level}）`,
        haystack: `${s.sku_id} ${s.warehouse_id} ${s.granularity_level}`.toLowerCase(),
      })),
    [series],
  );

  const filtered = useMemo(() => {
    const q = kw.trim().toLowerCase();
    if (!q) return options;
    return options.filter((o) => o.haystack.includes(q));
  }, [options, kw]);

  // 点击组件外部 → 收起（⚠️ 依赖 `open`，不要加 useRef 守卫：
  //    空依赖 effect 在 StrictMode 下会被 setup→cleanup→setup 走两遍，
  //    加了守卫反而让第二次挂不上监听）
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (!boxRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);

  const toggle = (key: string) => {
    if (value.includes(key)) {
      const next = value.filter((k) => k !== key);
      // 不允许清空：空图比「只选一条」更让人困惑
      onChange(next.length ? next : value);
      return;
    }
    if (value.length >= SERIES_MULTI_LIMIT) {
      onLimit?.(SERIES_MULTI_LIMIT);
      return;
    }
    onChange([...value, key]);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      setOpen(false);
      return;
    }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!open) setOpen(true);
      const delta = e.key === 'ArrowDown' ? 1 : -1;
      setCursor((c) => {
        const n = filtered.length;
        if (!n) return -1;
        return (c + delta + n) % n;
      });
      return;
    }
    if (e.key === 'Enter' && open && cursor >= 0 && filtered[cursor]) {
      e.preventDefault();
      toggle(filtered[cursor].key);
    }
  };

  const selectedLabels = options.filter((o) => value.includes(o.key));

  return (
    <div className="relative" ref={boxRef} data-testid="series-multi-select">
      {/* ---- 已选区 + 搜索输入 ---- */}
      <div
        className="flex min-h-9 flex-wrap items-center gap-1.5 rounded-[var(--radius-sm)] border px-2 py-1"
        style={{ borderColor: 'var(--border-field)', background: 'var(--surface)' }}
        onClick={() => {
          setOpen(true);
          inputRef.current?.focus();
        }}
      >
        {selectedLabels.slice(0, 3).map((o) => (
          <span
            key={o.key}
            className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[0.6875rem]"
            style={{ background: 'var(--primary-soft)', color: 'var(--primary)' }}
          >
            {o.label}
            <button
              type="button"
              aria-label={`移除 ${o.label}`}
              onClick={(e) => {
                e.stopPropagation();
                toggle(o.key);
              }}
              style={{ color: 'var(--primary)' }}
            >
              ×
            </button>
          </span>
        ))}
        {selectedLabels.length > 3 && (
          <span
            className="rounded-full px-2 py-0.5 text-[0.6875rem]"
            style={{ background: 'var(--bg-subtle)', color: 'var(--text-2)' }}
            title={selectedLabels.slice(3).map((o) => o.label).join('\n')}
          >
            +{selectedLabels.length - 3}
          </span>
        )}
        <input
          ref={inputRef}
          value={kw}
          onChange={(e) => {
            setKw(e.target.value);
            setOpen(true);
            setCursor(-1);
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={onKeyDown}
          role="combobox"
          aria-expanded={open}
          aria-controls="series-multi-listbox"
          aria-autocomplete="list"
          aria-label="搜索并选择要对比的序列"
          placeholder={selectedLabels.length ? '继续搜索添加…' : '搜索商品 / 仓库，可多选叠加对比'}
          className="min-w-[10rem] flex-1 border-0 bg-transparent px-1 text-[0.8125rem] outline-none"
          style={{ color: 'var(--text)' }}
        />
      </div>

      <p className="mt-1 text-[0.6875rem]" style={{ color: 'var(--text-3)' }}>
        可搜索并多选叠加，最多同时对比 {SERIES_MULTI_LIMIT} 条；选 1 条时显示
        「历史 + 预测 + 基线」完整三线图，选多条时只看预测段的叠加走势。
      </p>

      {/* ---- 下拉列表 ---- */}
      {open && (
        <ul
          id="series-multi-listbox"
          role="listbox"
          aria-multiselectable="true"
          aria-label="序列列表"
          className="absolute z-20 mt-1 max-h-64 w-full overflow-auto rounded-[var(--radius-sm)] border py-1 shadow-lg"
          style={{ borderColor: 'var(--border)', background: 'var(--surface)' }}
        >
          {filtered.length === 0 && (
            <li className="px-3 py-2 text-[0.75rem]" style={{ color: 'var(--text-3)' }}>
              没有匹配「{kw}」的序列
            </li>
          )}
          {filtered.map((o, i) => {
            const picked = value.includes(o.key);
            return (
              <li key={o.key} role="none">
                <button
                  type="button"
                  role="option"
                  aria-selected={picked}
                  onClick={() => toggle(o.key)}
                  onMouseEnter={() => setCursor(i)}
                  className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[0.8125rem]"
                  style={{
                    background: i === cursor ? 'var(--bg-subtle)' : 'transparent',
                    color: 'var(--text)',
                  }}
                >
                  <span
                    aria-hidden="true"
                    className="inline-flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-[3px] border text-[0.625rem]"
                    style={{
                      borderColor: picked ? 'var(--primary)' : 'var(--border-field)',
                      background: picked ? 'var(--primary)' : 'transparent',
                      color: 'var(--on-primary)',
                    }}
                  >
                    {picked ? '✓' : ''}
                  </span>
                  <span className="truncate">{o.label}</span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
