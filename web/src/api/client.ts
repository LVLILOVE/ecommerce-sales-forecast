/**
 * =============================================================================
 * 电商销量预测系统 · 前端 API 客户端
 * -----------------------------------------------------------------------------
 * 版本     : V1.0
 * 依据文档 : 《开发技术文档 V1.0》§5.1.3（通用约定）、§7.3（错误处理约定）
 * 依赖     : axios ^1.7, ./types.ts
 * 落位建议 : src/api/client.ts + src/api/*.ts
 * -----------------------------------------------------------------------------
 * 关键约定：
 *   1. baseURL 来自 import.meta.env.VITE_API_BASE，默认 /api/v1
 *      开发期由 Vite proxy 转发到 http://127.0.0.1:8000，避免跨域。
 *   2. 响应拦截器统一拆包 ApiResponse<T>，业务代码只拿 data，
 *      不重复写 res.data.data。
 *   3. 401 统一处理：清 Token → 跳登录 → 保留当前草稿参数
 *      （开发方案 §2.2 B12「跳登录并保留草稿参数」）。
 *   4. 失败时抛出 ApiError（携带业务 code），由调用方或全局兜底决定提示。
 * =============================================================================
 */

import axios, { AxiosError, type AxiosInstance } from 'axios';
import { ERR, type ApiResponse, type Paginated } from '../types';

/**
 * 客户端本地错误码 —— **不是**后端业务码。
 *
 * 用于「响应根本不像本系统的信封」这类**本地**判定。刻意用一个不与任何
 * 4xxxx / 5xxxx 业务码重合的值，避免页面按错误码分支时误命中某条业务分支。
 */
const CLIENT_BAD_RESPONSE = -1;

/** 业务错误：保留后端 code，便于按错误码分支处理 */
export class ApiError extends Error {
  constructor(
    public readonly code: number,
    message: string,
    public readonly data?: unknown,
  ) {
    /**
     * 🔒 **message 永不空串**（系统性修法）。
     *
     * 背景：多个页面用「字符串真值性」当错误判别（`if (error)` / `{error && ...}`）。
     * 一旦某个错误对象的 `message` 是空串，它就会被当成「没出错」——
     * 页面永久停在 loading、或静默降级，而且**控制台什么都没有**。
     * 与其要求每个调用方都记得改成 `error !== null`，不如在这里兜住：
     * 保证任何 `ApiError` 都带可读文案。
     */
    super(message || `请求失败（错误码 ${code}）`);
    this.name = 'ApiError';
  }
}

const TOKEN_KEY = 'sp_token';

export const tokenStore = {
  get: (): string | null => localStorage.getItem(TOKEN_KEY),
  set: (t: string): void => localStorage.setItem(TOKEN_KEY, t),
  clear: (): void => localStorage.removeItem(TOKEN_KEY),
};

/** 登录失效时的回调，由路由层注入，避免 api 层依赖 router */
let onUnauthorized: (() => void) | null = null;
export function registerUnauthorizedHandler(fn: () => void): void {
  onUnauthorized = fn;
}

