/**
 * =============================================================================
 * `EP-9` 兼容性走查（`WS-9-06` / `DOD-EP9` 第 4、5 条）
 * -----------------------------------------------------------------------------
 * 覆盖 `DOD-EP9` 中**只能靠真实渲染判定**的部分：
 *   ① 🔒 **1366×768** 下全部页面无横向溢出、无空白页（`WS-9-06` 的分辨率项）
 *   ② 🔒 1600×900 对照（确认宽屏下布局不回退）
 *   ③ 🔒 §9.7 第 4 项 **首屏 ≤ 3 s**（`NFR-PERF-06`）
 *   ④ 🔒 consoleErrors = 0（`DOD-EP9` 第 5 条）
 *   ⑤ 浏览器覆盖面：Edge / Chrome / Firefox / Safari **逐个体检并如实登记**
 *
 * 用系统自带 Edge（`--headless=new`）+ 原生 CDP 驱动，不依赖 playwright
 * （沿用 `verify_ep6/7/8_browser.mjs` 的骨架，原因见其文件头）。
 *
 * -----------------------------------------------------------------------------
 * 🔒 关于「四个浏览器」这条硬要求的诚实处理
 * -----------------------------------------------------------------------------
 * `DOD-EP9` 原文是「Chrome / Firefox / Safari / Edge 最新两版；1366×768」。
 * 本脚本**不会**把没真跑过的浏览器记成通过，而是分成两类：
 *
 *   · `PASS`   —— 真的在浏览器里跑过且断言通过；
 *   · `BLOCKED` —— 环境里确实没有该浏览器，
 *                  **明写「未实测，不能据此判定通过」**，进测试报告的遗留问题清单。
 *
 * 驱动通道有两条（由 `BROWSERS[].driver` 决定）：
 *
 *   · `cdp`  —— Chromium 系（Edge / Chrome）原生 DevTools 协议，`class CDP`；
 *   · `bidi` —— Firefox 的 WebDriver BiDi。**Firefox 从 129 起已不再实现 CDP**，
 *              所以这里补了一层 `CdpOverBidi`：对外暴露与 `CDP` **完全相同**的
 *              `send / eval / on` 接口，内部把本脚本实际用到的那 6 个 CDP 方法
 *              翻译成 BiDi 命令。这样全部用例代码一行都不用改，就能在
 *              Firefox 上原样执行 —— 否则「支持 Firefox」会退化成
 *              复制一份 600 行的用例再去调试两遍。
 *   · `webkit` —— Safari。**Windows 上没有 Safari 发行版**，本机不可能有，
 *              故 `cands` 为空、恒记 `BLOCKED`（这不是「没测」，是「测不了」）。
 *
 * 用哪一款由环境变量 `BROWSER` 选（`edge` / `chrome` / `firefox`；
 * 缺省 `auto` = 取第一个本机存在的 Chromium 系）。**同一时刻只驱动一款**——
 * 一次跑一款、各自出一份报告，比在一个进程里并发三款更容易定位问题。
 *
 * ⚠️ 关于首屏口径：`NFR-PERF-06` 的 3 s 必须用**生产构建**测。
 *    Vite dev server 是逐模块未打包加载，首屏天然 > 3 s，用它测等于自欺。
 *    故本脚本优先测 `PREVIEW_URL`（`vite preview`，默认 4173）；
 *    不可达时**记 BLOCKED**，而不是退回 dev 口径假装通过。
 *
 * 用法（在**工作区根目录**执行，前后端须同时在线）：
 *   APP_URL=http://127.0.0.1:5199 API_URL=http://127.0.0.1:8021 \
 *   node scripts/verify_ep9_browser.mjs
 *
 * 选浏览器跑（一次一款，各自出一份报告）：
 *   BROWSER=chrome  … → outputs/ep9_browser_report_chrome.json
 *   BROWSER=firefox … → outputs/ep9_browser_report_firefox.json
 *   BROWSER=edge（缺省 auto，取本机第一个可用的 Chromium 系）
 *                    → outputs/ep9_browser_report.json（保持历史文件名）
 *
 * 找不到内核时可用环境变量直接指定：`CHROME_BIN` / `FIREFOX_BIN` / `EDGE_BIN`
 *
 * 可选：`PREVIEW_URL=http://127.0.0.1:4173`（生产构建首屏口径）
 * =============================================================================
 */
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const APP = process.env.APP_URL || 'http://127.0.0.1:5199';
const API = process.env.API_URL || 'http://127.0.0.1:8021';
const PREVIEW = process.env.PREVIEW_URL || 'http://127.0.0.1:4173';
const OUT = process.env.OUT_DIR || 'outputs/ep9-shots';
const PORT = Number(process.env.CDP_PORT || 9900 + (process.pid % 90));

// 种子管理员（邮箱可默认；**口令必须由环境变量提供**）
const ADMIN_EMAIL = process.env.EP9_ADMIN_EMAIL || 'admin@salesforecast.cn';
// 🔒 不设默认口令：本文件会入库（仓库公开），且管理端口令随时可能被修改。
const ADMIN_PASSWORD = process.env.EP9_ADMIN_PASSWORD || '';
if (!ADMIN_PASSWORD) {
  console.error('缺少管理端口令：请用 EP9_ADMIN_PASSWORD 环境变量提供（种子口令见 code/db/seed.sql）。');
  console.error('示例：EP9_ADMIN_PASSWORD=<口令> BROWSER=edge node scripts/verify_ep9_browser.mjs');
  process.exit(2);
}
const PASSWORD = process.env.EP9_PASSWORD || 'Verify123456';
// ⚠️ 带时间戳：邮箱全局唯一，固定邮箱第二次跑会「已存在」而失败。
//    走查脚本必须**可重复执行**。
const USER_EMAIL = `ep9b_${Date.now().toString(36)}@verify.com`;

