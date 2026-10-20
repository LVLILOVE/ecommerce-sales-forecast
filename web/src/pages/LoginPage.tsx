/**
 * UX-P02 · 登录页（模板 C）
 * -----------------------------------------------------------------------------
 * 依据：UI/UX §7.2；PRD FR-USR-04 / 05 / 06 / 08；开发技术文档 §5.4 API-02
 *
 * 四个状态（原型 `login.html` 的 .state-pill 是其视觉演示，此处为真实交互）：
 *   正常    —— 表单可提交
 *   提交中  —— 按钮禁用 + 文案「登录中…」（`UX-P02-04`）
 *   密码错误 —— `.alert-danger`，文案含失败次数与距冷却的剩余次数（`UX-P02-02`）
 *   冷却中  —— 按钮禁用 + 文案「已锁定（47s）」+ 每秒倒计时（`UX-P02-03`，`BE-19`）
 *
 * 🔒 已按 `UX-GOV-01` 移除原型的「演示账号」信息块（`.demo-accounts`）——
 *    交付物中不得出现任何硬编码凭证。
 */
import { useEffect, useMemo, useState } from 'react';
import type { CSSProperties, FormEvent } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';

import { ApiError } from '../api/client';
import { useAuthStore } from '../store/useAuthStore';
import { ERR } from '../types';

/** 登录失败后由服务端回传的定位信息（见后端 auth_service.login 的 data 载荷） */
type CredentialData = {
  fail_count?: number;
  max_fail?: number;
  lock_seconds?: number;
  locked?: boolean;
  retry_after_seconds?: number;
};

type Alert = { title: string; text: string } | null;

const inputStyle: CSSProperties = {
  width: '100%',
  padding: '0.5rem 0.75rem',
  borderRadius: 'var(--radius-sm)',
  border: '1px solid var(--border-field)',
  background: 'var(--surface-2)',
  color: 'var(--text)',
  fontSize: '0.875rem',
  outline: 'none',
};