export function createClient(): AxiosInstance {
  const instance = axios.create({
    baseURL: import.meta.env.VITE_API_BASE ?? '/api/v1',
    timeout: 90_000, // 预测任务 SLA 60s，留 30s 余量
    headers: { 'Content-Type': 'application/json' },
  });

  instance.interceptors.request.use((config) => {
    const token = tokenStore.get();
    if (token) {
      // 标准头（直连 / Nginx 部署用）
      config.headers.Authorization = `Bearer ${token}`;
      // 🔴 备用头（2026-10-19）：部分托管平台的网关会**注入并覆盖**
      //    `Authorization`，导致客户端真正的 token 被丢弃，表现为
      //    「登录成功但立刻被 401 退回登录页」。后端 `deps._pick_token()`
      //    对 X-Auth-Token 的优先级高于 Authorization，因此这条能穿透网关。
      //    直连部署时两者内容一致，不会产生副作用。
      config.headers['X-Auth-Token'] = token;
    }
    return config;
  });

  instance.interceptors.response.use(
    (response) => response,
    async (error: AxiosError<ApiResponse<unknown>>) => {
      // 网络层错误（无响应）
      if (!error.response) {
        const isTimeout = error.code === 'ECONNABORTED';
        throw new ApiError(
          ERR.INFER_FAIL,
          isTimeout
            ? '请求超时。若任务数据量较大，请稍后在历史记录中查看结果'
            : '无法连接服务，请确认后端已启动',
        );
      }

      /**
       * 🔴 **非 2xx 的响应体不一定是 JSON**（`R-09`）。
       *
       * 后端从 2026-10-26 起把 `code >= 50000` 映射成真实 HTTP 500，
       * 于是「服务端故障」这类响应**不再进 success 分支**，改由这里处理。
       * 两种形态都要能吃下：
       *   ① 普通接口 → `error.response.data` 是已解析的 JSON；
       *   ② `responseType: 'blob'` 的下载接口（导出 / 错误明细）→ 它是 **Blob**，
       *      直接读 `body.code` 恒为 `undefined`，会把 50002「文件已丢失」
       *      降级成一句「请求失败（HTTP 500）」，用户看不出该重传数据。
       *
       * 注意这里是 `async`：`Blob.text()` 是异步的。响应拦截器的拒绝处理器
       * 允许返回 Promise，`await` 之后再 `throw` 仍会正确冒泡到调用方。
       */
      const raw: unknown = error.response.data;
      let body: ApiResponse<unknown> | null =
        raw !== null && typeof raw === 'object' && !(raw instanceof Blob)
          ? (raw as ApiResponse<unknown>)
          : null;
      if (raw instanceof Blob && (raw.type || '').includes('json')) {
        try {
          body = JSON.parse(await raw.text()) as ApiResponse<unknown>;
        } catch {
          body = null;
        }
      }

      const code =
        typeof body?.code === 'number' ? body.code : error.response.status * 100;
      const message =
        body?.message ?? `请求失败（HTTP ${error.response.status}）`;

      if (error.response.status === 401) {
        tokenStore.clear();
        onUnauthorized?.();
      }

      throw new ApiError(code, message, body?.data);
    },
  );

  return instance;
}

export const http = createClient();

/** 统一解包：后端永远是 {code, message, data}，HTTP 层失败已在拦截器抛出 */
async function unwrap<T>(p: Promise<{ data: ApiResponse<T> }>): Promise<T> {
  const res = await p;
  const body = res.data;

  /**
   * 🔴 **先校验信封形状**（`D-06`）。
   *
   * 反例（真实发生过）：前端 dev server 的接口代理未生效时，`/api/v1/*` 会被
   * Vite 的 SPA 兜底接走，以 **`text/html` + HTTP 200** 返回 `index.html`。
   * 此时 `body` 是字符串，`body.code` 为 `undefined` —— 原写法
   * `if (body.code !== 0)` 仍然成立，于是抛出一个**连 message 都没有**的
   * `ApiError(undefined, undefined)`，各页 `catch` 后**静默降级**，
   * 甚至（`ResultPage`）永久停在「正在加载…」。故障被完全掩盖。
   *
   * 这里显式识别并给出**可执行**的文案，让这类环境故障一眼可见。
   */
  if (body === null || typeof body !== 'object' || typeof body.code !== 'number') {
    throw new ApiError(
      CLIENT_BAD_RESPONSE,
      '服务返回了非预期的内容（不是本系统的响应格式）。通常是未连到后端，' +
        '或前端 dev server 的接口代理未生效 —— 请在浏览器网络面板确认 /api 请求的响应类型是 application/json。',
    );
  }

  if (body.code !== 0) {
    throw new ApiError(body.code, body.message, body.data);
  }
  return body.data;
}

/**
 * 二进制响应的统一解包（下载错误明细 / 模板 / 结果导出共用）。
 *
 * ⚠️ 业务失败在本项目里是 **HTTP 200 + JSON body**（`core/errors.py::BizError`），
 * 而 `responseType: 'blob'` 会让 axios 把那份 JSON 也当二进制收下 —— 于是
 * 「下载一个不存在错误明细的数据集」会**成功下载到一个内容为 JSON 的 .csv**，
 * 用户打开才发现。这里按 `Content-Type` 识别并还原成 `ApiError`，
 * 让调用方走与其它接口一致的错误分支。
 */