const VIEWPORTS = [
  { w: 1366, h: 768, label: '1366x768', tag: 'narrow' },
  { w: 1600, h: 900, label: '1600x900', tag: 'wide' },
];

const OUT_ABS = path.resolve(OUT);
fs.mkdirSync(OUT_ABS, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// =============================================================================
// 浏览器体检：存在的实跑，缺失的如实登记为 BLOCKED
// =============================================================================
/** 本次要驱动哪一款：`edge` / `chrome` / `firefox`；`auto` = 第一个可用的 Chromium 系 */
const WANT_BROWSER = String(process.env.BROWSER || 'auto').toLowerCase();

const BROWSERS = [
  {
    name: 'Edge',
    key: 'edge',
    driver: 'cdp',
    cands: [
      process.env.EDGE_BIN,
      'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
      'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
    ],
  },
  {
    name: 'Chrome',
    key: 'chrome',
    driver: 'cdp',
    cands: [
      process.env.CHROME_BIN,
      'C:/Program Files/Google/Chrome/Application/chrome.exe',
      'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
      process.env.LOCALAPPDATA
        ? `${process.env.LOCALAPPDATA}/Google/Chrome/Application/chrome.exe`
        : null,
      // 便携版：本机没装 Chrome，用 Chrome for Testing 解包到工作区得到。
      // 与 Edge 同为 Chromium 内核、同走 CDP，故 `driver` 不变。
      path.resolve('data/tmp/browsers/chrome-win64/chrome.exe'),
    ],
  },
  {
    name: 'Firefox',
    key: 'firefox',
    // 🔒 Firefox ≥129 起**移除了 CDP**，只能走 WebDriver BiDi —— 由 `CdpOverBidi` 承接
    driver: 'bidi',
    cands: [
      process.env.FIREFOX_BIN,
      'C:/Program Files/Mozilla Firefox/firefox.exe',
      'C:/Program Files (x86)/Mozilla Firefox/firefox.exe',
      // 便携解包版：本机没有装 Firefox，用 `msiexec /a`（管理员安装）把官方 MSI
      // **解包到工作区**得到，不写注册表、不建快捷方式、不污染系统。
      path.resolve('data/tmp/browsers/firefox/firefox.exe'),
      path.resolve('data/tmp/browsers/firefox/Firefox/firefox.exe'),
    ],
  },
  {
    name: 'Safari',
    key: 'safari',
    driver: 'webkit',
    cands: [], // Safari 不在 Windows 上发行
  },
];
const BROWSER_STATUS = [];

// =============================================================================
const results = [];
const blocked = [];

function check(name, ok, detail = '') {
  results.push({ name, ok: !!ok, detail });
  console.log(`  ${ok ? '✅' : '❌'} ${name}${detail ? ` — ${detail}` : ''}`);
  return !!ok;
}

function markBlocked(name, reason) {
  blocked.push({ name, reason });
  console.log(`  ⚠️  BLOCKED ${name} — ${reason}`);
}

function section(t) {
  console.log(`\n=== ${t} ===`);
}

const NOISE = [
  /chrome-extension:\/\//i,
  /extensions::/i,
  /DevTools/i,
  /favicon/i,
  // React DevTools 的提示是 warning 级且与产品无关
  /Download the React DevTools/i,
];
const isNoise = (t) => NOISE.some((re) => re.test(t));

const consoleErrors = [];

// =============================================================================
// 布局探针
// =============================================================================
const LAYOUT_PROBE = `(() => {
  const de = document.documentElement;
  const body = document.body;
  // 找出**最宽的那个元素**，便于定位溢出元凶（只看结论没有抓手很难修）
  let worst = { sel: '', w: 0 };
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width > worst.w) {
      const id = el.id ? '#' + el.id : '';
      const cls = (el.className && typeof el.className === 'string')
        ? '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.') : '';
      worst = { sel: el.tagName.toLowerCase() + id + cls, w: Math.round(r.width) };
    }
  }
  const text = (body.innerText || '').replace(/\\s+/g, ' ').trim();
  return {
    innerWidth: window.innerWidth,
    scrollWidth: de.scrollWidth,
    bodyScrollWidth: body.scrollWidth,
    overflowX: de.scrollWidth > window.innerWidth + 2,
    textLen: text.length,
    hasH1: !!document.querySelector('h1'),
    hasMain: !!document.querySelector('main'),
    worst,
    path: location.pathname + location.search,
    // 异常态关键词：落到 404 / 错误边界时页面通常有这些字
    errorish: /页面不存在|出错了|Something went wrong|Unexpected Application Error/i.test(text),
  };
})()`;

// =============================================================================
class CDP {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    this.listeners = [];
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(JSON.stringify(msg.error)));
        else resolve(msg.result);
      } else if (msg.method) {
        for (const l of [...this.listeners]) l(msg);
      }
    });
  }
  on(fn) {
    this.listeners.push(fn);
    return () => { this.listeners = this.listeners.filter((x) => x !== fn); };
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`CDP 超时: ${method}`));
        }
      }, 120000);
    });
  }
  async eval(expression, awaitPromise = false) {
    const r = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise });
    if (r.exceptionDetails) throw new Error(`页面内 JS 异常：${r.exceptionDetails.text}`);
    return r.result.value;
  }
  /** 通道初始化（CDP 无需握手；Firefox 侧要建 session + 订阅事件） */
  async init() { /* no-op */ }
}

