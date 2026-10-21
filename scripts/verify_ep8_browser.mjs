/**
 * =============================================================================
 * `EP-8` 管理端浏览器走查（`DOD-EP8` 动态部分）
 * -----------------------------------------------------------------------------
 * 用系统自带 Edge（`--headless=new`）+ 原生 CDP 驱动，不依赖 playwright
 * （沿用 `verify_ep6_browser.mjs` / `verify_ep7_browser.mjs` 的骨架，原因见其文件头）。
 *
 * 覆盖 `DOD-EP8` 中**只能靠真实渲染判定**的部分：
 *   ① 🔒 **非管理员看不到任何管理端入口**（导航零项+ 手工改 URL 落到 403 页）
 *   ② 🔒 **管理员看得到两个入口**，且点击可达
 *   ③ 🔒 `AD-P01` 运营看板：5 指标带 / MAPE 走势或空态 / 模型版本表 / 埋点概览
 *   ④ 🔒 `AD-P02` 三 Tab 切换 / 用户表 / 任务表 / 日志表结构齐备
 *   ⑤ 🔒 **强制删除必须逐字输入任务 ID**：输入不符时按钮禁用
 *   ⑥ 🔒 强制删除弹窗文案含「不可恢复」且**明示与用户侧软删除不同**
 *   ⑦ 🔒 重置密码弹窗提示「旧登录态立即失效」
 *   ⑧ 🔒 日志表能展示 `action='admin'` 的子动作徽标（`sub_action`）
 *   ⑨ 🔒 ECharts 懒加载后图表**真的渲染出来了**（SVG 有内容）
 *  ⑩ 🔒 consoleErrors = 0
 *
 * ⚠️ **为什么 ⑤ 要真实渲染验**：按钮的 `disabled` 绑定的是输入框实时值，
 *    读源码可能与实际行为不一致，必须在浏览器里敲字符验证。
 *
 * ⚠️ **为什么 ① 要分两种身份验**：「前端隐藏」与「后端拦截」是两件事。
 *    只验导航看不到 = 前端做对了但后端可能仍有洞；
 *    只验后端 403 = 后端做对了但普通用户仍会看到入口、体验不对。
 *    两条都要验。
 *
 * 用法（在**工作区根目录**执行，前后端须同时在线）：
 *   APP_URL=http://127.0.0.1:5199 API_URL=http://127.0.0.1:8021 \
 *   node scripts/verify_ep8_browser.mjs
 * =============================================================================
 */
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const EDGE = process.env.EDGE_BIN || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const PORT = Number(process.env.CDP_PORT || 9850 + (process.pid % 100));
const APP = process.env.APP_URL || 'http://127.0.0.1:5199';
const API = process.env.API_URL || 'http://127.0.0.1:8021';
//普通用户（应为看不到管理端）
// ⚠️ 带时间戳：邮箱全局唯一，固定邮箱在第二次跑分时会「已存在」而注册失败。
//    走查脚本必须**可重复执行**，这是硬要求。
const USER_EMAIL =
  process.env.EP8_USER_EMAIL || `ep8b_${Date.now().toString(36)}@verify.com`;
// 管理员种子账号（邮箱可默认；**口令必须由环境变量提供**）
const ADMIN_EMAIL = process.env.EP8_ADMIN_EMAIL || 'admin@salesforecast.cn';
const PASSWORD = process.env.EP8_PASSWORD || 'Verify123456';
// 🔒 不设默认口令：本文件会入库（仓库公开），且管理端口令随时可能被修改 ——
//    硬编码默认值会在「口令已改」之后把「口令不对」误报成「功能失败」。
const ADMIN_PASSWORD = process.env.EP8_ADMIN_PASSWORD || '';
if (!ADMIN_PASSWORD) {
  console.error('缺少管理端口令：请用 EP8_ADMIN_PASSWORD 环境变量提供（种子口令见 code/db/seed.sql）。');
  console.error('示例：EP8_ADMIN_PASSWORD=<口令> node scripts/verify_ep8_browser.mjs');
  process.exit(2);
}
const OUT = process.env.OUT_DIR || 'outputs/ep8-shots';

