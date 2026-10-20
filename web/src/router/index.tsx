/**
 * 路由表
 * -----------------------------------------------------------------------------
 * 布局模板分配（UI/UX §5）：
 *   MarketingLayout（D）→ `/`            产品首页 UX-P01
 *   AuthLayout（C）      → `/login` `/register`   UX-P02 / UX-P03
 *   AppLayout（A）       → `/dashboard`    工作台 UX-P04（**需登录**）
 *   AsideLayout（B）     → `/upload` `/params` `/running/:taskId` `/result/:taskId`
 *                          上传 UX-P05 / 参数 UX-P06 / 执行 UX-P07 / 结果 UX-P08
 *   AppLayout（A）       → `/history` `/history/:taskId` `/compare`
 *                          历史 UX-P09 / 详情 UX-P10 / 对比 UX-P11（EP-7，均需登录）
 *
 * 🔒 **游客可达性**（`FR-USR-08` / `JT-10`）——不要给这些页面套 `RequireAuth`：
 *   上传 → 参数 → 执行 → 结果 这条链路**允许未登录跑通**，数据归属写 `user_id = 0`
 *   （游客哨兵）。只有「导出」与「历史记录」才要求登录。
 *   EP-5 新增 `/running/:taskId` 时同样**不得**加守卫：游客必须能看到自己刚跑完的
 *   任务执行结果与失败原因（否则「跑通了但看不到为什么超时」）。
 *
 * ⚠️ `/running/:taskId` 带**路径参数**（任务 ID），而 `/upload` `/params` 用
 *    **查询参数**（`?datasetId=`）。两者都是「地址即真源」：刷新、分享、
 *    前进后退都不丢，且服务端与前端都不需要为这次跳转保存任何状态。
 *    执行页另取 `?horizon=&holiday=&datasetId=&t=` 四个展示用参数 —— 它们
 *    **不参与任何业务判定**（真实参数已随任务落库），只用于把页面渲染完整。
 */
import type { RouteObject } from 'react-router-dom';

import RequireAdmin from '../components/RequireAdmin';
import RequireAuth from '../components/RequireAuth';
import AppLayout from '../layouts/AppLayout';
import AsideLayout from '../layouts/AsideLayout';
import AuthLayout from '../layouts/AuthLayout';
import MarketingLayout from '../layouts/MarketingLayout';
import AdminDashboardPage from '../pages/AdminDashboardPage';
import AdminManagePage from '../pages/AdminManagePage';
import ComparePage from '../pages/ComparePage';
import DashboardPage from '../pages/DashboardPage';
import HistoryDetailPage from '../pages/HistoryDetailPage';
import HistoryPage from '../pages/HistoryPage';
import HomePage from '../pages/HomePage';
import LoginPage from '../pages/LoginPage';
import ParamsPage from '../pages/ParamsPage';
import ProfilePage from '../pages/ProfilePage';
import RegisterPage from '../pages/RegisterPage';
import ResultPage from '../pages/ResultPage';
import RunningPage from '../pages/RunningPage';
import UploadPage from '../pages/UploadPage';

