/**
 * 应用外壳（模板 A 的骨架：页头 + 侧栏 + 主内容槽 + 页脚）
 * -----------------------------------------------------------------------------
 * AppLayout 与 AsideLayout 共用此骨架，区别仅在中间主内容槽的形态。
 * 视觉对齐原型 app.css 的 .app-header / .sidebar 结构。
 */
import type { ReactNode } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';

import Brand from '../components/Brand';
import SiteFooter from '../components/SiteFooter';
import ThemeToggle from '../components/ThemeToggle';
import { useAuthStore } from '../store/useAuthStore';

const NAV_ITEMS = [
  { key: 'dashboard', label: '工作台', to: '/dashboard' },
  { key: 'upload', label: '新建预测', to: '/upload' },
  // EP-7：记录类入口。三者都要求登录（路由已套 RequireAuth），
  //   故不需要额外的登录态判断 —— 未登录时守卫会先跳登录页。
  { key: 'history', label: '历史任务', to: '/history' },
  { key: 'compare', label: '跨任务对比', to: '/compare' },
];

/**
 * `AD-P01` / `AD-P02` 的导航入口（`QD-01` / `WS-8-12`）。
 *
 * 🔒 **只在 `isAdmin` 时插入**，且放在**顶栏用户区**而非侧栏分组：
 *   - 侧栏新增「管理」分组会让普通用户看到一个点进去空无一物的分组，
 *     IA 的「4 组 / 8 导航项」也会被破坏（`QD-01` 的原始理由）；
 *   - 「管理员」徽标已经表明身份，把入口放在徽标旁边是最低认知成本的。
 *
 * ⚠️ **这是体验层的收敛，不是安全边界**。真正的判定在后端
 *    `get_current_admin`（`BE-ADM-01`）：非管理员直接调接口一律 `40301`。
 *    手工改URL 进来会看到 `RequireAdmin` 的 403 落点。
 */
const ADMIN_NAV_ITEMS = [
  { key: 'admin', label: '数据看板', to: '/admin' },
  { key: 'admin-manage', label: '用户与任务管理', to: '/admin/manage' },
];

export default function AppShell({ children }: { children: ReactNode }) {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);
  const isAdmin = useAuthStore((s) => s.isAdmin);
  const logout = useAuthStore((s) => s.logout);

  async function onLogout() {
    await logout();
    navigate('/login', { replace: true });
  }

  return (
    <div className="flex min-h-screen flex-col">
      <header
        className="sticky top-0 z-50 flex h-[var(--header-h)] items-center gap-4 px-5"
        style={{
          background: 'color-mix(in oklab, var(--surface) 88%, transparent)',
          borderBottom: '1px solid var(--border)',
          backdropFilter: 'blur(12px) saturate(1.4)',
        }}
      >
        <Brand />
        <nav className="ml-auto flex items-center gap-4">
          {NAV_ITEMS.map((it) => (
            <Link
              key={it.key}
              to={it.to}
              className="text-[0.8125rem] no-underline hover:underline"
              style={{ color: pathname === it.to ? 'var(--primary)' : 'var(--text-2)' }}
            >
              {it.label}
            </Link>
          ))}
          <ThemeToggle />

          {/* 用户区（API-04 的数据源，UX-P04-1 页头用户菜单） */}
          {user ? (
            <div className="flex items-center gap-2.5 border-l pl-4" style={{ borderColor: 'var(--border)' }}>
              <span className="text-[0.8125rem]" style={{ color: 'var(--text-2)' }}>
                {user.nickname ?? user.email}
              </span>
              {/* FR-USR-07 的入口：放在用户区而非侧栏 ——
                  侧栏是「业务导航」4 项（IA 固定），个人资料属于账号级操作，
                  与「退出登录」同属一组语义。 */}
              <Link
                to="/profile"
                className="text-[0.75rem] no-underline hover:underline"
                style={{ color: pathname === '/profile' ? 'var(--primary)' : 'var(--text-3)' }}
                data-testid="nav-profile"
              >
                个人资料
              </Link>
              {isAdmin && (
                <>
                  <span
                    className="rounded-full px-2 py-[1px] text-[0.6875rem] font-semibold"
                    style={{ background: 'var(--primary-soft)', color: 'var(--on-primary-soft)' }}
                    title="当前账号具有管理端访问权限"
                    data-testid="admin-badge"
                  >
                    管理员
                  </span>
                  {/* 🔒 管理端入口仅 `isAdmin` 可见（`QD-01`）：
                      放在「管理员」徽标右侧，与身份标识相邻。 */}
                  {ADMIN_NAV_ITEMS.map((it) => (
                    <Link
                      key={it.key}
                      to={it.to}
                      className="rounded-[var(--radius-sm)] px-2 py-1 text-[0.75rem] no-underline"
                      style={{
                        background: 'var(--primary-soft)',
                        color: 'var(--on-primary-soft)',
                      }}
                      data-testid={`nav-${it.key}`}
                    >
                      {it.label}
                    </Link>
                  ))}
                </>
              )}
              <button
                type="button"
                onClick={onLogout}
                className="text-[0.75rem]"
                style={{ color: 'var(--text-3)', background: 'none' }}
              >
                退出登录
              </button>
            </div>
          ) : (
            <Link
              to="/login"
              className="text-[0.75rem] no-underline"
              style={{ color: 'var(--primary)' }}
            >
              登录
            </Link>
          )}
        </nav>
      </header>

      <div className="flex flex-1">
        <aside
          className="w-[var(--sidebar-w)] flex-none border-r"
          style={{ background: 'var(--surface)', borderColor: 'var(--border)' }}
        >
          <nav className="flex flex-col gap-0.5 p-3">
            {NAV_ITEMS.map((it) => (
              <Link
                key={it.key}
                to={it.to}
                className="rounded-[var(--radius-sm)] px-3 py-2 text-[0.8125rem] no-underline"
                style={{
                  color: pathname === it.to ? 'var(--primary)' : 'var(--text-2)',
                  background: pathname === it.to ? 'var(--primary-soft)' : 'transparent',
                }}
              >
                {it.label}
              </Link>
            ))}
          </nav>
        </aside>
        <div className="min-w-0 flex-1">{children}</div>
      </div>

      <SiteFooter />
    </div>
  );
}