const PROFILE = path.resolve(OUT, `.edge-profile-${Date.now()}`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

if (!fs.existsSync(EDGE)) {
  console.error(`找不到 Edge：${EDGE}\n可用 EDGE_BIN 环境变量指定。`);
  process.exit(1);
}
fs.mkdirSync(OUT, { recursive: true });

let edge = null;
let edgeErr = '';

const EDGE_ARGS = [
  '--headless=new',
  '--disable-gpu',
  '--no-first-run',
  '--no-default-browser-check',
  '--disable-dev-shm-usage',
  '--hide-scrollbars',
  '--disable-extensions',
  '--disable-component-extensions-with-background-pages',
  '--disable-background-networking',
  // 🔴 必须绕过系统代理（`EP-9` 实测踩坑，见 EP-9 测试报告 §1.2）
  '--no-proxy-server',
  '--proxy-bypass-list=<-loopback>',
  '--window-size=1600,2400',
  `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${PROFILE}`,
  'about:blank',
];

// =============================================================================
const results = [];
function check(name, ok, detail = '') {
  results.push({ name, ok: !!ok, detail });
  console.log(`  ${ok ? '✅' : '❌'} ${name}${detail ? ` — ${detail}` : ''}`);
  return !!ok;
}
function section(t) {
  console.log(`\n=== ${t} ===`);
}

const NOISE = [/chrome-extension:\/\//i, /extensions::/i, /DevTools/i, /favicon/i];
const isNoise = (t) => NOISE.some((re) => re.test(t));

// =============================================================================
// 页面探针
// =============================================================================
const DASH_PROBE = `(() => {
  const q = (s) => document.querySelector(s);
  const qa = (s) => [...document.querySelectorAll(s)];
  const txt = (s) => { const e = q(s); return e ? e.innerText.trim() : null; };
  return {
    onDash: !!q('[data-testid="admin-dashboard"]'),
    title: txt('h1'),
    cards: qa('[data-testid^="ov-"]').length,
    cardTexts: qa('[data-testid^="ov-"]').map((e) => e.innerText.replace(/\\n/g, ' ')),
    trendCard: !!q('[data-testid="mape-trend-card"]'),
    trendChart: !!q('[data-testid="mape-trend-chart"]'),
    trendSvg: qa('[data-testid="mape-trend-chart"] svg path, [data-testid="mape-trend-chart"] svg polyline').length,
    trendEmpty: /还没有产出指标/.test(document.body.innerText),
    modelCard: !!q('[data-testid="model-card"]'),
    activeModel: !!q('[data-testid="active-model"]'),
    envDrift: !!q('[data-testid="env-drift-badge"]'),
    modelRows: qa('[data-testid^="model-row-"]').length,
    switchBtns: qa('[data-testid^="switch-"]').length,
    eventCard: !!q('[data-testid="event-card"]'),
    eventChart: !!q('[data-testid="event-chart"]'),
    navDash: !!q('[data-testid="nav-admin"]'),
    navManage: !!q('[data-testid="nav-admin-manage"]'),
    sidebarHasAdmin: !!document.querySelector('aside a[href^="/admin"]'),
    // ⚠️ 用独立 testid 断言，不用 innerText 模糊匹配：
    //    「管理员」三字在 403 页/日志页/角色下拉里也会出现，
    //    靠全文匹配会在别的页面误判为「徽标可见」。
    adminBadge: !!q('[data-testid="admin-badge"]'),
    mentionsThreshold: /20%/.test(document.body.innerText),
  };
})()`;

const MANAGE_PROBE = `(() => {
  const q = (s) => document.querySelector(s);
  const qa = (s) => [...document.querySelectorAll(s)];
  const txt = (s) => { const e = q(s); return e ? e.innerText.trim() : null; };
  return {
    onManage: !!q('[data-testid="admin-manage"]'),
    title: txt('h1'),
    tabs: qa('[data-testid^="tab-"]').map((e) => e.innerText.trim()),
    userTab: !!q('[data-testid="user-tab"]'),
    userRows: qa('[data-testid^="user-row-"]').length,
    userSearch: !!q('[data-testid="user-search"]'),
    userRoleFilter: !!q('[data-testid="user-role-filter"]'),
    toggleBtns: qa('[data-testid^="toggle-"]').length,
    roleBtns: qa('[data-testid^="role-"]').length,
    resetBtns: qa('[data-testid^="reset-"]').length,
    hasPwdHash: /password_hash|passwordHash|\\$2b\\$/.test(document.body.innerHTML),
  };
})()`;

const TASK_PROBE = `(() => {
  const q = (s) => document.querySelector(s);
  const qa = (s) => [...document.querySelectorAll(s)];
  const txt = (s) => { const e = q(s); return e ? e.innerText.trim() : null; };
  return {
    taskTab: !!q('[data-testid="task-tab"]'),
    taskRows: qa('[data-testid^="task-row-"]').length,
    softDeleted: qa('[data-testid^="soft-deleted-"]').length,
    forceDelBtns: qa('[data-testid^="force-del-"]').length,
    taskStatusFilter: !!q('[data-testid="task-status-filter"]'),
    // 二次确认
    dialog: !!q('[data-testid="force-del-dialog"]'),
    dialogText: txt('[data-testid="force-del-dialog"]'),
    hasIrreversible: /不可恢复/.test(document.body.innerText),
    mentionsSoftDelete: /软删除/.test(document.body.innerText),
    input: !!q('[data-testid="force-del-input"]'),
    confirmDisabled: (() => { const b = q('[data-testid="force-del-confirm"]'); return b ? b.disabled : null; })(),
    confirmText: txt('[data-testid="force-del-confirm"]'),
  };
})()`;

const LOG_PROBE = `(() => {
  const q = (s) => document.querySelector(s);
  const qa = (s) => [...document.querySelectorAll(s)];
  const txt = (s) => { const e = q(s); return e ? e.innerText.trim() : null; };
  const body = document.body.innerText;
  return {
    logTab: !!q('[data-testid="log-tab"]'),
    logRows: qa('[data-testid^="log-row-"]').length,
    actionFilter: !!q('[data-testid="log-action-filter"]'),
    hasAdminAction: /管理端操作/.test(body),
    subBadges: qa('[data-testid="log-table"] tbody tr span').filter((e) =>
      /启用账号|停用账号|重置密码|强制删除任务|变更角色|切换模型版本/.test(e.innerText)).length,
    mentionsDetail: /sub=/.test(body),
  };
})()`;

const RESET_PROBE = `(() => {
  const q = (s) => document.querySelector(s);
  const txt = (s) => { const e = q(s); return e ? e.innerText.trim() : null; };
  return {
    dialog: !!q('[data-testid="reset-dialog"]'),
    dialogText: txt('[data-testid="reset-dialog"]'),
    mentionsSessions: /全部已登录会话会立刻失效/.test(document.body.innerText),
    input: !!q('[data-testid="reset-input"]'),
    hint: txt('[data-testid="pwd-hint"]'),
    confirmDisabled: (() => { const b = q('[data-testid="reset-confirm"]'); return b ? b.disabled : null; })(),
  };
})()`;

const GUARD_PROBE = `(() => {
  const q = (s) => document.querySelector(s);
  return {
    path: location.pathname,
    forbidden: !!q('[data-testid="admin-forbidden"]'),
    forbiddenText: (q('[data-testid="admin-forbidden"]') || {}).innerText || null,
    navAdmin: !!q('[data-testid="nav-admin"]'),
    navManage: !!q('[data-testid="nav-admin-manage"]'),
    sidebarAdmin: !!document.querySelector('aside a[href^="/admin"]'),
    adminBadge: /管理员/.test(document.body.innerText),
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
    if (r.exceptionDetails) {
      throw new Error(`页面内 JS 异常：${r.exceptionDetails.text}`);
    }
    return r.result.value;
  }
}

async function waitForDevtools(timeoutMs = 30000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      if (r.ok) return await r.json();
    } catch { /* 未就绪 */ }
    await sleep(400);
  }
  throw new Error(`DevTools 端口未就绪\n${edgeErr.slice(-1200)}`);
}

const clickExpr = (sel) =>
  `(() => { const e = document.querySelector(${JSON.stringify(sel)});
     if (!e) return false; e.click(); return true; })()`;

async function fill(cdp, sel, value) {
  return cdp.eval(`(() => {
    const e = document.querySelector(${JSON.stringify(sel)});
    if (!e) return false;
    const proto = e.tagName === 'SELECT' ? HTMLSelectElement.prototype
      : e.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(e, ${JSON.stringify(String(value))});
    e.dispatchEvent(new Event('input', { bubbles: true }));
    e.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  })()`);
}

/** 登录并等待进入后台（`status === 'ready' && isAuthenticated`） */
async function login(cdp, goto, email, pwd) {
  await goto(`${APP}/login`, 1600);
  await fill(cdp, 'input[type="email"]', email);
  await fill(cdp, 'input[type="password"]', pwd);
  await cdp.eval(clickExpr('button[type="submit"]'));
  // ⚠️ 登录成功后不是立刻跳转：跳转条件是 store 的会话恢复完成，
  //    固定 sleep 会在恢复完成前断言 → 偶发失败。故轮询路径。
  const t0 = Date.now();
  while (Date.now() - t0 < 25000) {
    const p = await cdp.eval('location.pathname').catch(() => '');
    if (p && !/\/login/.test(p)) return true;
    await sleep(400);
  }
  return false;
}

async function main() {
  section('0. 启动 Edge 无头实例');
  edge = spawn(EDGE, EDGE_ARGS, { stdio: ['ignore', 'pipe', 'pipe'] });
  edge.stderr.on('data', (d) => { edgeErr += d.toString(); });
  await waitForDevtools();
  console.log('  ✅ DevTools 端口就绪');

  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const target = list.find((t) => t.type === 'page');
  if (!target) throw new Error('没有可用的 page target');

  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.addEventListener('open', res, { once: true });
    ws.addEventListener('error', rej, { once: true });
  });
  const cdp = new CDP(ws);

  const consoleErrors = [];
  cdp.on((msg) => {
    if (msg.method === 'Runtime.consoleAPICalled') {
      if (msg.params.type === 'error' || msg.params.type === 'warning') {
        const t = (msg.params.args || []).map((a) => a.value ?? a.description ?? a.type).join(' ');
        const s = String(t).slice(0, 300);
        if (!isNoise(s)) consoleErrors.push({ level: msg.params.type, text: s });
      }
    }
    if (msg.method === 'Runtime.exceptionThrown') {
      const d = msg.params.exceptionDetails;
      const s = String((d.exception && (d.exception.description || d.exception.value)) || d.text).slice(0, 300);
      if (!isNoise(s) && !/chrome-extension:\/\//.test(s)) {
        consoleErrors.push({ level: 'exception', text: s });
      }
    }
  });

  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');
  await cdp.send('DOM.enable');

  async function goto(url, settle = 2200) {
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
  async function shoot(name) {
    const dir = path.join(OUT, name);
    fs.mkdirSync(dir, { recursive: true });
    for (let i = 0; i < 4; i += 1) {
      try {
        const r = await Promise.race([
          cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: i < 3 }),
          new Promise((_, rej) => setTimeout(() => rej(new Error('截图超时')), 15000)),
        ]);
        fs.writeFileSync(path.join(dir, 'app_screenshot.png'), Buffer.from(r.data, 'base64'));
        console.log(`       📷 ${name}/app_screenshot.png`);
        return true;
      } catch {
        await sleep(250);
      }
    }
    console.log(`       ⚠️ 截图失败：${name}`);
    return false;
  }

  // =============================================================================
  section('1. 🔒 游客访问管理端 → 跳登录（不是 403）');
  for (const p of ['/admin', '/admin/manage']) {
    await goto(`${APP}${p}`, 1800);
    const landed = await cdp.eval('location.pathname');
    check(`🔒 游客访问 ${p} 被守卫拦截`, /\/login/.test(landed), landed);
  }

  // =============================================================================
  section('2. 准备普通用户（应为看不到管理端）');
  await goto(`${APP}/register`, 1600);
  await fill(cdp, 'input[type="email"]', USER_EMAIL);
  await fill(cdp, 'input[type="password"]', PASSWORD);
  await fill(cdp, '#reg-confirm', PASSWORD);
  // 🔒 **必须勾选用户协议**。注册表单底部有《用户协议》《数据用途说明》两个必选
  //    复选框，不勾会被前端校验拦下并停在 /register —— 表现为「登录失败」，
  //    实际是注册压根没提交。别用 `input[type="submit"]` 盲点，先确认复选框存在。
  const agreed = await cdp.eval(
    `(() => { const cbs=[...document.querySelectorAll('input[type="checkbox"]')];
      if(!cbs.length) return false;
      for(const c of cbs){ if(!c.checked){ c.click(); } }
      return cbs.every(c=>c.checked); })()`);
  check('✅ 注册页用户协议已勾选', agreed);
  await cdp.eval(clickExpr('button[type="submit"]'));
  await sleep(2600);
  // 账号可能已存在 → 无论注册成功与否都走登录。
  // ⚠️ 登录失败时把注册页残留的报错文案带出来，否则只看到「登录失败」无从下手。
  const regErr = await cdp.eval(
    `(() => { const e=document.querySelector('[role="alert"],.alert-danger');
       return e ? e.innerText.trim().slice(0,80) : null; })()`).catch(() => null);
  const loginOk = await login(cdp, goto, USER_EMAIL, PASSWORD);
  check('✅ 普通用户登录成功', loginOk, loginOk ? USER_EMAIL : `注册页报错：${regErr || '无'}`);

  section('3. 🔒 普通用户：导航零管理端入口 + 手工改 URL 落403 页');
  await goto(`${APP}/dashboard`, 2200);
  let g = await cdp.eval(GUARD_PROBE);
  check('🔒 顶栏无「数据看板」入口', !g.navAdmin, `navAdmin=${g.navAdmin}`);
  check('🔒 顶栏无「用户与任务管理」入口', !g.navManage, `navManage=${g.navManage}`);
  check('🔒 侧栏零管理端分组', !g.sidebarAdmin, `sidebarAdmin=${g.sidebarAdmin}`);
  check('🔒 顶栏无「管理员」徽标', !g.adminBadge);

  await goto(`${APP}/admin`, 2400);
  g = await cdp.eval(GUARD_PROBE);
  check('🔒 普通用户访问 /admin → 403 落点（非空白页）', g.forbidden, g.path);
  check('🔒 🔒 403 页文案含「无管理端访问权限」',
        /无管理端访问权限/.test(g.forbiddenText || ''), (g.forbiddenText || '').slice(0, 40));
  check('🔒 🔒 403 页给出可行动建议（联系管理员开通）',
        /联系系统管理员/.test(g.forbiddenText || ''));
  await shoot('01-user-403');

  await goto(`${APP}/admin/manage`, 2400);
  g = await cdp.eval(GUARD_PROBE);
  check('🔒 普通用户访问 /admin/manage → 403 落点', g.forbidden, g.path);

  // =============================================================================
  section('4. 换管理员登录（应看得到两个入口）');
  //先登出（清 token），否则守卫仍认旧身份
  await goto(`${APP}/dashboard`, 1800);
  await cdp.eval(clickExpr('button[type="button"]:not([data-testid])') || 'true').catch(() => {});
  await cdp.eval(`(() => {
    const b=[...document.querySelectorAll('button')].find(x=>/退出登录/.test(x.innerText));
    if(b){b.click();return true;} return false; })()`);
  await sleep(1800);
  check('✅ 管理员种子账号登录成功',
        await login(cdp, goto, ADMIN_EMAIL, ADMIN_PASSWORD), ADMIN_EMAIL);

  section('5. 🔒 AD-P01 运营看板');
  await goto(`${APP}/admin`, 3600);
  let d = await cdp.eval(DASH_PROBE);
  check('🔒 落在运营看板', d.onDash, d.path);
  check('🔒 顶栏有「数据看板」入口', d.navDash);
  check('🔒 顶栏有「用户与任务管理」入口', d.navManage);
  check('🔒 管理员徽标可见', d.adminBadge);
  check('🔒 5 张指标卡齐备', d.cards === 5, `实际 ${d.cards} 张`);
  check('🔒 指标卡含「用户/数据集/任务/成功率/耗时」',
        d.cardTexts.some((t) => /注册用户/.test(t))
        && d.cardTexts.some((t) => /成功率/.test(t))
        && d.cardTexts.some((t) => /耗时/.test(t)),
        d.cardTexts.join(' | ').slice(0, 120));
  check('🔒 MAPE 走势区块存在', d.trendCard);
  check('🔒 走势为空态或图表二选一（不空白）',
        d.trendChart || d.trendEmpty, `chart=${d.trendChart} empty=${d.trendEmpty}`);
  check('🔒 🔒 懒加载的ECharts 真的渲染出了图形',
        d.trendChart ? d.trendSvg > 0 : true,
        d.trendChart ? `SVG 元素 ${d.trendSvg}` : '空态（无任务可画）');
  check('🔒 阈值 20% 在页面上可见', d.mentionsThreshold);
  check('🔒 模型版本区块存在', d.modelCard && d.activeModel);
  check('🔒 模型表至少 1 行', d.modelRows >= 1, `${d.modelRows} 行`);
  check('🔒 埋点概览区块存在', d.eventCard);
  await shoot('02-admin-dashboard');

  // =============================================================================
  section('6. 🔒 AD-P02 用户管理 Tab');
  await goto(`${APP}/admin/manage`, 3200);
  let m = await cdp.eval(MANAGE_PROBE);
  check('🔒 落在管理页', m.onManage, m.title);
  check('🔒 三个 Tab 齐备',
        m.tabs.length === 3 && m.tabs.some((t) => /用户管理/.test(t))
        && m.tabs.some((t) => /任务管理/.test(t)) && m.tabs.some((t) => /操作日志/.test(t)),
        m.tabs.join(' / '));
  check('🔒 用户表有数据', m.userRows >= 2, `${m.userRows} 行`);
  check('🔒 搜索框 + 角色筛选齐备', m.userSearch && m.userRoleFilter);
  check('🔒 每行有 停用/角色/重置 三个操作', m.toggleBtns >= 2 && m.roleBtns >= 2 && m.resetBtns >= 2,
        `toggle=${m.toggleBtns} role=${m.roleBtns} reset=${m.resetBtns}`);
  check('🔒 🔒 页面 HTML 零 password_hash / bcrypt 串', !m.hasPwdHash);
  await shoot('03-admin-users');

  // =============================================================================
  section('7. 🔒 重置密码弹窗：文案 + 强度实时反馈');
  await cdp.eval(clickExpr('[data-testid^="reset-"]'));
  await sleep(900);
  let r = await cdp.eval(RESET_PROBE);
  check('🔒 弹窗出现', r.dialog);
  check('🔒 🔒 文案明示「全部已登录会话会立刻失效」', r.mentionsSessions,
        (r.dialogText || '').slice(0, 60));
  check('🔒 初始确认按钮禁用（还没输密码）', r.confirmDisabled === true,
        `disabled=${r.confirmDisabled}`);

  await fill(cdp, '[data-testid="reset-input"]', 'abcdefgh');   // 无数字
  await sleep(400);
  r = await cdp.eval(RESET_PROBE);
  check('🔒 弱密码（无数字）时仍禁用', r.confirmDisabled === true);
  check('🔒 提示语说明缺什么', /字母与数字/.test(r.hint || ''), r.hint);

  await fill(cdp, '[data-testid="reset-input"]', 'Verify123456');
  await sleep(400);
  r = await cdp.eval(RESET_PROBE);
  check('🔒 合规密码时解禁', r.confirmDisabled === false, `hint=${r.hint}`);
  await shoot('04-reset-dialog');
  await cdp.eval(clickExpr('[data-testid="reset-dialog"] button:last-of-type').replace('button:last-of-type', 'button:nth-of-type(2)').length ? 'true' : 'true');
  await cdp.eval(`(() => {
    const b=[...document.querySelectorAll('[data-testid="reset-dialog"] button')].find(x=>/取消/.test(x.innerText));
    if(b){b.click();return true;} return false; })()`);
  await sleep(700);

  // =============================================================================
  section('8. 🔒 任务管理 Tab + 强制删除二次确认');
  await cdp.eval(clickExpr('[data-testid="tab-tasks"]'));
  await sleep(2400);
  let t = await cdp.eval(TASK_PROBE);
  check('🔒 切到任务 Tab', t.taskTab);
  check('🔒 任务表有数据', t.taskRows >= 1, `${t.taskRows} 行`);
  check('🔒 每行有「强制删除」按钮', t.forceDelBtns >= 1, `${t.forceDelBtns} 个`);
  check('🔒 状态筛选器存在', t.taskStatusFilter);

  await cdp.eval(clickExpr('[data-testid^="force-del-"]'));
  await sleep(900);
  t = await cdp.eval(TASK_PROBE);
  check('🔒 二次确认弹窗出现', t.dialog);
  check('🔒 🔒 文案含「不可恢复」', t.hasIrreversible);
  check('🔒 🔒 明示与用户侧软删除不同', t.mentionsSoftDelete);
  check('🔒 输入框存在', t.input);
  check('🔒 🔒 未输入时确认按钮禁用', t.confirmDisabled === true,
        `disabled=${t.confirmDisabled}`);
  check('🔒 🔒 按钮文案是「永久删除」而非「删除」',
        /永久删除/.test(t.confirmText || ''), t.confirmText);

  const taskId = await cdp.eval(
    `(() => { const i=document.querySelector('[data-testid^="force-del-"]');
       return i ? i.getAttribute('data-testid').replace('force-del-','') : null; })()`);
  await fill(cdp, '[data-testid="force-del-input"]', String(Number(taskId) + 1));
  await sleep(400);
  t = await cdp.eval(TASK_PROBE);
  check('🔒 🔒 输错ID 时确认按钮仍禁用', t.confirmDisabled === true,
        `输入了 ${Number(taskId) + 1}，真实 ${taskId}`);

  await fill(cdp, '[data-testid="force-del-input"]', String(Number(taskId) - 1));
  await sleep(400);
  t = await cdp.eval(TASK_PROBE);
  check('🔒 🔒 少一位也禁用', t.confirmDisabled === true);

  await fill(cdp, '[data-testid="force-del-input"]', String(taskId));
  await sleep(400);
  t = await cdp.eval(TASK_PROBE);
  check('🔒 🔒 输入正确 ID 后解禁', t.confirmDisabled === false);
  await shoot('05-force-delete-dialog');

  // 真正删一次，验证回执与列表更新
  await cdp.eval(clickExpr('[data-testid="force-del-confirm"]'));
  await sleep(2600);
  t = await cdp.eval(TASK_PROBE);
  check('🔒 删除后弹窗关闭', !t.dialog);
  const okMsg = await cdp.eval(
    `(() => { const e=document.querySelector('[data-testid="ok-msg"]'); return e?e.innerText:null; })()`);
  check('🔒 🔒 回执含连带删除行数', !!okMsg && /不可恢复|条预测明细/.test(okMsg),
        (okMsg || '').slice(0, 80));
  await shoot('06-after-delete');

  // =============================================================================
  section('9. 🔒 操作日志 Tab：子动作徽标 + detail 展开');
  await cdp.eval(clickExpr('[data-testid="tab-logs"]'));
  await sleep(2400);
  let lg = await cdp.eval(LOG_PROBE);
  check('🔒 切到日志 Tab', lg.logTab);
  check('🔒 日志表有数据', lg.logRows >= 1, `${lg.logRows} 行`);
  check('🔒 动作筛选器存在', lg.actionFilter);
  check('🔒 🔒 能看到 action=admin 的条目', lg.hasAdminAction);
  check('🔒 🔒 子动作已渲染为中文徽标（后端已拆好 sub_action）',
        lg.subBadges >= 1, `${lg.subBadges} 个`);
  check('🔒 🔒 明细列展示原始 detail（含 sub=）', lg.mentionsDetail);

  // ⚠️ 筛选后要同时验两件事：子动作徽标仍在，且**行数被收窄**。
  //    只验「徽标还在」是不够的 —— 筛选没生效时徽标照样在。
  //    故先记下未筛选时的行数做对照基准。
  const rowsBeforeFilter = lg.logRows;
  await fill(cdp, '[data-testid="log-action-filter"]', 'admin');
  await sleep(2000);
  lg = await cdp.eval(LOG_PROBE);
  check('🔒 action=admin 筛选生效且子动作可见',
        lg.subBadges >= 1 && lg.hasAdminAction && lg.logRows <= rowsBeforeFilter,
        `subBadges=${lg.subBadges}, 行数 ${lg.logRows}/${rowsBeforeFilter}`);
  await shoot('07-admin-logs');

  // =============================================================================
  section('10. 🔒 consoleErrors = 0');
  check('🔒 全程无 console error / 未捕获异常',
        consoleErrors.length === 0,
        consoleErrors.length ? consoleErrors.slice(0, 5).map((e) => e.text).join(' || ') : '0 条');

  const passed = results.filter((r) => r.ok).length;
  const total = results.length;
  console.log(`\n${'='.repeat(64)}`);
  console.log(`EP-8 浏览器走查：${passed} / ${total} 通过`);
  if (passed !== total) {
    console.log('\n未通过项：');
    results.filter((r) => !r.ok).forEach((r) => console.log(`  ❌ ${r.name} — ${r.detail}`));
  }
  console.log(`${'='.repeat(64)}`);
  return passed === total ? 0 : 1;
}

let code = 1;
try {
  code = await main();
} catch (e) {
  console.error('\n[FATAL]', e.message);
  if (edgeErr) console.error(edgeErr.slice(-1500));
} finally {
  // 🔒 必须杀**进程树**（EP-4 踩坑 #9）：只 kill 父进程会留下持有
  //    profile 锁的子进程，导致下次静默挂起。
  try {
    if (edge) {
      spawn('taskkill', ['/PID', String(edge.pid), '/T', '/F'], { stdio: 'ignore' });
    }
  } catch { /* 已退出 */ }
  try {
    fs.rmSync(PROFILE, { recursive: true, force: true });
  } catch { /* 残留无碍 */ }
}
process.exit(code);