// =============================================================================
// WebDriver BiDi → CDP 兼容层
// -----------------------------------------------------------------------------
// Firefox ≥129 **移除了 CDP**，只提供 WebDriver BiDi。若为它单写一套用例，
// 就等于把上面 300 多行断言复制一遍 —— 两份代码从此必须同步维护，
// 迟早会漂移成「Edge 测到的和 Firefox 测到的不是同一件事」。
//
// 所以这里做反向的事：**把 BiDi 伪装成 CDP**。对外方法与 `class CDP`
// 完全一致（`on / send / eval / init`），内部只翻译本脚本真正用到的方法：
//
//   `Page.enable` / `Runtime.enable` / `DOM.enable`  → 空操作（改由订阅事件）
//   `Runtime.evaluate`                               → `script.evaluate`
//   `Page.navigate`                                  → `browsingContext.navigate`
//   `Emulation.setDeviceMetricsOverride`             → `browsingContext.setViewport`
//   `Page.captureScreenshot`                         → `browsingContext.captureScreenshot`
//
// 🔴 **最大的坑：BiDi 的返回值不是 JS 值**。`script.evaluate` 返回的是
//    RemoteValue —— 对象被序列化成 `[[key, RemoteValue], …]` 的**键值对数组**，
//    数字可能是字符串（`"NaN"` / `"Infinity"`）。若直接拿它当值用，
//    `p.overflowX` / `p.textLen` 全是 `undefined`，断言会**静默地全绿或全红**。
//    故必须过一遍 `fromRemoteValue()` 还原（含嵌套对象）。
// =============================================================================
function fromRemoteValue(rv) {
  if (rv === undefined || rv === null) return undefined;
  switch (rv.type) {
    case 'undefined': return undefined;
    case 'null': return null;
    case 'string': return rv.value;
    case 'boolean': return rv.value;
    case 'number': {
      // BiDi 把非有限数编码成字符串
      const v = rv.value;
      if (v === 'NaN') return NaN;
      if (v === 'Infinity') return Infinity;
      if (v === '-Infinity') return -Infinity;
      if (v === '-0') return -0;
      return Number(v);
    }
    case 'bigint': return Number(rv.value);
    case 'array':
    case 'set':
      return (rv.value || []).map(fromRemoteValue);
    case 'object':
    case 'map': {
      const out = {};
      for (const pair of rv.value || []) {
        if (!Array.isArray(pair)) continue;
        const [k, v] = pair;
        const key = typeof k === 'string' ? k : String(fromRemoteValue(k));
        out[key] = fromRemoteValue(v);
      }
      // Date / RegExp 等由 `type` 判定后原样交给调用方
      if (rv.value === undefined || rv.value === null) return rv.value === null ? null : out;
      return out;
    }
    default:
      return rv.value;
  }
}

class CdpOverBidi {
  constructor(ws) {
    this.mode = 'bidi';
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    this.listeners = [];
    this.ctx = null;
    this.log = [];
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.type === 'error') reject(new Error(`BiDi ${msg.error}: ${msg.message}`));
        else resolve(msg.result ?? {});
      } else if (msg.method) {
        for (const l of [...this.listeners]) l(this.#toCdpEvent(msg));
      }
    });
  }

  on(fn) {
    this.listeners.push(fn);
    return () => { this.listeners = this.listeners.filter((x) => x !== fn); };
  }

  /** 裸 BiDi 命令（不走 CDP 翻译） */
  raw(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`BiDi 超时: ${method}`));
        }
      }, 120000);
    });
  }

  /** 把 BiDi 的 `log.entryAdded` 事件改写成 CDP 的两个事件名 */
  #toCdpEvent(msg) {
    if (msg.method !== 'log.entryAdded') return { method: msg.method, params: msg.params };
    const e = msg.params || {};
    const text = e.text
      || (e.args || []).map((a) => a.value ?? a.description ?? '').join(' ');
    const s = String(text);
    // ⚠️ `type === 'javascript'` 才是未捕获异常；`console` 是应用自己打的
    //    （实探报文：console.error → `{type:'console', method:'error', level:'error'}`；
    //     未捕获异常 → `{type:'javascript', level:'error'}`）。
    if (e.type === 'javascript') {
      return {
        method: 'Runtime.exceptionThrown',
        params: { exceptionDetails: { text: s, exception: { description: s } } },
      };
    }
    // CDP 侧的判定只看 `params.type === 'error'`，所以这里必须把
    // **level 为 error 的都归成 'error'** —— 只看 `method` 会漏掉
    // 那些 method 非 'error' 但 level='error' 的条目（BiDi 允许多种组合）。
    const cdpType = (e.level === 'error' || e.method === 'error') ? 'error'
      : (e.level === 'warn' || e.method === 'warning' || e.method === 'warn') ? 'warning'
        : (e.method || e.level || 'log');
    return {
      method: 'Runtime.consoleAPICalled',
      params: { type: cdpType, args: [{ value: s }] },
    };
  }

  async init() {
    // ① **必须显式 `session.new`**。
    //    ⚠️ 别被 `session.status` 的 `{ready: true}` 骗了 —— 它只表示
    //    「服务端准备好接受一个新 session」，**不等于当前连接已有 session**。
    //    首轮据此跳过 `session.new`，紧接着的 `session.subscribe` 就报
    //    `invalid session id: WebDriver session does not exist, or is not active`。
    //    故这里无条件发；只有在「已存在」这类错误时才当正常忽略。
    try {
      await this.raw('session.new', { capabilities: {} });
    } catch (e) {
      if (!/already|exist/i.test(String(e.message))) throw e;
    }
    await this.raw('session.subscribe', { events: ['log.entryAdded'] });

    // ② 取一个可用的 browsing context（headless 启动带 about:blank，通常已有）
    const tree = await this.raw('browsingContext.getTree', { maxDepth: 1 });
    const contexts = tree?.contexts || [];
    if (!contexts.length) {
      const created = await this.raw('browsingContext.create', { type: 'tab' });
      this.ctx = created.context;
    } else {
      this.ctx = contexts[0].context;
    }
    return this.ctx;
  }

  /** CDP 方法名 → BiDi 命令的翻译表 */
  send(method, params = {}) {
    switch (method) {
      case 'Page.enable':
      case 'Runtime.enable':
      case 'DOM.enable':
        return Promise.resolve({});
      case 'Runtime.evaluate':
        return this.raw('script.evaluate', {
          expression: params.expression,
          target: { context: this.ctx },
          awaitPromise: !!params.awaitPromise,
          resultOwnership: 'none',
        }).then((r) => (r.type === 'exception'
          ? {
            exceptionDetails: {
              // 实探报文里异常原因就在 `exceptionDetails.text`（如 `Error: probe boom`）
              text: r.exceptionDetails?.text
                || JSON.stringify(r.exceptionDetails || r).slice(0, 400),
            },
          }
          : { result: { value: fromRemoteValue(r.result) } }));
      case 'Page.navigate':
        // `wait: 'none'` 才对得上 CDP `Page.navigate` 的语义（不等加载完成）；
        // 等待由调用方的 `document.readyState` 轮询负责。
        return this.raw('browsingContext.navigate', {
          context: this.ctx, url: params.url, wait: 'none',
        }).then(() => ({}));
      case 'Emulation.setDeviceMetricsOverride':
        return this.raw('browsingContext.setViewport', {
          context: this.ctx,
          viewport: { width: params.width, height: params.height },
          devicePixelRatio: params.deviceScaleFactor || 1,
        }).then(() => ({}));
      case 'Page.captureScreenshot':
        return this.raw('browsingContext.captureScreenshot', {
          context: this.ctx, format: { type: 'image/png' },
        }).then((r) => ({ data: r.data }));
      default:
        return Promise.reject(new Error(`CdpOverBidi 未实现的方法：${method}`));
    }
  }

  async eval(expression, awaitPromise = false) {
    const r = await this.send('Runtime.evaluate', { expression, awaitPromise });
    if (r.exceptionDetails) throw new Error(`页面内 JS 异常：${r.exceptionDetails.text}`);
    return r.result.value;
  }
}

