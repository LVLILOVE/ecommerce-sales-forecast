/**
 * UX-P12 · 个人资料（补 `FR-USR-07` 的前端落点）
 * -----------------------------------------------------------------------------
 * 背景：`FR-USR-07`（P1）要求「用户可查看与修改个人信息：昵称、密码；
 *   修改密码需输入原密码；修改后原 Token 失效并强制重新登录」。
 *   后端 `PATCH /api/v1/users/me`（`API-05`）早已实现并有
 *   `token_version + 1` 联动，`client.ts` 也封装了 `updateMe()`，
 *   但**此前没有任何页面调用它** —— 用户界面上无法自助改密。
 *   本页是这条需求的界面落点（2026-10-20 验收报告 `Y-01`）。
 *
 * 交互要点
 *   1. 邮箱只读（注册后不可改），角色 / 注册时间 / 上次登录为只读信息。
 *   2. 昵称与密码分成两个独立表单 —— 它们的失败语义不同，
 *      混在一个表单里会让「改昵称失败」看起来像「密码错了」。
 *   3. 🔒 **改密成功后必须主动清会话并跳登录页**：
 *      后端已 `token_version + 1`，当前 Token 立即作废；若停留在原页，
 *      下一次任意请求都会 401 再被拦截器弹走 —— 主动跳转更清晰。
 *   4. 🔒 **72 字节上限在前端可见**（`JT-12`）：bcrypt 上限是字节不是字符，
 *      24 个中文就已触顶，只在提交后由后端拒绝会让用户困惑。
 *
 * ⚠️ 错误态一律用 `alert !== null` 判定，**不用字符串真值性**（`Error.message`
 *    为空串时会被当成「没出错」，本项目曾因此出现永久 loading）。
 */
import { useState } from 'react';
import type { CSSProperties, FormEvent, ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';

import { ApiError, authApi } from '../api/client';
import { useAuthStore } from '../store/useAuthStore';
import { ERR, LIMITS, isPasswordWithinBcryptLimit, passwordByteLength } from '../types';

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

/** 只读信息行 */
function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline gap-3 py-1.5">
      <span className="w-[5.5rem] flex-none text-[0.75rem]" style={{ color: 'var(--text-3)' }}>
        {label}
      </span>
      <span className="text-[0.8125rem]" style={{ color: 'var(--text)' }}>
        {value}
      </span>
    </div>
  );
}

/** 卡片外壳：标题 + 说明 + 内容 */
function Card({
  title,
  desc,
  children,
  testId,
}: {
  title: string;
  desc: string;
  children: ReactNode;
  testId: string;
}) {
  return (
    <section
      className="rounded-[var(--radius)] border p-5"
      style={{ background: 'var(--surface)', borderColor: 'var(--border)' }}
      data-testid={testId}
    >
      <h2 className="text-[1rem] font-semibold" style={{ color: 'var(--text)' }}>
        {title}
      </h2>
      <p className="mt-1 text-[0.75rem]" style={{ color: 'var(--text-3)' }}>
        {desc}
      </p>
      <div className="mt-4">{children}</div>
    </section>
  );
}