async function unwrapBlob(p: Promise<{ data: Blob }>): Promise<Blob> {
  const res = await p;
  const blob = res.data as Blob;
  if ((blob.type || '').includes('application/json')) {
    let body: ApiResponse<unknown> | null = null;
    try {
      body = JSON.parse(await blob.text()) as ApiResponse<unknown>;
    } catch {
      body = null;
    }
    if (body && typeof body.code === 'number') {
      throw new ApiError(body.code, body.message, body.data);
    }
  }

  /**
   * 🔴 **HTML 兜底也要拦**（`D-06` 的姊妹场景）。
   *
   * 代理失效时 `/api/...` 返回的是 `text/html` 的 `index.html`。若不拦，
   * 这里会把它当普通二进制交出去，用户就**成功下载到一个内容其实是网页的
   * .csv / .xlsx** —— 打开才发现，比直接报错更糟。
   */
  if ((blob.type || '').includes('text/html')) {
    throw new ApiError(
      CLIENT_BAD_RESPONSE,
      '下载失败：服务返回的是网页而不是文件。通常是未连到后端，或前端 dev server 的接口代理未生效。',
    );
  }

  return blob;
}

/* =============================================================================
 * 认证                                                 API-01 ~ API-05
 * ========================================================================== */

export const authApi = {
  register: (req: import('../types').RegisterRequest) =>
    unwrap<import('../types').RegisterResponse>(http.post('/auth/register', req)),

  login: (req: import('../types').LoginRequest) =>
    unwrap<import('../types').LoginResponse>(http.post('/auth/login', req)),

  logout: () => unwrap<null>(http.post('/auth/logout')),

  me: () => unwrap<import('../types').UserProfile>(http.get('/users/me')),

  updateMe: (req: import('../types').UpdateUserRequest) =>
    unwrap<import('../types').UserProfile>(http.patch('/users/me', req)),
};

/* =============================================================================
 * 数据集                                               API-06 ~ API-11
 * ========================================================================== */

export const datasetApi = {
  /** 上传 CSV：走 multipart，带上传进度回调（FR-UPL-04 进度条） */
  upload: (
    file: File,
    onProgress?: (percent: number) => void,
  ): Promise<import('../types').UploadDatasetResponse> => {
    const form = new FormData();
    form.append('file', file);
    return unwrap(
      http.post('/datasets', form, {
        headers: { 'Content-Type': 'multipart/form-data' },
        onUploadProgress: (e) => {
          if (onProgress && e.total) {
            onProgress(Math.round((e.loaded / e.total) * 100));
          }
        },
      }),
    );
  },

  /** 一键加载示例数据：幂等，重复调用返回同一 dataset_id */
  loadSample: () =>
    unwrap<import('../types').UploadDatasetResponse>(http.get('/datasets/sample')),

  /**
   * 数据集详情（`API-09`）。
   *
   * ⚠️ `EP-4` 遗留缺陷修复（`EP-5`）：这里的泛型原先写成 `DatasetSummary`，
   *    而后端返回的是 `DatasetDetailResponse`（`{dataset, level_preview,
   *    estimated_ms, over_budget_risk, estimate_by_horizon}`）。
   *    类型断言只是让 TS 闭了嘴 —— 运行时 `detail.file_name` / `detail.id`
   *    全部是 `undefined`，参数页会渲染出空白概要。EP-4 的浏览器走查没覆盖
   *    参数页深层内容，所以没暴露。
   */
  detail: (datasetId: number) =>
    unwrap<import('../types').DatasetDetailResponse>(http.get(`/datasets/${datasetId}`)),

  /**
   * 数据预览（`API-10`）：`keyword` 走**服务端**模糊匹配（商品 / 仓库），
   * 而不是只过滤已返回的那 20 行 —— 否则用户搜「第 300 行的商品」永远搜不到，
   * 会误判成「数据里没有这个商品」。
   */
  preview: (datasetId: number, limit = 20, keyword?: string) =>
    unwrap<import('../types').DatasetPreview>(
      http.get(`/datasets/${datasetId}/preview`, {
        params: keyword ? { limit, keyword } : { limit },
      }),
    ),

  /** 错误明细下载（FR-UPL-06）：返回 Blob，由调用方触发下载 */
  downloadErrors: (datasetId: number): Promise<Blob> =>
    unwrapBlob(http.get(`/datasets/${datasetId}/errors`, { responseType: 'blob' })),

  /** 标准模板下载（FR-UPL-08） */
  downloadTemplate: (): Promise<Blob> =>
    unwrapBlob(http.get('/datasets/template', { responseType: 'blob' })),

  /** 断档处置（FR-UPL-13）：必须由用户显式选择后才继续 */
  resolveGap: (req: import('../types').ResolveGapRequest) =>
    unwrap<import('../types').UploadDatasetResponse>(http.post('/datasets/gap', req)),
};