async function waitForDevtools(port, timeoutMs = 30000, errBox = { err: '' }) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (r.ok) return await r.json();
    } catch { /* 未就绪 */ }
    await sleep(400);
  }
  throw new Error(`DevTools 端口未就绪(port=${port})\n${String(errBox.err).slice(-1200)}`);
}

/**
 * 等待 WebSocket 可连（**Firefox BiDi 专用**）。
 *
 * Firefox 没有 Chromium 那样的 `/json/version` HTTP 发现端点，BiDi 的
 * WebSocket 直接监听在调试端口上，所以「是否就绪」只能靠**试连**判断。
 */
async function waitForWs(url, timeoutMs = 60000) {
  const t0 = Date.now();
  let lastErr = '';
  while (Date.now() - t0 < timeoutMs) {
    try {
      return await new Promise((resolve, reject) => {
        const w = new WebSocket(url);
        const timer = setTimeout(() => {
          try { w.close(); } catch { /* ignore */ }
          reject(new Error('open 超时'));
        }, 4000);
        w.addEventListener('open', () => { clearTimeout(timer); resolve(w); }, { once: true });
        w.addEventListener('error', () => { clearTimeout(timer); reject(new Error('ws 握手失败')); }, { once: true });
      });
    } catch (e) {
      lastErr = e.message;
      await sleep(500);
    }
  }
  throw new Error(`WebSocket 未就绪(${url})：${lastErr}`);
}