export default function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const login = useAuthStore((s) => s.login);
  const clear = useAuthStore((s) => s.clear);
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const status = useAuthStore((s) => s.status);

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [remember, setRemember] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [alert, setAlert] = useState<Alert>(null);
  /** 冷却截止时间（毫秒时间戳）。0 表示不在冷却中 */
  const [lockedUntil, setLockedUntil] = useState(0);
  const [tick, setTick] = useState(() => Date.now());
  /**
   * 「强制登出」只执行一次的守卫。
   * ⚠️ `forceLogout` 来自 `location.state`，登录成功后路由仍是 `/login`、
   *    state 也还在 —— 若不加这个守卫，用户输对新密码后会被 effect 再次
   *    `clear()` 掉，表现为「登录成功瞬间又被踢回登录页」（实测踩到）。
   */
  const [forcedLogoutDone, setForcedLogoutDone] = useState(false);

  // 已登录（含刷新后 token 仍有效）→ 直接进工作台或回来源页
  const from = (location.state as { from?: string } | null)?.from;
  /** 其他页面跳来时的一次性成功提示（如 `/profile` 改密成功 → 请用新密码登录） */
  const notice = (location.state as { notice?: string } | null)?.notice ?? '';
  /** 改密后要求强制登出（由 `ProfilePage` 传入，见下方 effect） */
  const forceLogout = (location.state as { forceLogout?: boolean } | null)?.forceLogout === true;
  useEffect(() => {
    if (status === 'ready' && isAuthenticated) {
      // 🔒 改密后的强制登出：由 `ProfilePage` 跳转过来时带 `forceLogout`。
      //    放在这里而不是资料页，是为了避免「clear() 紧急更新 vs navigate transition」
      //    的竞态 —— 那样会让 RequireAuth 抢先重定向并覆盖 notice（实测过）。
      if (forceLogout && !forcedLogoutDone) {
        setForcedLogoutDone(true);
        clear();
        return;
      }
      navigate(from ?? '/dashboard', { replace: true });
    }
  }, [status, isAuthenticated, from, navigate, forceLogout, forcedLogoutDone, clear]);

  // 冷却倒计时
  useEffect(() => {
    if (!lockedUntil) return;
    const timer = window.setInterval(() => setTick(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [lockedUntil]);

  const remain = useMemo(
    () => (lockedUntil ? Math.max(0, Math.ceil((lockedUntil - tick) / 1000)) : 0),
    [lockedUntil, tick],
  );
  const locked = remain > 0;
  const disabled = submitting || locked;

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (disabled) return;
    if (!email.trim() || !password) {
      setAlert({ title: '请补全登录信息', text: '邮箱与密码均为必填项。' });
      return;
    }

    setSubmitting(true);
    setAlert(null);
    try {
      await login({ email: email.trim(), password });
      // 成功后由上面的 useEffect 统一跳转，避免两处各写一次 navigate
    } catch (err) {
      if (err instanceof ApiError) {
        setAlert(describeError(err));
        applyLock(err);
      } else {
        setAlert({ title: '登录失败', text: '请稍后重试。若持续失败，请检查网络连接。' });
      }
    } finally {
      setSubmitting(false);
    }
  }

  /** 把后端的业务码翻译成「错在哪 + 现在还能做什么」 */
  function describeError(err: ApiError): Alert {
    const d = (err.data ?? {}) as CredentialData;
    if (err.code === ERR.BAD_CREDENTIAL) {
      const failNo = d.fail_count;
      const maxFail = d.max_fail ?? 3;
      const lockSec = d.lock_seconds ?? 60;
      return {
        title: '邮箱或密码不正确',
        text:
          failNo && failNo < maxFail
            ? `请检查后重试。这是第 ${failNo} 次失败，连续 ${maxFail} 次失败后该邮箱将进入 ${lockSec} 秒冷却期。`
            : `请检查后重试。连续失败已达 ${maxFail} 次，该邮箱已进入 ${lockSec} 秒冷却期。`,
      };
    }
    if (err.code === ERR.LOGIN_LOCKED) {
      return {
        title: '尝试次数过多，已暂时锁定',
        text: `请在 ${d.retry_after_seconds ?? 60} 秒后重试。这是防止密码被暴力破解的安全策略。`,
      };
    }
    if (err.code === ERR.BAD_PARAM) {
      return { title: '登录信息格式有误', text: err.message };
    }
    return { title: '登录失败', text: err.message };
  }

  /** 服务端判定已锁定（第 3 次失败或冷却期内）时，本地同步进入冷却态 */
  function applyLock(err: ApiError) {
    const d = (err.data ?? {}) as CredentialData;
    const seconds = d.retry_after_seconds ?? (d.locked ? d.lock_seconds ?? 60 : 0);
    if (seconds > 0) setLockedUntil(Date.now() + seconds * 1000);
  }

  return (
    <div>
      <div className="mb-6">
        <h2 className="text-[1.25rem]">登录</h2>
        <p className="mt-1 text-[0.8125rem]" style={{ color: 'var(--text-3)' }}>
          使用邮箱与密码登录，或先{' '}
          <Link to="/upload" style={{ color: 'var(--primary)', fontWeight: 600 }}>
            用示例数据体验
          </Link>
        </p>
      </div>

      {/* 改密成功等一次性提示（成功态，与下方错误态分开配色） */}
      {notice !== '' && (
        <div
          className="mb-4 flex gap-2.5 rounded-[var(--radius-sm)] px-3.5 py-3"
          style={{
            background: 'var(--success-soft)',
            border: '1px solid color-mix(in oklab, var(--success) 34%, transparent)',
          }}
          role="status"
          data-testid="login-notice"
        >
          <div className="text-[0.75rem] leading-[1.7]">
            <p style={{ color: 'var(--text)' }}>{notice}</p>
          </div>
        </div>
      )}

      {/* 错误 / 限流提示（UX-P02-02、UX-P02-03 的 `.alert-danger`）。
          类名沿用原型语义命名，仅作结构与测试钩子，样式由 Tailwind + 令牌承担。 */}
      {alert && (
        <div
          className="alert-danger mb-4 flex gap-2.5 rounded-[var(--radius-sm)] px-3.5 py-3"
          style={{
            background: 'var(--danger-soft)',
            border: '1px solid color-mix(in oklab, var(--danger) 34%, transparent)',
          }}
          role="alert"
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden className="mt-0.5 flex-none">
            <circle cx="12" cy="12" r="9" stroke="var(--danger)" strokeWidth="1.8" />
            <path d="M12 7.5v5.5M12 16.2v.1" stroke="var(--danger)" strokeWidth="1.8" strokeLinecap="round" />
          </svg>
          <div className="text-[0.75rem] leading-[1.7]">
            <div className="font-bold" style={{ color: 'var(--danger)' }}>
              {alert.title}
            </div>
            <p style={{ color: 'var(--text-2)' }}>{alert.text}</p>
          </div>
        </div>
      )}

      <form onSubmit={onSubmit} noValidate>
        <div className="mb-3.5">
          <label htmlFor="login-email" className="mb-1.5 block text-[0.8125rem] font-semibold">
            邮箱
          </label>
          <input
            id="login-email"
            name="email"
            type="email"
            autoComplete="username"
            placeholder="you@example.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            aria-invalid={alert !== null}
            style={inputStyle}
          />
        </div>

        <div className="mb-3.5">
          <label htmlFor="login-password" className="mb-1.5 block text-[0.8125rem] font-semibold">
            密码
          </label>
          <input
            id="login-password"
            name="password"
            type="password"
            autoComplete="current-password"
            placeholder="请输入密码"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            aria-invalid={alert !== null}
            style={inputStyle}
          />
        </div>

        <div className="mb-4 flex items-center justify-between">
          <label className="flex cursor-pointer items-center gap-2 text-[0.8125rem]" style={{ color: 'var(--text-2)' }}>
            <input
              type="checkbox"
              checked={remember}
              onChange={(e) => setRemember(e.target.checked)}
            />
            {/* ⚠️「记住我」暂不改变 Token 有效期：后端 JWT_EXPIRE_MINUTES 统一 720。
                勾选项只在前端保留，避免出现「没勾也长期有效」的误导性承诺。 */}
            记住我
          </label>
          <button
            type="button"
            className="text-[0.8125rem]"
            style={{ color: 'var(--text-3)', background: 'none', cursor: 'not-allowed' }}
            title="找回密码为 P2 功能，本期未开放"
            disabled
          >
            忘记密码？
          </button>
        </div>

        <button
          type="submit"
          disabled={disabled}
          className="w-full rounded-[var(--radius-sm)] py-2.5 text-[0.875rem] font-semibold text-white transition-opacity"
          style={{
            background: 'var(--primary)',
            opacity: disabled ? 0.6 : 1,
            cursor: disabled ? 'not-allowed' : 'pointer',
          }}
        >
          {submitting ? '登录中…' : locked ? `已锁定（${remain}s）` : '登录'}
        </button>
      </form>

      <div
        className="mt-5 border-t pt-4 text-center text-[0.75rem]"
        style={{ borderColor: 'var(--border)', color: 'var(--text-3)' }}
      >
        还没有账号？{' '}
        <Link to="/register" style={{ color: 'var(--primary)', fontWeight: 600 }}>
          免费注册
        </Link>
        <div className="mt-1">登录即表示同意《用户协议》与《数据用途说明》</div>
      </div>
    </div>
  );
}