export const router: RouteObject[] = [
  {
    path: '/',
    element: <MarketingLayout />,
    children: [{ index: true, element: <HomePage /> }],
  },
  {
    path: '/login',
    element: <AuthLayout />,
    children: [{ index: true, element: <LoginPage /> }],
  },
  {
    path: '/register',
    element: <AuthLayout />,
    children: [{ index: true, element: <RegisterPage /> }],
  },
  {
    // 工作台是「登录后首页」（UX-P04），必须登录
    path: '/dashboard',
    element: (
      <RequireAuth>
        <AppLayout />
      </RequireAuth>
    ),
    children: [{ index: true, element: <DashboardPage /> }],
  },
  {
    // ⚠️ 刻意不加 RequireAuth：游客可从首页直接进来体验示例数据（FR-USR-08）
    path: '/upload',
    element: <AsideLayout />,
    children: [{ index: true, element: <UploadPage /> }],
  },
  {
    // 🔒 同上，游客链路必须能走到这里（DOD-EP4 硬验收）。
    //    EP-5 已把占位页替换为 UX-P06 完整表单。
    path: '/params',
    element: <AsideLayout />,
    children: [{ index: true, element: <ParamsPage /> }],
  },
  {
    // 🔒 EP-5 新增：执行进度（UX-P07）。同样**不加**守卫。
    //    任务 ID 走路径参数；展示用参数走查询串（见文件头说明）。
    path: '/running/:taskId',
    element: <AsideLayout />,
    children: [{ index: true, element: <RunningPage /> }],
  },
  {
    // 🔒 EP-5 新增占位：结果页（UX-P08 属 EP-6）。
    //    `UX-P07` 完成态的「查看预测结果」必须有落点，否则游客链路在最后
    //    一步断掉（FR-USR-08 要求「上传→参数→预测→结果」全程可达）。
    path: '/result/:taskId',
    element: <AsideLayout />,
    children: [{ index: true, element: <ResultPage /> }],
  },
  {
    // 🔒 EP-7 新增：历史任务（UX-P09）。**要求登录**（FR-HIS-01）。
    //    与游客链路相反 —— 历史里可能存在他人的预测结果，
    //    敏感度高于单次结果页，故三条记录类路由全部加守卫。
    path: '/history',
    element: (
      <RequireAuth>
        <AppLayout />
      </RequireAuth>
    ),
    children: [{ index: true, element: <HistoryPage /> }],
  },
  {
    // 🔒 EP-7：任务详情（UX-P10）。**不加守卫会 404** ——
    //    `/history` 是父路由，`/history/:taskId` 必须挂在同一父级下，
    //    否则 `AppLayout` 的导航与页头不会出现。
    path: '/history/:taskId',
    element: (
      <RequireAuth>
        <AppLayout />
      </RequireAuth>
    ),
    children: [{ index: true, element: <HistoryDetailPage /> }],
  },
  {
    // 🔒 EP-7：跨任务对比（UX-P11 / FR-HIS-04）。同样要求登录。
    path: '/compare',
    element: (
      <RequireAuth>
        <AppLayout />
      </RequireAuth>
    ),
    children: [{ index: true, element: <ComparePage /> }],
  },
  {
    // 🔒 FR-USR-07（P1）：个人资料 —— 查看账号信息、改昵称、改密码。
    //    挂 `RequireAuth`：本页全部操作都作用于「当前登录用户」，
    //    游客（`user_id=0`）没有可维护的账号实体。
    //    入口在顶栏用户区（`AppShell`），**不占侧栏 4 项 IA**。
    path: '/profile',
    element: (
      <RequireAuth>
        <AppLayout />
      </RequireAuth>
    ),
    children: [{ index: true, element: <ProfilePage /> }],
  },
  {
    // 🔒 EP-8：`AD-P01` 运营看板（`CH-01`）。
    //    `RequireAuth` **套在 `RequireAdmin` 之外** —— 顺序不能反：
    //    未登录时应该跳登录页；已登录但不是管理员才由 `RequireAdmin`
    //    给403 落点。两个守卫的职责是「先认证后授权」（`BE-41`），
    //    顺序颠倒会让未登录用户看到「无管理端权限」而不是「请登录」。
    path: '/admin',
    element: (
      <RequireAuth>
        <RequireAdmin>
          <AppLayout />
        </RequireAdmin>
      </RequireAuth>
    ),
    children: [{ index: true, element: <AdminDashboardPage /> }],
  },
  {
    // 🔒 EP-8：`AD-P02` 用户与任务管理（`CH-01`）。
    //    与 `/admin` 同级而非子级 —— 它们是两个独立页面，不共享父布局元素，
    //    挂在同一父级下是为了复用 `AppLayout`（导航 + 页头 + 页脚）。
    path: '/admin/manage',
    element: (
      <RequireAuth>
        <RequireAdmin>
          <AppLayout />
        </RequireAdmin>
      </RequireAuth>
    ),
    children: [{ index: true, element: <AdminManagePage /> }],
  },
];