export default function ProfilePage() {
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);
  const setUser = useAuthStore((s) => s.setUser);

  // ---- 昵称 ----
  const [nickname, setNickname] = useState(user?.nickname ?? '');
  const [nickSaving, setNickSaving] = useState(false);
  const [nickAlert, setNickAlert] = useState<Alert>(null);
  const [nickOk, setNickOk] = useState('');

  // ---- 密码 ----
  const [oldPwd, setOldPwd] = useState('');
  const [newPwd, setNewPwd] = useState('');
  const [confirmPwd, setConfirmPwd] = useState('');
  const [pwdSaving, setPwdSaving] = useState(false);
  const [pwdAlert, setPwdAlert] = useState<Alert>(null);

  const okLen = newPwd.length >= 8;
  const okAlpha = /[A-Za-z]/.test(newPwd);
  const okNum = /\d/.test(newPwd);
  const withinBytes = isPasswordWithinBcryptLimit(newPwd);
  const byteLen = passwordByteLength(newPwd);
  const basicOk = okLen && okAlpha && okNum && withinBytes;
  const mismatch = confirmPwd.length > 0 && confirmPwd !== newPwd;

  const rules = [
    { key: 'len', text: '长度不少于 8 位', ok: okLen },
    { key: 'alpha', text: '至少包含 1 个字母', ok: okAlpha },
    { key: 'num', text: '至少包含 1 个数字', ok: okNum },
  ];

  async function onSaveNickname(e: FormEvent) {
    e.preventDefault();
    if (nickSaving) return;
    setNickAlert(null);
    setNickOk('');

    const next = nickname.trim();
    if (next.length === 0) {
      setNickAlert({
        title: '昵称不能为空',
        text: '昵称用于界面显示。若想去掉昵称，请填写任意称呼后保存。',
      });
      return;
    }

    setNickSaving(true);
    try {
      const updated = await authApi.updateMe({ nickname: next });
      setUser(updated);
      setNickOk('昵称已更新。');
    } catch (err) {
      setNickAlert({
        title: '昵称保存失败',
        text:
          err instanceof ApiError && err.code === ERR.BAD_PARAM
            ? `${err.message}（昵称不超过 32 个字符）`
            : err instanceof Error
              ? err.message
              : '请稍后重试。',
      });
    } finally {
      setNickSaving(false);
    }
  }

  async function onChangePassword(e: FormEvent) {
    e.preventDefault();
    if (pwdSaving) return;
    setPwdAlert(null);

    if (oldPwd.length === 0) {
      setPwdAlert({ title: '请填写原密码', text: '修改密码需要先验证身份，请填写当前使用的密码。' });
      return;
    }
    if (!basicOk) {
      setPwdAlert({
        title: '新密码强度不足',
        text: withinBytes
          ? '新密码需至少 8 位，且同时包含字母与数字。'
          : `密码编码后不得超过 ${LIMITS.password_max_bytes} 字节（当前 ${byteLen} 字节）。中文等字符每个占 3 字节，请缩短密码或改用字母与数字。`,
      });
      return;
    }
    if (newPwd === oldPwd) {
      setPwdAlert({
        title: '新密码与原密码相同',
        text: '请设置一个与当前不同的密码，否则本次修改没有意义。',
      });
      return;
    }
    if (mismatch) {
      setPwdAlert({ title: '两次输入的新密码不一致', text: '请确认「确认新密码」与上方新密码完全相同。' });
      return;
    }

    setPwdSaving(true);
    try {
      await authApi.updateMe({ old_password: oldPwd, new_password: newPwd });
      // 🔒 后端已 token_version + 1：当前会话立即失效，必须回登录页重新登录。
      // ⚠️ 这里**只跳转、不在这里 clear()**：
      //    React Router 的 navigate 走 transition（低优先级），而 clear() 是紧急更新。
      //    若两者同时发起，紧急更新会先渲染一帧「已登出但路由仍是 /profile」，
      //    于是 `RequireAuth` 抢先重定向并写入 `state.from=/profile`，
      //    把这里的 `notice` 覆盖掉 —— 实测就是「看不到提示 + 登录后又被送回资料页」。
      //    改为：把「登出」这个动作交给登录页（它收到 notice 就执行 clear），
      //    全程只有 navigate 一个写入者，不存在竞态。
      navigate('/login', {
        replace: true,
        state: { notice: '密码已修改，请用新密码重新登录。', forceLogout: true },
      });
    } catch (err) {
      if (err instanceof ApiError && err.code === ERR.BAD_CREDENTIAL) {
        setPwdAlert({
          title: '原密码不正确',
          text: '请确认「原密码」填写的是当前正在使用的密码。若已忘记，需由管理员重置。',
        });
      } else if (err instanceof ApiError && err.code === ERR.WEAK_PASSWORD) {
        setPwdAlert({ title: '新密码不符合要求', text: err.message });
      } else if (err instanceof ApiError && err.code === ERR.BAD_PARAM) {
        setPwdAlert({ title: '密码格式有误', text: err.message });
      } else {
        setPwdAlert({
          title: '密码修改失败',
          text: err instanceof Error ? err.message : '请稍后重试。',
        });
      }
    } finally {
      setPwdSaving(false);
    }
  }

  if (!user) {
    // RequireAuth 已保证有 user；这里是类型收窄的兜底，正常不会走到
    return null;
  }

  return (
    <div data-testid="profile-page">
      <header className="mb-5">
        <p className="text-[0.75rem]" style={{ color: 'var(--text-3)' }}>
          账号 / 个人资料
        </p>
        <h1 className="mt-1 text-[1.5rem] font-semibold" style={{ color: 'var(--text)' }}>
          个人资料
        </h1>
        <p className="mt-1 text-[0.8125rem]" style={{ color: 'var(--text-2)' }}>
          查看账号信息，修改昵称与登录密码。邮箱作为登录账号，注册后不可修改。
        </p>
      </header>

      <div className="flex max-w-[640px] flex-col gap-4">
        {/* ---------- 账号信息（只读） ---------- */}
        <Card title="账号信息" desc="以下信息由系统维护，不可自行修改。" testId="card-account">
          <div className="divide-y" style={{ borderColor: 'var(--border)' }}>
            <InfoRow label="邮箱" value={user.email} />
            <InfoRow label="角色" value={user.role === 'admin' ? '管理员' : '普通用户'} />
            <InfoRow label="注册时间" value={user.created_at} />
            <InfoRow label="上次登录" value={user.last_login_at ?? '—'} />
            <InfoRow label="账号状态" value={user.status === 1 ? '正常' : '已停用'} />
          </div>
        </Card>

        {/* ---------- 昵称 ---------- */}
        <Card title="基本资料" desc="昵称用于界面显示（顶栏、工作台问候语），可随时修改。" testId="card-nickname">
          {nickAlert !== null && (
            <div
              className="alert-danger mb-3 flex gap-2.5 rounded-[var(--radius-sm)] px-3.5 py-3"
              style={{
                background: 'var(--danger-soft)',
                border: '1px solid color-mix(in oklab, var(--danger) 34%, transparent)',
              }}
              role="alert"
              data-testid="nick-alert"
            >
              <div className="text-[0.75rem] leading-[1.7]">
                <div className="font-bold" style={{ color: 'var(--danger)' }}>
                  {nickAlert.title}
                </div>
                <p style={{ color: 'var(--text-2)' }}>{nickAlert.text}</p>
              </div>
            </div>
          )}
          {nickOk !== '' && (
            <p className="mb-3 text-[0.75rem]" style={{ color: 'var(--success)' }} data-testid="nick-ok">
              {nickOk}
            </p>
          )}

          <form onSubmit={onSaveNickname} noValidate>
            <label htmlFor="profile-nickname" className="mb-1.5 block text-[0.8125rem] font-semibold">
              昵称
            </label>
            <input
              id="profile-nickname"
              value={nickname}
              maxLength={32}
              onChange={(e) => setNickname(e.target.value)}
              placeholder="用于界面显示，如 王琳"
              style={inputStyle}
            />
            <div className="mt-1 text-[0.6875rem]" style={{ color: 'var(--text-3)' }}>
              不超过 32 个字符
            </div>
            <button
              type="submit"
              disabled={nickSaving}
              className="mt-3 rounded-[var(--radius-sm)] px-4 py-2 text-[0.8125rem] font-semibold"
              style={{
                background: 'var(--primary)',
                color: 'var(--on-primary)',
                opacity: nickSaving ? 0.6 : 1,
                cursor: nickSaving ? 'not-allowed' : 'pointer',
              }}
              data-testid="nickname-save"
            >
              {nickSaving ? '保存中…' : '保存昵称'}
            </button>
          </form>
        </Card>

        {/* ---------- 密码 ---------- */}
        <Card
          title="修改密码"
          desc="需先输入当前密码验证身份。修改成功后，所有已登录的会话都会立即失效，需要用新密码重新登录。"
          testId="card-password"
        >
          {pwdAlert !== null && (
            <div
              className="alert-danger mb-3 flex gap-2.5 rounded-[var(--radius-sm)] px-3.5 py-3"
              style={{
                background: 'var(--danger-soft)',
                border: '1px solid color-mix(in oklab, var(--danger) 34%, transparent)',
              }}
              role="alert"
              data-testid="pwd-alert"
            >
              <div className="text-[0.75rem] leading-[1.7]">
                <div className="font-bold" style={{ color: 'var(--danger)' }}>
                  {pwdAlert.title}
                </div>
                <p style={{ color: 'var(--text-2)' }}>{pwdAlert.text}</p>
              </div>
            </div>
          )}

          <form onSubmit={onChangePassword} noValidate>
            <div className="mb-3">
              <label htmlFor="profile-old-pwd" className="mb-1.5 block text-[0.8125rem] font-semibold">
                原密码
              </label>
              <input
                id="profile-old-pwd"
                type="password"
                autoComplete="current-password"
                placeholder="当前正在使用的密码"
                value={oldPwd}
                onChange={(e) => setOldPwd(e.target.value)}
                style={inputStyle}
              />
            </div>

            <div className="mb-3">
              <label htmlFor="profile-new-pwd" className="mb-1.5 block text-[0.8125rem] font-semibold">
                新密码
              </label>
              <input
                id="profile-new-pwd"
                type="password"
                autoComplete="new-password"
                placeholder="至少 8 位，含字母与数字"
                value={newPwd}
                onChange={(e) => setNewPwd(e.target.value)}
                aria-invalid={newPwd.length > 0 && !basicOk}
                style={inputStyle}
              />

              {/* 3 条实时规则（与注册页同一套口径，`UX-P03-02`） */}
              <div className="mt-2 flex flex-col gap-[3px]">
                {rules.map((r) => (
                  <div
                    key={r.key}
                    className="flex items-center gap-1.5 text-[0.6875rem]"
                    style={{ color: r.ok ? 'var(--success)' : 'var(--text-3)' }}
                  >
                    <span
                      className="flex h-3 w-3 flex-none items-center justify-center rounded-full"
                      style={{
                        background: r.ok ? 'var(--success)' : 'transparent',
                        border: r.ok ? '1px solid var(--success)' : '1px solid var(--border-strong)',
                      }}
                      aria-hidden
                    >
                      {r.ok && (
                        <svg width="8" height="8" viewBox="0 0 24 24" fill="none" aria-hidden>
                          <path
                            d="M5 13l4 4L19 7"
                            stroke="var(--on-primary)"
                            strokeWidth="3.4"
                            strokeLinecap="round"
                            strokeLinejoin="round"
                          />
                        </svg>
                      )}
                    </span>
                    {r.text}
                  </div>
                ))}
              </div>

              {/* 🔒 72 字节上限实时可见（`JT-12`） */}
              <div
                style={{
                  marginTop: '0.375rem',
                  fontSize: '0.6875rem',
                  color: withinBytes ? 'var(--text-3)' : 'var(--danger)',
                }}
                data-testid="pwd-bytes"
              >
                密码编码长度 {byteLen} / {LIMITS.password_max_bytes} 字节
                {!withinBytes && '（已超出 bcrypt 上限，请缩短或改用字母与数字）'}
              </div>
            </div>

            <div className="mb-3">
              <label htmlFor="profile-confirm-pwd" className="mb-1.5 block text-[0.8125rem] font-semibold">
                确认新密码
              </label>
              <input
                id="profile-confirm-pwd"
                type="password"
                autoComplete="new-password"
                placeholder="再次输入新密码"
                value={confirmPwd}
                onChange={(e) => setConfirmPwd(e.target.value)}
                aria-invalid={mismatch}
                style={inputStyle}
              />
              {mismatch && (
                <div className="mt-1 text-[0.6875rem]" style={{ color: 'var(--danger)' }}>
                  两次输入的新密码不一致
                </div>
              )}
            </div>

            <button
              type="submit"
              disabled={pwdSaving}
              className="rounded-[var(--radius-sm)] px-4 py-2 text-[0.8125rem] font-semibold"
              style={{
                background: 'var(--primary)',
                color: 'var(--on-primary)',
                opacity: pwdSaving ? 0.6 : 1,
                cursor: pwdSaving ? 'not-allowed' : 'pointer',
              }}
              data-testid="pwd-save"
            >
              {pwdSaving ? '修改中…' : '修改密码'}
            </button>

            <p className="mt-3 text-[0.6875rem]" style={{ color: 'var(--text-3)' }}>
              忘记原密码？本系统未提供自助找回通道，请联系管理员重置。
            </p>
          </form>
        </Card>
      </div>
    </div>
  );
}