// =============================================================================
// 后端直调（造数据用，避免走 UI 的不确定性）
// =============================================================================
async function api(pathname, { method = 'GET', token, body, form } = {}) {
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  let payload;
  if (form) {
    payload = form;
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  const res = await fetch(API + pathname, { method, headers, body: payload });
  const text = await res.text();
  try {
    return { status: res.status, json: JSON.parse(text) };
  } catch {
    return { status: res.status, json: null, text: text.slice(0, 200) };
  }
}

async function prepareData() {
  await api('/api/v1/auth/register', { method: 'POST', body: { email: USER_EMAIL, password: PASSWORD } });
  const login = await api('/api/v1/auth/login', { method: 'POST', body: { email: USER_EMAIL, password: PASSWORD } });
  const token = login.json?.data?.access_token;
  if (!token) throw new Error(`普通用户登录失败：${JSON.stringify(login.json).slice(0, 200)}`);

  const sample = await api('/api/v1/datasets/sample', { token });
  const dsId = sample.json?.data?.dataset?.id;
  if (!dsId) throw new Error(`加载示例数据失败：${JSON.stringify(sample.json).slice(0, 200)}`);

  const task = await api('/api/v1/predictions', {
    method: 'POST', token, body: { dataset_id: dsId, horizon: 7 },
  });
  const taskId = task.json?.data?.task_id;
  if (!taskId) throw new Error(`建任务失败：${JSON.stringify(task.json).slice(0, 200)}`);

  return { token, dsId, taskId, status: task.json?.data?.status };
}

// =============================================================================
// 主流程
// =============================================================================
let browser = null;
let browserErr = '';
// ⚠️ 提到模块作用域：`finally` 要能拿到它做清理。首轮写成 `main()` 内的
//    `const profile`，结果 `finally` 里够不着 —— 每次走查都往
//    `outputs/ep9-shots/` 留一个 70~80 MB 的浏览器 profile（实测 3 个残留 232 MB）。
let profileDir = '';

async function main() {
  section('0. 浏览器体检（实跑 vs 如实登记）');
  for (const b of BROWSERS) {
    const hit = (b.cands || []).filter(Boolean).find((p) => p && fs.existsSync(p));
    BROWSER_STATUS.push({ name: b.name, key: b.key, path: hit || null, driver: b.driver, ran: false });
    if (hit) {
      console.log(`  ✅ 找到 ${b.name}：${hit}`);
    } else {
      const why = b.name === 'Safari'
        ? 'Safari 不在 Windows 平台发行，本机不可能存在（这不是「没测」，是「测不了」）'
        : b.name === 'Firefox'
          ? '本机未安装 Firefox。可 `msiexec /a`（管理员安装）把官方 MSI 解包到 '
            + 'data/tmp/browsers/firefox 后重跑（不写注册表、不污染系统）'
          : '本机未安装该浏览器';
      markBlocked(`${b.name} 未实测`, why);
    }
  }

  // 选定本次要驱动的一款。**一次只驱动一款**：跑完一款出一份报告，
  // 比在一个进程里并发三款更容易定位「是谁挂了」。
  const usable = BROWSER_STATUS.filter((x) => x.path);
  const target = WANT_BROWSER === 'auto'
    ? (usable.find((x) => x.driver === 'cdp') || usable[0] || null)
    : (usable.find((x) => x.key === WANT_BROWSER) || null);
  if (!target) {
    throw new Error(
      `没有可用于本次走查的浏览器（BROWSER=${WANT_BROWSER}）—— 本机可用：`
      + (usable.map((x) => x.name).join(' / ') || '无'));
  }
  target.ran = true;
  // 截图目录：Edge 保持历史路径不动（既有 28 张与报告都在引用它），
  // 其余浏览器各占一个子目录，避免互相覆盖。
  const SHOTS = target.key === 'edge' ? OUT_ABS : path.join(OUT_ABS, target.key);
  console.log(`  ▶ 本次驱动：${target.name}（通道 ${target.driver}）`);


  section('1. 造数据（API 直调，避免 UI 交互的不确定性）');
  const data = await prepareData();
  check('普通用户 + 示例数据集 + 一个成功任务 已就绪',
    !!data.token && !!data.dsId && !!data.taskId,
    `dataset=${data.dsId} task=${data.taskId} status=${data.status}`);

  const adminLogin = await api('/api/v1/auth/login', {
    method: 'POST', body: { email: ADMIN_EMAIL, password: ADMIN_PASSWORD },
  });
  const adminToken = adminLogin.json?.data?.access_token || null;
  check('种子管理员可登录（管理端页面走查前置）', !!adminToken,
    adminToken ? '' : JSON.stringify(adminLogin.json).slice(0, 160));

  // ---------------------------------------------------------------------------
  section(`2. 启动 ${target.name} 无头实例（通道 ${target.driver}）`);
  profileDir = path.join(OUT_ABS, `.${target.key}-profile-${Date.now()}`);
  const launchArgs = target.driver === 'bidi'
    ? [
      // Firefox 的空格分隔写法与 Chromium 的 `--flag=value` 不同，别混用
      '-headless',
      '-no-remote',
      '-new-instance',
      '-profile', profileDir,
      '--remote-debugging-port', String(PORT),
      'about:blank',
    ]
    : [
      '--headless=new',
      '--disable-gpu',
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-dev-shm-usage',
      '--hide-scrollbars',
      '--disable-extensions',
      '--disable-background-networking',
      // 🔴 **必须绕过系统代理**（`EP-9` 实测踩坑）：
      //    本机环境设了 `HTTP_PROXY/HTTPS_PROXY=http://127.0.0.1:51141`，
      //    Chromium 无头会继承它 → 连 `http://127.0.0.1:5199` 这种本地地址
      //    也被塞进代理，结果拿到 502（代理连不上只监听 IPv6 的 vite），
      //    表现出来就是「页面全白、断言全失败」，很容易误判成前端崩了。
      '--no-proxy-server',
      '--proxy-bypass-list=<-loopback>',
      // 窗口尺寸只作兜底，真实视口由 Emulation.setDeviceMetricsOverride 定
      '--window-size=1600,900',
      `--remote-debugging-port=${PORT}`,
      `--user-data-dir=${profileDir}`,
      'about:blank',
    ];
  browser = spawn(target.path, launchArgs, { stdio: ['ignore', 'pipe', 'pipe'] });
  browser.stderr.on('data', (d) => { browserErr += d.toString(); });

  // 🔑 CDP 与 BiDi 的**连接方式不同**（都是踩出来的）：
  //    · CDP  —— 先等 HTTP 发现端点 `/json/version`，再从 `/json/list` 里挑一个
  //              page target，为**具体 tab** 建 WebSocket；
  //    · BiDi —— Firefox **没有** HTTP 发现端点（`/json/version` 返回 404），
  //              BiDi 的 WebSocket 直接开在调试端口上，且**路径是 `/session`**。
  //              根路径 `/` 会返回 `HTTP 200 + HTML`，Node 的 `WebSocket`
  //              只会报一个没有细节的 `non-101 status code` —— 这是本轮
  //              最费时间的一个坑（靠裸 TCP 手工握手对比才定位）。
  let ws;
  if (target.driver === 'bidi') {
    ws = await waitForWs(`ws://127.0.0.1:${PORT}/session`, 60000);
    console.log('  ✅ BiDi WebSocket 就绪（/session）');
  } else {
    await waitForDevtools(PORT, 30000, { err: browserErr });
    console.log('  ✅ DevTools 端口就绪');
    const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
    const page = (Array.isArray(list) ? list : []).find((t) => t.type === 'page') || (list || [])[0];
    if (!page) throw new Error('没有可用的 page target');
    ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((res, rej) => {
      ws.addEventListener('open', res, { once: true });
      ws.addEventListener('error', rej, { once: true });
    });
    console.log('  ✅ DevTools WebSocket 就绪');
  }
  const cdp = target.driver === 'bidi' ? new CdpOverBidi(ws) : new CDP(ws);

  cdp.on((msg) => {
    if (msg.method === 'Runtime.consoleAPICalled') {
      if (msg.params.type === 'error') {
        const t = (msg.params.args || []).map((a) => a.value ?? a.description ?? a.type).join(' ');
        const s = String(t).slice(0, 300);
        if (!isNoise(s)) consoleErrors.push({ level: msg.params.type, text: s });
      }
    }
    if (msg.method === 'Runtime.exceptionThrown') {
      const d = msg.params.exceptionDetails;
      const s = String((d.exception && (d.exception.description || d.exception.value)) || d.text).slice(0, 300);
      if (!isNoise(s)) consoleErrors.push({ level: 'exception', text: s });
    }
  });

  // ⚠️ `on()` 必须先于 `init()` —— BiDi 在 `init` 里才订阅事件，
  //    反过来的话首次导航之前的控制台报错会被丢掉（那正是最该抓的）。
  const ctx = await cdp.init();
  if (target.driver === 'bidi') console.log(`  ✅ BiDi 会话就绪（context=${ctx}）`);

  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');
  await cdp.send('DOM.enable');

  async function setViewport(w, h) {
    await cdp.send('Emulation.setDeviceMetricsOverride', {
      width: w, height: h, deviceScaleFactor: 1, mobile: false,
    });
  }

  async function goto(url, settle = 1800) {
    await cdp.send('Page.navigate', { url });
    const t0 = Date.now();
    while (Date.now() - t0 < 30000) {
      try {
        if ((await cdp.eval('document.readyState')) === 'complete') break;
      } catch { /* 导航中 */ }
      await sleep(200);
    }
    await sleep(settle);
  }

  async function shoot(folder) {
    const dir = path.join(SHOTS, folder);
    fs.mkdirSync(dir, { recursive: true });
    for (let i = 0; i < 3; i += 1) {
      try {
        const r = await Promise.race([
          cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true }),
          new Promise((_, rej) => setTimeout(() => rej(new Error('截图超时')), 20000)),
        ]);
        fs.writeFileSync(path.join(dir, 'app_screenshot.png'), Buffer.from(r.data, 'base64'));
        return true;
      } catch { await sleep(250); }
    }
    return false;
  }

  /** 逐页断言：横向溢出 / 非空 / 非异常态 /（登录态页）未跳离预期路径 */
  async function auditPage(url, label, vp, { requireContent = true, expectPath = null } = {}) {
    await goto(url);
    const p = await cdp.eval(LAYOUT_PROBE);
    const tag = `${vp.tag}`;
    check(`[${vp.label}] ${label} 无横向溢出`,
      !p.overflowX,
      p.overflowX
        ? `scrollWidth=${p.scrollWidth} > innerWidth=${p.innerWidth}；最宽元素=${p.worst.sel}(${p.worst.w}px)`
        : `scrollWidth=${p.scrollWidth}`);
    if (requireContent) {
      check(`[${vp.label}] ${label} 渲染出内容（非空白页）`,
        p.textLen > 30, `文本长度=${p.textLen}`);
    }
    check(`[${vp.label}] ${label} 未落到异常/404 态`, !p.errorish);
    // 🔒 **必须校验真的停在了预期路径上**。
    //    本项目有三条守卫链（`RequireAuth` / `RequireAdmin` / 游客链路），
    //    若凭据注入失败，受保护页会**静默跳到 `/login`**，而登录页同样是
    //    「无溢出、有内容、非异常态」—— 三条断言全部通过，整轮走查
    //    其实一条业务页都没测到。这类「断言全绿但什么都没测」是走查脚本
    //    最危险的失效模式，故单列一条路径断言把它钉死。
    if (expectPath) {
      const landed = String(p.path).split('?')[0];
      check(`[${vp.label}] ${label} 停在预期路径（鉴权/守卫通过）`,
        landed === expectPath, `期望 ${expectPath}，实际 ${landed}`);
    }
    await shoot(`${vp.tag}/${label.replace(/[^\w\u4e00-\u9fa5-]/g, '_')}`);
    return p;
  }

  // ---------------------------------------------------------------------------
  section('3. §9.7 第 4 项：首屏 ≤ 3 s（NFR-PERF-06，生产构建口径）');
  //
  // ⚠️ 必须放在正式走查**之前**做。`preview` 与 `APP` 不同 origin，且生产
  //    构建里 `VITE_API_BASE` 是相对路径、preview 不做代理 → 首页若发接口
  //    请求会 404 并在控制台留下错误。若在走查之后测，这些错误会混进
  //    「consoleErrors = 0」的计数里，把一条环境性噪音记成产品缺陷。
  //    这里用**快照 + 迁移**：preview 期间新增的错误单独归档，不进主数组。
  const previewErrors = [];
  {
    let previewUp = false;
    try {
      previewUp = (await fetch(PREVIEW, { method: 'GET' })).ok;
    } catch { previewUp = false; }

    if (previewUp) {
      await setViewport(1366, 768);
      const before = consoleErrors.length;
      // 🔒 首屏口径 = **FCP**（`first-contentful-paint`），不是 `loadEventEnd`。
      //
      //    `EP-9` 首轮用了 `loadEventEnd`，测出 41~48 ms 这种「好得不像真的」
      //    数字 —— 原因是第 2、3 次导航命中了浏览器 HTTP 缓存，而且 load 事件
      //    只代表资源加载完，对 SPA 而言它**早于 React 把首屏画出来**。
      //    用 FCP 才是「用户第一次看到内容」的时刻，也才是 `NFR-PERF-06`
      //    说的那件事。
      //
      //    连测 3 次取**中位数**：第 1 次是冷加载（无缓存），后两次含缓存，
      //    这正是真实施工场景下「首次访问 vs 再次访问」的分布。两次口径都记录，
      //    判定用中位数。
      const samples = [];
      const loads = [];
      // ⚠️ **Chrome 实测踩坑**：3 次导航只拿到 2 个 paint 样本（`n=2`）。
      //    原因是 `goto` 的 settle 只等 500 ms，冷加载时 React 还没把首屏
      //    画出来，`performance.getEntriesByType('paint')` 便是**空数组**。
      //    对策**不是**把断言放宽成「n≥2」—— 那等于自己削弱证据强度，
      //    而是给这次读取**加重试**：没值就再等 400 ms 重读，最多 6 轮。
      //    （Edge 上侥幸 3/3 是因为它启动更快，属运气，不是设计。）
      const PAINT_PROBE = `(() => {
        const paints = performance.getEntriesByType('paint');
        const fcp = paints.find((e) => e.name === 'first-contentful-paint');
        const nav = performance.getEntriesByType('navigation')[0];
        return {
          fcp: fcp ? Math.round(fcp.startTime) : -1,
          load: nav ? Math.round(nav.loadEventEnd || 0) : -1,
        };
      })()`;
      for (let i = 0; i < 3; i += 1) {
        await goto(PREVIEW, 500);
        let m = await cdp.eval(PAINT_PROBE);
        for (let k = 0; k < 6 && !(m && m.fcp > 0); k += 1) {
          await sleep(400);
          m = await cdp.eval(PAINT_PROBE);
        }
        if (m && m.fcp > 0) samples.push(m.fcp);
        if (m && m.load > 0) loads.push(m.load);
      }
      previewErrors.push(...consoleErrors.splice(before));
      const sorted = [...samples].sort((a, b) => a - b);
      const median = sorted.length ? sorted[Math.floor(sorted.length / 2)] : -1;
      check('🔒 首屏 FCP ≤ 3 s（生产构建 vite preview 口径）',
        median > 0 && median <= 3000,
        `FCP 中位数=${median}ms（${samples.join('/')}，3 次；首次=冷加载，后两次含缓存）`);
      check('首屏样本有效（paint timing 有值，3 次全取到）',
        samples.length === 3, `n=${samples.length}`);
      check('辅助口径：loadEventEnd 亦 ≤ 3 s',
        loads.length === 3 && Math.max(...loads) <= 3000,
        `load 样本=${loads.join('/')}ms`);
      check(`preview 域产生的控制台错误已单独归档（${previewErrors.length} 条，不计入主断言）`,
        true, previewErrors.slice(0, 3).map((e) => e.text).join(' || ') || '0 条');
    } else {
      markBlocked('§9.7 第 4 项 首屏 ≤ 3 s',
        `${PREVIEW} 不可达 —— 需先 npm run build && npm run preview。`
        + '刻意不用 dev server 口径代替：dev 逐模块加载，首屏天然 > 3 s，'
        + '测了也不能作为 NFR-PERF-06 的结论。');
      check('首屏项已明确处置（未实测必须给出理由，不许静默跳过）', true, '已登记为 BLOCKED');
    }
  }

  // ---------------------------------------------------------------------------
  section('4. 游客可达页（1366×768 + 1600×900）');
  // 先落到 APP 域下再注入 token —— 在 about:blank 上 localStorage 属 opaque origin，会抛异常。
  await goto(`${APP}/login`, 800);
  await cdp.eval('localStorage.clear()');

  const ANON = [
    ['/', 'UX-P01_首页'],
    ['/login', 'UX-P02_登录'],
    ['/register', 'UX-P03_注册'],
    ['/upload', 'UX-P05_上传_游客'],
    ['/params', 'UX-P06_参数_游客无参'],
  ];
  for (const vp of VIEWPORTS) {
    await setViewport(vp.w, vp.h);
    for (const [p, label] of ANON) {
      await auditPage(`${APP}${p}`, label, vp);
    }
  }

  // ---------------------------------------------------------------------------
  section('5. 普通用户页（1366×768 + 1600×900）');
  await setViewport(1366, 768);
  await goto(`${APP}/login`, 800);
  await cdp.eval(`localStorage.setItem('sp_token', ${JSON.stringify(data.token)})`);

  const USER_PAGES = [
    ['/dashboard', 'UX-P04_工作台', '/dashboard'],
    ['/upload', 'UX-P05_上传_登录', '/upload'],
    [`/params?datasetId=${data.dsId}`, 'UX-P06_参数', '/params'],
    [`/result/${data.taskId}`, 'UX-P08_结果', `/result/${data.taskId}`],
    ['/history', 'UX-P09_历史', '/history'],
    [`/history/${data.taskId}`, 'UX-P10_详情', `/history/${data.taskId}`],
    // ⚠️ 对比页的选中态存在 store 里（不落 URL），跨页刷新会回到空态。
    //    这里测的是**空态布局**；选中态已由 EP-7 的浏览器走查覆盖。
    ['/compare', 'UX-P11_对比_空态', '/compare'],
  ];
  for (const vp of VIEWPORTS) {
    await setViewport(vp.w, vp.h);
    for (const [p, label, expectPath] of USER_PAGES) {
      await auditPage(`${APP}${p}`, label, vp, { expectPath });
    }
  }

  // ---------------------------------------------------------------------------
  section('6. 管理端页（管理员身份）');
  if (adminToken) {
    await setViewport(1366, 768);
    await goto(`${APP}/login`, 800);
    await cdp.eval(`localStorage.setItem('sp_token', ${JSON.stringify(adminToken)})`);
    const ADMIN_PAGES = [
      ['/admin', 'AD-P01_看板', '/admin'],
      ['/admin/manage', 'AD-P02_管理', '/admin/manage'],
    ];
    for (const vp of VIEWPORTS) {
      await setViewport(vp.w, vp.h);
      for (const [p, label, expectPath] of ADMIN_PAGES) {
        await auditPage(`${APP}${p}`, label, vp, { expectPath });
      }
    }
  } else {
    markBlocked('管理端两页布局走查', '种子管理员登录失败，无法以管理员身份进入');
  }

  // ---------------------------------------------------------------------------
  section('7. 🔒 consoleErrors = 0');
  check('🔒 全程无 console error / 未捕获异常',
    consoleErrors.length === 0,
    consoleErrors.length ? consoleErrors.slice(0, 5).map((e) => e.text).join(' || ') : '0 条');

  const passed = results.filter((r) => r.ok).length;
  const total = results.length;
  console.log(`\n${'='.repeat(68)}`);
  console.log(`EP-9 兼容性走查（${target.name}）：${passed} / ${total} 通过；BLOCKED ${blocked.length} 项`);
  if (passed !== total) {
    console.log('\n未通过项：');
    results.filter((r) => !r.ok).forEach((r) => console.log(`  ❌ ${r.name} — ${r.detail}`));
  }
  if (blocked.length) {
    console.log('\n⚠️  BLOCKED（**未实测，不能据此判定通过**，须进遗留问题清单）：');
    blocked.forEach((b) => console.log(`  · ${b.name} — ${b.reason}`));
  }
  console.log(`${'='.repeat(68)}`);

  // 报告按浏览器分文件：跑第二款时不会覆盖第一款的结果。
  // Edge 保持历史文件名（既有报告、文档、脚本守卫都在引用它）。
  const reportFile = target.key === 'edge'
    ? 'ep9_browser_report.json'
    : `ep9_browser_report_${target.key}.json`;
  fs.writeFileSync(path.join(OUT_ABS, reportFile), JSON.stringify({
    stage: 'WS-9-06 兼容性走查',
    browser: { name: target.name, key: target.key, driver: target.driver, path: target.path },
    app: APP, api: API, preview: PREVIEW,
    viewports: VIEWPORTS.map((v) => v.label),
    passed, total,
    browsers: BROWSER_STATUS,
    blocked,
    console_errors: consoleErrors,
    items: results,
    checked_at: new Date().toISOString(),
  }, null, 2), 'utf8');

  return passed === total ? 0 : 1;
}

