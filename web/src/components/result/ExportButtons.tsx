/**
 * 结果页·导出按钮组（`FR-VIS-05` / `UX-P08-08`）
 * =============================================================================
 * 🔒 **游客可见但不可用**（`FR-USR-08` / `JT-10`）：
 *   游客链路（上传 → 参数 → 预测 → 结果）全程可走，但导出要求登录
 *   （`API-16` 用 `CurrentUser`）。所以这里**不隐藏按钮**而是禁用 + 给原因 ——
 *   隐藏按钮的话，游客会以为「这个功能不存在」；禁用 + 提示「登录后可用」
 *   才 telling 用户「有这个功能，你需要登录」。
 *
 * 🔒 **文件名从响应头取，而不是前端拼**：
 *   服务端 `Content-Disposition` 里带 `filename*=UTF-8''<percent-encoded>`，
 *   含任务号与时间戳。前端拼名字会出现「下载下来叫 预测结果.csv」，
 *   连续导两次就分不清是哪一次的结果 —— 而任务号是回查历史的关键线索。
 *
 * ⚠️ **导出中禁用按钮**：导出 3 万行 Excel 要几秒，期间重复点击会发起
 *   并发请求，服务端每次都重新生成整份文件，白白吃掉 CPU 与磁盘。
 */
import { useState } from 'react';
import { Link } from 'react-router-dom';

import { ApiError, predictApi } from '../../api/client';

type Props = {
  taskId: number;
  detailTotal: number;
  /** 是否已登录（游客不可导出） */
  isAuthenticated: boolean;
  onNeedLogin: () => void;
  onError: (msg: string) => void;
  /**
   * 导出函数（`FR-HIS-03`）。
   *
   * 📌 默认走结果页的 `predictApi.export`（`API-16`）；
   *    历史详情页传 `historyApi.export`（`API-19a`）—— 两条路由
   *    **列定义完全相同**（`FR-HIS-03`：「列定义同 `FR-VIS-05`」），
   *    只差 URL 前缀与归属校验，所以按钮组本身可以完全复用。
   */
  exporter?: (taskId: number, format: 'csv' | 'xlsx') => Promise<Blob>;
  /** 下载文件名的兜底（服务端 `Content-Disposition` 读不到时用） */
  fallbackNamePrefix?: string;
};

function parseFilename(headers: unknown, fallback: string): string {
  // ⚠️ fetch 的响应头在跨域时不可读（CORS 暴露白名单问题）。
  //    读不到就退回前端生成的名字 —— 不能因为拿不到头就彻底不能下载。
  const h = headers as Record<string, string> | undefined;
  const cd = h?.['content-disposition'];
  if (!cd) return fallback;
  const star = /filename\*=UTF-8''([^;]+)/i.exec(cd);
  if (star?.[1]) {
    try {
      return decodeURIComponent(star[1]);
    } catch {
      return fallback;
    }
  }
  const plain = /filename="([^"]+)"/i.exec(cd);
  return plain?.[1] ?? fallback;
}

export default function ExportButtons({
  taskId,
  detailTotal,
  isAuthenticated,
  onNeedLogin,
  onError,
  exporter = predictApi.export,
  fallbackNamePrefix = '预测结果',
}: Props) {
  const [busy, setBusy] = useState<'csv' | 'xlsx' | null>(null);

  const doExport = async (fmt: 'csv' | 'xlsx') => {
    if (!isAuthenticated) {
      onNeedLogin();
      return;
    }
    setBusy(fmt);
    try {
      const res = await exporter(taskId, fmt);
      const blob = res instanceof Blob ? res : new Blob([res]);
      const name = parseFilename(
        (res as unknown as { headers?: unknown }).headers,
        `${fallbackNamePrefix}_${taskId}.${fmt}`,
      );
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      // 🔒 必须 revoke：否则 Blob 的内存直到页面关闭才释放。
      //    连续导几次大文件会明显吃掉内存（移动端尤其明显）。
      URL.revokeObjectURL(url);
    } catch (e) {
      if (e instanceof ApiError) {
        // 游客 Token 过期也会走到这里，给可执行的提示而不是「未知错误」
        onError(
          e.code === 40101 || e.code === 40102
            ? '登录状态已失效，请重新登录后再导出'
            : e.message,
        );
      } else {
        onError(e instanceof Error ? e.message : '导出失败，请稍后重试');
      }
    } finally {
      setBusy(null);
    }
  };

  const btn = (fmt: 'csv' | 'xlsx', label: string, primary = false) => (
    <button
      type="button"
      onClick={() => void doExport(fmt)}
      // 🔒 游客**直接禁用**，而不是「点了再弹提示」。
      //    早期版本只在 `doExport` 内部判 `isAuthenticated` 后 setToast，
      //    按钮始终可点 —— 那样等于「用一个必然失败的请求 + 一条 toast」
      //    代替禁用，用户会以为功能坏了。禁用 + 旁边常驻说明才是正确形态。
      disabled={busy !== null || !isAuthenticated}
      title={isAuthenticated ? undefined : '导出需登录'}
      aria-disabled={busy !== null || !isAuthenticated}
      className="rounded-[var(--radius-sm)] border px-3 py-1.5 text-[0.8125rem] font-medium disabled:cursor-not-allowed disabled:opacity-50"
      style={{
        borderColor: primary ? 'var(--primary)' : 'var(--border-field)',
        background: primary ? 'var(--primary)' : 'var(--surface)',
        color: primary ? 'var(--on-primary)' : 'var(--text)',
      }}
      data-testid={`export-${fmt}`}
    >
      {busy === fmt ? '导出中…' : label}
    </button>
  );

  return (
    <div className="flex flex-col items-end gap-1.5" data-testid="export-buttons">
      <div className="flex items-center gap-2">
        {btn('csv', '导出 CSV', true)}
        {btn('xlsx', '导出 Excel')}
        {!isAuthenticated && (
          <Link
            to="/login"
            className="rounded-[var(--radius-sm)] border px-3 py-1.5 text-[0.8125rem] font-medium"
            style={{ borderColor: 'var(--primary)', color: 'var(--primary)' }}
            data-testid="export-login-link"
          >
            登录后导出
          </Link>
        )}
      </div>
      <p
        className="text-[0.6875rem]"
        style={{ color: isAuthenticated ? 'var(--text-3)' : 'var(--warning)' }}
        data-testid="export-hint"
      >
        {isAuthenticated
          ? `将导出 ${detailTotal.toLocaleString('zh-CN')} 行明细，列序固定为 商品 / 仓库 / 日期 / 预测销量 / 分级 / 实际销量 / 误差`
          : '导出需登录（可先查看完整结果）'}
      </p>
    </div>
  );
}