/* =============================================================================
 * 预测任务                                             API-12 ~ API-15
 * ========================================================================== */

export const predictApi = {
  create: (req: import('../types').CreateTaskRequest) =>
    unwrap<import('../types').CreateTaskResponse>(http.post('/predictions', req)),

  status: (taskId: number) =>
    unwrap<import('../types').TaskStatusResponse>(
      http.get(`/predictions/${taskId}/status`),
    ),

  result: (taskId: number) =>
    unwrap<import('../types').PredictionResultResponse>(
      http.get(`/predictions/${taskId}`),
    ),

  /** 明细表分页（结果页表格用，避免单响应过大） */
  detail: (taskId: number, query: import('../types').PredictionDetailQuery = {}) =>
    unwrap<Paginated<import('../types').PredictionRow>>(
      http.get(`/predictions/${taskId}/detail`, { params: query }),
    ),

  /** 导出：format 为 csv 或 xlsx（FR-VIS-05） */
  export: (taskId: number, format: 'csv' | 'xlsx' = 'csv'): Promise<Blob> =>
    unwrapBlob(
      http.get(`/predictions/${taskId}/export`, {
        params: { format },
        responseType: 'blob',
      }),
    ),
};

/* =============================================================================
 * 历史与对比                                           API-16 ~ API-19
 * ========================================================================== */

export const historyApi = {
  list: (query: import('../types').HistoryQuery = {}) =>
    unwrap<Paginated<import('../types').HistoryItem>>(
      http.get('/history', { params: query }),
    ),

  detail: (taskId: number) =>
    unwrap<import('../types').PredictionResultResponse>(http.get(`/history/${taskId}`)),

  compare: (req: import('../types').CompareRequest) =>
    unwrap<import('../types').CompareResponse>(http.post('/history/compare', req)),

  /** 软删除（FR-HIS-05），需二次确认后调用 */
  remove: (taskId: number) => unwrap<null>(http.delete(`/history/${taskId}`)),

  /**
   * 历史记录导出（`FR-HIS-03`，`API-19a`）。
   *
   * 🔒 **列定义与结果页导出完全一致**（PRD 原文：「列定义同 `FR-VIS-05`」），
   *    后端也是同一个 `prepare_export` —— 所以这里只需要换 URL 前缀。
   *    必须登录（与历史详情同理：历史里是别人的结果，比结果页更敏感）。
   */
  export: (taskId: number, format: 'csv' | 'xlsx' = 'csv'): Promise<Blob> =>
    unwrapBlob(
      http.get(`/history/${taskId}/export`, {
        params: { format },
        responseType: 'blob',
      }),
    ),

  /**
   * `UX-P04` 工作台概览。
   *
   * 🔴 **不要在前端自行聚合**统计卡：工作台「最近任务」与历史列表首屏
   *    必须是同一份数据（`DOD-EP7` 验收点）。前端再聚合一次，两处口径
   *    一旦分叉就会对不上，而这种差异极难被发现。
   */
  dashboard: () =>
    unwrap<import('../types').WorkbenchSummary>(
      http.get('/history/dashboard'),
    ),
};

/* =============================================================================
 * 系统                                                 API-20
 * ========================================================================== */

export const systemApi = {
  /**
   * 健康检查（`API-21`）。
   *
   * 🔴 `EP-5` 修复的遗留缺陷：`API-21` 挂在**根路径** `/health`（不在 `/api/v1` 下，
   *    见 `app/api/health.py` 的文件头说明），而 `http` 实例的 `baseURL` 是
   *    `/api/v1` —— 原先写作 `http.get('/health')` 会打到 `/api/v1/health`，
   *    得到 404。此前该方法**没有任何调用方**，所以缺陷一直潜伏；
   *    参数页要用它取「启用中的模型版本」时才会暴露。
   *    故此处显式剥掉 baseURL 的路径部分，只保留 origin。
   */
  health: () => unwrap<import('../types').HealthResponse>(axios.get(healthUrl())),
};

/** `API-21` 的绝对地址：从 `VITE_API_BASE` 中剥掉路径，只保留 origin + `/health` */
function healthUrl(): string {
  const base = import.meta.env.VITE_API_BASE ?? '/api/v1';
  try {
    // 相对 base（默认 '/api/v1'）以当前 origin 解析，因此开发期仍走 Vite proxy
    return `${new URL(base, window.location.origin).origin}/health`;
  } catch {
    return '/health';
  }
}