let code = 1;
try {
  code = await main();
} catch (e) {
  console.error('\n[FATAL]', e.message);
  if (browserErr) console.error(browserErr.slice(-1500));
} finally {
  // 🔒 必须杀**进程树**（EP-4 踩坑 #9）：只 kill 父进程会留下持有 profile 锁的
  //    子进程，导致下次静默挂起。
  try {
    if (browser) spawn('taskkill', ['/PID', String(browser.pid), '/T', '/F'], { stdio: 'ignore' });
  } catch { /* 已退出 */ }

  // 🔒 **必须等进程真的退出，再删 profile**（`EP-9` 收尾补测踩坑，见 `T-26`）。
  //
  //    首版是「`taskkill` 之后立刻 `rmSync`」，注释还写着「浏览器还活着时删会被
  //    文件锁挡住」—— 以为放在后面就够了。实际 `spawn('taskkill')` 是**异步**的：
  //    它刚发出，浏览器子进程还持有 profile 里的 SQLite/缓存文件锁，
  //    `rmSync` 必然抛错，再被这里的 `catch` 吞掉 → **每轮静默留一个 60~80 MB
  //    的 profile 目录**。三款浏览器跑一轮就攒下 4 个（200+ MB），
  //    这正是 `P3-03` 那个「残留 1.65 GB」问题的成因在重演。
  //    → 所以改为「等 1.5 s + 最多 6 次重试，删到目录真的不存在为止」。
  if (profileDir) {
    await sleep(1500);
    for (let i = 0; i < 6; i += 1) {
      try { fs.rmSync(profileDir, { recursive: true, force: true }); } catch { /* 文件锁，重试 */ }
      if (!fs.existsSync(profileDir)) break;
      await sleep(1000);
    }
    if (fs.existsSync(profileDir)) {
      console.warn(`  ⚠️ profile 未能清理（仍有文件锁）：${profileDir}`);
    }
  }
}
process.exit(code);
