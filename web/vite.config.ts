/**
 * Vite 配置（依据技术文档 §2.7 端口与进程约定：前端 5173，proxy /api → 8000）
 * =============================================================================
 * 🔴 后端地址**不写死**：`VITE_API_TARGET` 覆盖，缺省仍是 8000。
 *
 *    写死的真实代价：`EP-4`/`EP-5` 的浏览器走查必须用**独立临时库**起后端
 *    （`DB_PATH=data/tmp/ep5_browser/app.db`），端口自然不能是 8000；
 *    代理写死 8000 时，前端能开、页面也能渲染，但**每个接口都 502/连不上** ——
 *    排查起来非常浪费时间（表现为「页面空着但控制台说网络错误」，
 *    而后端日志里一条请求都没有）。
 *
 *    例：
 *      VITE_API_TARGET=http://127.0.0.1:8010 npx vite --port 5180
 *
 * ⚠️ 前端**不读** `import.meta.env.VITE_*` 里的后端地址 ——
 *    地址只存在于 dev server 的代理层，浏览器永远只看到同源的 `/api`。
 *    这样也顺带避免了「前端打包产物里泄漏内网地址」。
 *
 * 🔴 `build.manualChunks`（`R-10`，2026-10-26）—— **不要删**。
 *    改动前：`index` 567.8 kB + `EChart` 1,036.1 kB（两张 chunk 混装业务与三方）。
 *    改动后：`index` 329.1 / `antd` 77.2 / `react` 162.8 / `zrender` 215.4 /
 *            `echarts` 822.6 kB。
 *
 *    📌 **实测校正**（原评审说「首屏多一次 1 MB 请求」，不完全准确）：
 *       echarts 走的是结果页的懒加载路由，`index.html` 一直**没有** preload 它，
 *       所以首屏总字节基本没变（569 → 569 kB）。
 *       真正的收益是**部署间的缓存复用**：业务代码改动时，需要重新下载的
 *       从 567.8 kB 降到 329.1 kB，react/antd/zrender/echarts 四块 hash 不变。
 *    按「变更频率」切块（echarts / zrender / antd / react 运行时）。
 *    ⚠️ 分包后 `index.html` 引用**多个** hash 文件，发布必须整体覆盖 `dist`，
 *    漏一个即白屏（`main.py::spa_fallback` 的 `no-store` 只管 HTML 不缓存）。
 * =============================================================================
 */
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const API_TARGET = process.env.VITE_API_TARGET || 'http://127.0.0.1:8000';

/**
 * 把模块 id 归到某个 vendor chunk；返回 `undefined` 表示「不干预，交给 Rollup」。
 *
 * ⚠️ 必须先把 `\` 归一成 `/`：Windows 上 Rollup 的 id 有时是反斜杠形式，
 *    直接 `includes('/node_modules/')` 会全部漏掉 —— 表现为「配置写了但没生效」。
 *
 * 🔴 **刻意没有 `return 'vendor'` 这种兜底**（第一次就踩了）：
 *    把所有「没点名」的依赖塞进同一个 `vendor`，会立刻造出
 *    `Circular chunk: vendor -> react -> vendor` / `vendor -> antd -> vendor`
 *    —— 因为 `echarts-for-react`（在 vendor）import react，而 react 侧又
 *    通过 `@remix-run/router`（也被塞进 vendor）反向依赖。循环 chunk 在
 *    Rollup 里是**警告而非报错**，但会在某些加载顺序下触发 TDZ
 *    （`Cannot access 'X' before initialization`），属于最难查的一类运行时故障。
 *    ⇒ 只点名「体量大且边界清晰」的四组，其余留在使用方 chunk 里。
 */
function chunkOf(id: string): string | undefined {
  const p = id.split('\\').join('/');
  if (!p.includes('/node_modules/')) return undefined;
  if (/\/node_modules\/echarts\//.test(p)) return 'echarts';
  // zrender 单独切：echarts 本体 minify 后约 1.0 MB，整块会一直顶着体积告警。
  if (/\/node_modules\/zrender\//.test(p)) return 'zrender';
  if (/\/node_modules\/(antd|@ant-design|rc-[^/]+)\//.test(p)) return 'antd';
  // `@remix-run/router` 是 react-router 的内部依赖，必须同块，否则 react 块反向依赖它。
  if (
    /\/node_modules\/(react|react-dom|react-router|react-router-dom|scheduler)\//.test(p) ||
    /\/node_modules\/@remix-run\//.test(p)
  )
    return 'react';
  return undefined;
}

export default defineConfig({
  plugins: [react()],
  build: {
    // ⚠️ 这不是把告警「调没了」，而是把门槛调到**已知上限之上**：
    //    echarts 单库 minify 后 822 kB（已切出 zrender 后），是这套依赖的物理下限。
    //    维持在 800 只会让一条永远成立、永远无法修复的告警常驻 ——
    //    告警一旦常驻，真问题就被淹掉了。900 = 「只有比 echarts 还大的块才报警」。
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        manualChunks: chunkOf,
      },
    },
  },
  server: {
    port: Number(process.env.VITE_PORT || 5173),
    strictPort: true,
    proxy: {
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
      },
      '/health': {
        target: API_TARGET,
        changeOrigin: true,
      },
    },
  },
});