/* =============================================================================
 * 工具：触发浏览器下载（导出与模板共用）
 * ========================================================================== */

export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

/** 结果导出统一文件名：预测结果_{taskId}_{YYYYMMDD}.csv */
export function exportFilename(taskId: number, ext: 'csv' | 'xlsx'): string {
  const d = new Date();
  const stamp = `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, '0')}${String(
    d.getDate(),
  ).padStart(2, '0')}`;
  return `预测结果_${taskId}_${stamp}.${ext}`;
}

/* =============================================================================
 * 管理端（`CH-01`）                                    API-22 ~ API-30
 * ========================================================================== */

/**
 * 🔒 本组接口是数据隔离红线（`NFR-SEC-02`）的**唯一显式例外**。
 *    前端侧的对应纪律：**非管理员不应看到任何管理端入口**
 *    （导航项与路由都由 `isAdmin` 把关，见 `AppShell` 与 `RequireAdmin`），
 *    但**前端隐藏不等于安全** —— 真正的判定在后端 `get_current_admin`，
 *    前端只是「不把不该看的入口摆出来」。
 */
export const adminApi = {
  /** `API-22` 运营看板概览 */
  overview: (days = 30) =>
    unwrap<import('../types').AdminOverview>(http.get('/admin/overview', { params: { days } })),

  /** `API-23` 用户列表（🔒 响应不含密码哈希） */
  listUsers: (query: import('../types').AdminUserQuery = {}) =>
    unwrap<Paginated<import('../types').AdminUserBrief>>(
      http.get('/admin/users', { params: query }),
    ),

  /**
   * `API-24` 启停用 / 角色变更。
   *
   * ⚠️ 服务层有三条自我保护（不能停用/降级自己、不能动最后一个管理员），
   *    失败时抛 `40012`，**message 里带原因**—— 前端直接展示即可，
   *    不要自己猜文案。
   */
  updateUser: (
    userId: number,
    payload: { status?: 0 | 1; role?: 'user' | 'admin' },
  ) =>
    unwrap<{ user: import('../types').AdminUserBrief }>(
      http.patch(`/admin/users/${userId}`, payload),
    ),

  /**
   * `API-25` 重置密码。
   *
   * 🔒 服务层会同时 `token_version + 1`，该用户全部旧 Token 立即失效。
   *    前端**不需要**（也不应该）额外调任何「踢下线」接口。
   */
  resetPassword: (userId: number, newPassword: string) =>
    unwrap<import('../types').ResetPasswordResult>(
      http.post(`/admin/users/${userId}/reset-password`, { new_password: newPassword }),
    ),

  /** `API-26` 全量任务列表（🔒 含已软删除的，`is_deleted` 标记） */
  listTasks: (query: import('../types').AdminTaskQuery = {}) =>
    unwrap<Paginated<import('../types').AdminTaskBrief>>(
      http.get('/admin/tasks', { params: query }),
    ),

  /**
   * `API-27` 强制删除任务（**硬删除**，不可恢复）。
   *
   * 🔒 二次确认文案必须与用户侧软删除区分清楚：
   *    这里写「此操作不可恢复」，不能写「可在回收路径恢复」——
   *    用户侧 `API-20` 才是可恢复的。混淆会让人不敢删，或误以为还能恢复。
   */
  forceDeleteTask: (taskId: number) =>
    unwrap<import('../types').AdminDeleteResult>(http.delete(`/admin/tasks/${taskId}`)),

  /** `API-28` 模型版本列表（📌 与 `API-29` 必须分离，`QD-07`） */
  listModels: () => unwrap<import('../types').AdminModelList>(http.get('/admin/models')),

  /**
   * `API-29` 切换启用版本。
   *
   * 🔒 返回的 `warnings` **必须展示**（`BE-ADM-03`）：`.env` 的
   *    `ACTIVE_MODEL` 未同步时，数据库切了但进程下次重启会回退，
   *    悄悄成功等于制造静默故障。
   */
  switchModel: (version: string) =>
    unwrap<import('../types').SwitchModelResult>(
      http.patch(`/admin/models/${version}`, { is_active: true }),
    ),

  /** `API-30` 操作日志查询（`sub_action` 已由后端结构化拆好） */
  listLogs: (query: import('../types').AdminLogQuery = {}) =>
    unwrap<Paginated<import('../types').AdminLogBrief>>(
      http.get('/admin/logs', { params: query }),
    ),
};
