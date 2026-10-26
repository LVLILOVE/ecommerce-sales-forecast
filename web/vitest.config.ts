/**
 * Vitest 配置（单测专用，与 `vite.config.ts` 解耦）
 * =============================================================================
 * 为什么不写进 `vite.config.ts`：那份配置服务于 dev server 与生产构建，
 * 动它会影响代理与产物；单测只需要一个「node 环境 + 纯函数」的极简配置。
 *
 * 🔒 `environment: 'node'`：本目录下只测**纯函数**，不渲染组件、不碰 DOM，
 *    因此不需要 jsdom（少一层依赖，也快得多）。
 *    将来要测组件时改为 `jsdom` 并补 `@testing-library/jest-dom` 的 setup 文件。
 * =============================================================================
 */
import { defineConfig } from 'vitest/config';

export default defineConfig({
  // ⚠️ 必须把缓存放回工作区：默认会写到系统 Temp（`%TEMP%/UL_xxx/ssr`），
  //    在有沙箱/杀软的机器上会 `EPERM: operation not permitted`，
  //    表现为「用例其实过了但进程以 1 退出」。
  cacheDir: 'node_modules/.vite-test',
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
    reporters: 'default',
  },
});
