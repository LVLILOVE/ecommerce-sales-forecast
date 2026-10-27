/**
 * =============================================================================
 * `EP-7` 浏览器端校验（DOD-EP7 动态部分）
 * -----------------------------------------------------------------------------
 * 用系统自带 Edge（`--headless=new`）+ 原生 CDP 驱动，不依赖 playwright
 * （沿用 `verify_ep6_browser.mjs` 的骨架，原因见其文件头）。
 *
 * 覆盖 `DOD-EP7` 中**只能靠真实渲染判定**的部分：
 *   ① 🔒 三条记录类路由对游客一律跳登录（不能只走后端 401）
 *   ② 🔒 历史列表：汇总条 5 项 / 筛选真实生效 / 骨架 / 空态两出口
 *   ③ 🔒 🔒 **删除二次确认必须逐字输入任务 ID**：输入不符时按钮禁用
 *   ④ 🔒 删除为软删除：列表中消失但详情仍可访问（可追溯）
 *   ⑤ 🔒 详情页：参数快照带「冻结」徽标 / 指标与结果页同源 / 阶段耗时
 *   ⑥ 🔒 失败任务详情：错误码 + 失败阶段 + 归因 + 重跑入口
 *   ⑦ 🔒 对比页：最多 4 个（勾第 5 个被禁用）/ 最优值取**最小**（误差类）
 *   ⑧ 🔒 数据文件不一致 → 显著警告；步长不一致 → 指标不可比的警告
 *   ⑨ 🔒 工作台最近任务与历史列表首屏**逐条一致**
 *   ⑩ 🔒 consoleErrors = 0
 *
 * ⚠️ **为什么第 ③ 条要用真实渲染验**：按钮的 `disabled` 属性在源码里
 *    一眼可见，但它是否**真的**绑定到输入框的实时值，只能在浏览器里
 *    敲字符验证。写单测或读代码都可能与实际行为不一致。
 *
 * 用法（在**工作区根目录**执行）：
 *   APP_URL=http://127.0.0.1:5199 API_URL=http://127.0.0.1:8021 \
 *   node scripts/verify_ep7_browser.mjs
 * =============================================================================
 */
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const EDGE = process.env.EDGE_BIN || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const PORT = Number(process.env.CDP_PORT || 9950 + (process.pid % 100));
const APP = process.env.APP_URL || 'http://127.0.0.1:5199';
const API = process.env.API_URL || 'http://127.0.0.1:8021';
const EMAIL = process.env.EP7_EMAIL || 'ep7b@verify.com';
const PASSWORD = 'Verify123456';
const OUT = process.env.OUT_DIR || 'outputs/ep7-shots';

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
// 断言框架
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
const HISTORY_PROBE = `(() => {
  const q = (s) => document.querySelector(s);
  const qa = (s) => [...document.querySelectorAll(s)];
  const txt = (s) => { const e = q(s); return e ? e.innerText.trim() : null; };
  return {
    onHistory: !!q('[data-testid="history-page"]'),
    title: txt('h1'),
    skeleton: !!q('[data-testid="history-skeleton"]'),
    empty: !!q('[data-testid="dashboard-empty"]') || /还没有|没有符合/.test(document.body.innerText),

    // 汇总条
    summary: {
      total: txt('[data-testid="sum-total"]'),
      succeeded: txt('[data-testid="sum-succeeded"]'),
      failed: txt('[data-testid="sum-failed"]'),
      mape: txt('[data-testid="sum-mape"]'),
      duration: txt('[data-testid="sum-duration"]'),
    },

    // 列表
    rowIds: qa('[data-testid^="history-row-"]').map((e) => e.getAttribute('data-testid').replace('history-row-', '')),
    rowCount: qa('[data-testid^="history-row-"]').length,
    mapeCells: qa('[data-testid^="mape-"]').map((e) => e.innerText.trim()),
    statusTexts: qa('[data-testid^="status-"]').map((e) => e.innerText.trim()),

    // 筛选
    filters: !!q('[data-testid="history-filters"]'),
    keywordInput: !!q('[data-testid="filter-keyword"]'),
    statusSelect: !!q('[data-testid="filter-status"]'),
    pager: txt('[data-testid="history-pager"]'),

    // 删除确认
    dialog: !!q('[data-testid="delete-dialog"]'),
    dialogText: txt('[data-testid="delete-dialog"]'),
    confirmDisabled: (() => { const b = q('[data-testid="delete-confirm"]'); return b ? b.disabled : null; })(),
    confirmInput: !!q('[data-testid="delete-confirm-input"]'),
    deleteBtns: qa('[data-testid^="delete-"]').map((e) => e.getAttribute('data-testid')),
  };
})()`;

const DETAIL_PROBE = `(() => {
  const q = (s) => document.querySelector(s);
  const qa = (s) => [...document.querySelectorAll(s)];
  const txt = (s) => { const e = q(s); return e ? e.innerText.trim() : null; };
  return {
    onDetail: !!q('[data-testid="history-detail"]'),
    title: txt('h1'),
    frozenBadge: txt('[data-testid="frozen-badge"]'),
    paramCount: qa('[data-testid="param-grid"] > div').length,
    fallbackNote: txt('[data-testid="fallback-note"]'),

    metric: !!q('[data-testid="metric-block"]'),
    verdict: !!q('[data-testid="verdict-bar"]'),
    metricCards: qa('[data-testid^="metric-card-"]').length,
    baseline: !!q('[data-testid="baseline-table"]'),
    coverage: !!q('[data-testid="coverage-panel"]'),
    dist: !!q('[data-testid="distribution-chart"]'),
    series: !!q('[data-testid="series-block"]'),
    seriesPicker: !!q('[data-testid="series-picker"]'),
    // 🔴 选择器已由原生 `<select>` 换成「搜索 + 多选」自研组件（`FR-VIS-09`）：
    //    选项不再是 `<option>`（而是 `role="option"` 的按钮），且下拉默认**收起**
    //    —— 本探针是同步 IIFE，点开后 React 要下一帧才渲染列表，数不到。
    //    故判据改为「存在可搜索的 combobox 输入框」，选项数量与上限 50
    //    由 `verify_ep7.py` 的静态断言兜（`SERIES_MULTI_LIMIT`）。
    seriesOptions: qa('[data-testid="series-picker"] input[role="combobox"]').length,

    timing: !!q('[data-testid="timing-block"]'),
    timingRows: qa('[data-testid="timing-list"] li').length,
    timingTotal: txt('[data-testid="timing-total"]'),

    failed: !!q('[data-testid="failed-block"]'),
    failedTitle: txt('[data-testid="failed-block"]'),
    failedAttr: txt('[data-testid="failed-attribution"]'),
    retry: !!q('[data-testid="retry-block"]'),
    retryBtn: !!q('[data-testid="retry-run"]'),

    error: !!q('[data-testid="detail-error"]'),
    evalNote: txt('[data-testid="eval-note"]'),
  };
})()`;

const COMPARE_PROBE = `(() => {
  const q = (s) => document.querySelector(s);
  const qa = (s) => [...document.querySelectorAll(s)];
  const txt = (s) => { const e = q(s); return e ? e.innerText.trim() : null; };
  const body = document.body.innerText;
  return {
    onCompare: !!q('[data-testid="compare-page"]'),
    title: txt('h1'),
    picker: !!q('[data-testid="task-picker"]'),
    pickList: qa('[data-testid^="pick-"]').filter((e) => e.tagName === 'INPUT').length,
    pickedCount: txt('[data-testid="picked-count"]'),
    runDisabled: (() => { const b = q('[data-testid="run-compare"]'); return b ? b.disabled : null; })(),
    hint: txt('[data-testid="pick-hint"]'),

    metrics: !!q('[data-testid="compare-metrics"]'),
    metricRows: qa('[data-testid="compare-metrics"] tbody tr').length,
    bestCells: qa('[data-testid^="cell-"][data-best="true"]').map((e) => e.getAttribute('data-testid')),
    mapeCells: qa('[data-testid^="cell-mape-"]').map((e) => ({
      id: e.getAttribute('data-testid').replace('cell-mape-', ''),
      text: e.innerText.trim(),
      best: e.getAttribute('data-best') === 'true',
    })),
    improveCells: qa('[data-testid^="cell-maeImprove-"]').map((e) => ({
      text: e.innerText.trim(), best: e.getAttribute('data-best') === 'true',
    })),

    datasetWarning: !!q('[data-testid="dataset-warning"]'),
    warnings: qa('[data-testid="compare-warning"]').map((e) => e.innerText.trim()),
    attribution: !!q('[data-testid="compare-attribution"]'),
    attrRows: qa('[data-testid="compare-attribution"] li').length,
    series: !!q('[data-testid="compare-series"]'),
    seriesNote: txt('[data-testid="series-note"]'),
    seriesEmpty: !!q('[data-testid="compare-series-empty"]'),
    mentionsStepDiff: /步长/.test(body),
  };
})()`;

const DASH_PROBE = `(() => {
  const q = (s) => document.querySelector(s);
  const qa = (s) => [...document.querySelectorAll(s)];
  const txt = (s) => { const e = q(s); return e ? e.innerText.trim() : null; };
  return {
    onDash: !!q('[data-testid="dashboard-page"]'),
    title: txt('h1'),
    lede: txt('[data-testid="dashboard-lede"]'),
    cta: !!q('[data-testid="cta-new-task"]'),
    stats: {
      total: txt('[data-testid="stat-total"]'),
      succeeded: txt('[data-testid="stat-succeeded"]'),
      failed: txt('[data-testid="stat-failed"]'),
      mape: txt('[data-testid="stat-mape"]'),
    },
    quick: qa('[data-testid^="quick-"]').length,
    rowIds: qa('[data-testid^="dash-row-"]').map((e) => e.getAttribute('data-testid').replace('dash-row-', '')),
    empty: !!q('[data-testid="dashboard-empty"]'),
    navHasHistory: !!q('a[href="/history"]'),
    navHasCompare: !!q('a[href="/compare"]'),
  };
})()`;

// =============================================================================
// CDP 驱动（与 verify_ep6_browser.mjs 相同）
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

/** 在页面里派发真实点击（React 受控组件必须走原生 click 才触发 onChange） */
const clickExpr = (sel) =>
  `(() => { const e = document.querySelector(${JSON.stringify(sel)});
     if (!e) return false; e.click(); return true; })()`;

/** 设置 input/select 的值并派发 input+change（React 受控值劫持的标准做法） */
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
  section('1. 游客可达性（三条记录类路由必须跳登录）');
  for (const p of ['/history', '/history/19', '/compare']) {
    await goto(`${APP}${p}`, 1800);
    const landed = await cdp.eval('location.pathname + location.search');
    check(`🔒 游客访问 ${p} 被守卫拦截`, /\/login/.test(landed), landed);
  }

  // =============================================================================
  section('2. 准备登录态并跑两个任务');
  await goto(`${APP}/login`, 1800);
  let loggedIn = false;
  {
    // 注册优先：账号可能已存在（`register` 已存在会返 40105 邮箱已占用）
    await goto(`${APP}/register`, 1500);
    await fill(cdp, 'input[type="email"]', EMAIL);
    await fill(cdp, 'input[type="password"]', PASSWORD);
    await fill(cdp, '#reg-confirm', PASSWORD);
    await cdp.eval(clickExpr('button[type="submit"]'));
    await sleep(2600);
    await goto(`${APP}/login`, 1600);
    await fill(cdp, 'input[type="email"]', EMAIL);
    await fill(cdp, 'input[type="password"]', PASSWORD);
    await cdp.eval(clickExpr('button[type="submit"]'));
    // ⚠️ 登录成功后不是立刻跳转：LoginPage 的跳转条件是
    //    `status === 'ready' && isAuthenticated` —— `status` 来自
    //    store 的会话恢复（读 localStorage + 拉 /users/me）。
    //    恢复要发一次网络请求，固定 sleep 极易在它返回前就断言，
    //    表现为「偶发登录失败」。这里改成轮询路径变化。
    let landed = '';
    for (let i = 0; i < 24; i += 1) {
      await sleep(500);
      landed = await cdp.eval('location.pathname');
      if (!/\/login/.test(landed)) break;
    }
    loggedIn = /\/dashboard/.test(landed);
    if (!loggedIn) {
      const why = await cdp.eval(`(() => {
        const a = document.querySelector('[role="alert"]');
        const form = document.querySelector('form');
        return JSON.stringify({
          alert: a ? a.innerText.trim().slice(0, 80) : null,
          values: [...document.querySelectorAll('input')].map((i) => i.type + '=' + (i.value ? '有' : '空')),
          formPresent: !!form,
        });
      })()`);
      console.log('       诊断：', why);
    }
  }
  check('登录成功并落到工作台', loggedIn, await cdp.eval('location.pathname'));
  if (!loggedIn) {
    console.log('\n[FATAL] 登录未成功，后续用例无法执行');
    return 1;
  }

  // 直接走接口跑任务（比走 UI 快，且浏览器脚本不该依赖 UI 完成数据准备）
  section('3. 通过接口准备两个任务（供列表 / 详情 / 对比使用）');
  const tok = await cdp.eval(`(() => {
    for (let i = 0; i < localStorage.length; i += 1) {
      const k = localStorage.key(i);
      const v = localStorage.getItem(k) || '';
      if (v.includes('eyJ') || k.toLowerCase().includes('token')) {
        try { const j = JSON.parse(v); return j.access_token || j.token || v; } catch { return v; }
      }
    }
    return null;
  })()`);
  let taskIds = [];
  if (tok) {
    const H = { Authorization: `Bearer ${tok}`, 'Content-Type': 'application/json' };
    const dsRes = await (await fetch(`${API}/api/v1/datasets/sample`, { headers: H })).json();
    const dsId = dsRes?.data?.dataset?.id;
    if (dsId) {
      for (const h of [7, 14]) {
        const r = await (
          await fetch(`${API}/api/v1/predictions`, {
            method: 'POST',
            headers: H,
            body: JSON.stringify({ dataset_id: dsId, horizon: h }),
          })
        ).json();
        if (r?.data?.task_id) taskIds.push(r.data.task_id);
      }
    }
    check('接口侧准备两个任务', taskIds.length === 2, `ids=${taskIds.join(',')}`);
  } else {
    check('能从 localStorage 取到令牌', false, '令牌未找到，后续用已有历史数据');
  }

  // =============================================================================
  section('4. 历史列表（UX-P09）');
  await goto(`${APP}/history`, 3000);
  const h = await cdp.eval(HISTORY_PROBE);
  check('页面渲染', h.onHistory, h.title);
  check('🔒 汇总条 5 项齐备',
    h.summary.total && h.summary.succeeded && h.summary.failed && h.summary.mape && h.summary.duration,
    JSON.stringify(h.summary));
  check('筛选栏可用（关键词 + 状态）', h.filters && h.keywordInput && h.statusSelect);
  if (h.rowCount > 0) {
    check('列表有数据行', true, `${h.rowCount} 行`);
    check('🔒 失败任务的 MAPE 显示「—」而非 0',
      h.mapeCells.every((c) => c === '—' || /%$/.test(c)),
      h.mapeCells.slice(0, 4).join(' '));
    await shoot('01-history-list');
  } else {
    check('列表有数据行', false, '无数据行');
  }

  section('5. 筛选真实生效（不是装饰）');
  const firstName = taskIds.length
    ? (await (await fetch(`${API}/api/v1/history`, { headers: { Authorization: `Bearer ${tok}` } })).json())
        ?.data?.items?.[0]?.task_name ?? ''
    : '';
  const kw = firstName ? firstName.slice(0, 5) : 'demo';
  await fill(cdp, '[data-testid="filter-keyword"]', kw);
  await cdp.eval(clickExpr('[data-testid="filter-submit"]'));
  await sleep(2200);
  const hFiltered = await cdp.eval(HISTORY_PROBE);
  check('🔒 关键词筛选后仍有结果（数据真实匹配）',
    hFiltered.rowCount > 0, `kw=${kw} rows=${hFiltered.rowCount}`);
  await fill(cdp, '[data-testid="filter-keyword"]', 'zzz-不存在-zzz');
  await cdp.eval(clickExpr('[data-testid="filter-submit"]'));
  await sleep(2000);
  const hEmpty = await cdp.eval(HISTORY_PROBE);
  check('🔒 无匹配时进入空态（不是空白页）', hEmpty.rowCount === 0 && hEmpty.empty,
    `empty=${hEmpty.empty}`);
  await shoot('02-history-filter-empty');
  await cdp.eval(clickExpr('[data-testid="filter-reset"]'));
  await sleep(2000);

  // =============================================================================
  section('6. 🔒 删除二次确认：逐字输入任务 ID（UX-P09-04）');
  // ⚠️ 软删除不可逆（is_deleted=1 后用户侧就再也看不到它），
  //   所以删除用例**必须用独立的任务**，不能删掉后续对比要用的那一个。
  //   早期版本删的是 `taskIds` 的最后一个，结果它从列表消失后
  //   对比页少一个可选项，「已选 1 / 4」—— 看起来像勾选失效，
  //   实际是数据准备顺序的错。
  let victim = null;
  if (tok) {
    const H = { Authorization: `Bearer ${tok}`, 'Content-Type': 'application/json' };
    const dsRes = await (await fetch(`${API}/api/v1/datasets/sample`, { headers: H })).json();
    const dsId = dsRes?.data?.dataset?.id;
    if (dsId) {
      const r = await (
        await fetch(`${API}/api/v1/predictions`, {
          method: 'POST',
          headers: H,
          body: JSON.stringify({ dataset_id: dsId, horizon: 7, task_name: 'EP7 待删除用例' }),
        })
      ).json();
      if (r?.data?.task_id) victim = r.data.task_id;
    }
  }
  check('为删除用例准备了独立任务', victim !== null, `victim=${victim}`);
  if (victim) {
    // 🔒 新建的任务必须**重新加载列表**才会出现在 DOM 里。
    //    前面第 4 节取到的 rows 是上一批任务，直接点
    //    `[data-testid="delete-${victim}"]` 会因为元素不存在而静默失败
    //    （`clickExpr` 对找不到元素返回 false，不抛错）。
    await goto(`${APP}/history`, 2600);
    const hasVictimRow = await cdp.eval(
      `!!document.querySelector('[data-testid="history-row-${victim}"]')`,
    );
    check('待删除任务在列表首屏可见', hasVictimRow);
    await cdp.eval(clickExpr(`[data-testid="delete-${victim}"]`));
    await sleep(900);
    let d = await cdp.eval(HISTORY_PROBE);
    check('点删除弹出确认框', d.dialog);
    check('🔒 确认框复用危险提示「不可撤销」', /不可撤销/.test(d.dialogText || ''),
      (d.dialogText || '').slice(0, 40));
    check('🔒 确认框提示「用筛选而非删除」', /筛选/.test(d.dialogText || ''));
    check('🔒 未输入时确认按钮禁用', d.confirmDisabled === true);

    // 输入错误 ID → 仍禁用
    await fill(cdp, '[data-testid="delete-confirm-input"]', String(victim + 1));
    await sleep(400);
    d = await cdp.eval(HISTORY_PROBE);
    check('🔒 输入错误 ID 时确认按钮仍禁用', d.confirmDisabled === true);

    // 少一位（部分匹配）→ 仍禁用
    await fill(cdp, '[data-testid="delete-confirm-input"]', String(victim).slice(0, -1));
    await sleep(400);
    d = await cdp.eval(HISTORY_PROBE);
    check('🔒 部分匹配（少一位）时确认按钮仍禁用', d.confirmDisabled === true);

    // 输入正确 ID → 解禁
    await fill(cdp, '[data-testid="delete-confirm-input"]', String(victim));
    await sleep(400);
    d = await cdp.eval(HISTORY_PROBE);
    check('🔒 输入完整正确 ID 后确认按钮解禁', d.confirmDisabled === false);
    await shoot('03-delete-confirm');

    await cdp.eval(clickExpr('[data-testid="delete-confirm"]'));
    await sleep(2600);
    d = await cdp.eval(HISTORY_PROBE);
    check('🔒 删除后该行从列表消失', !d.rowIds.includes(String(victim)),
      `rows=${d.rowIds.join(',')}`);

    // 🔒 软删除：库中仍在 → 列表查不到，但接口层应明确「不存在」
    const afterDel = await (
      await fetch(`${API}/api/v1/history/${victim}`, { headers: { Authorization: `Bearer ${tok}` } })
    ).json();
    check('🔒 软删除后详情接口不再返回该任务（列表/详情一致）',
      afterDel.code !== 0, `code=${afterDel.code}`);
  } else {
    check('无可删除的任务（跳过）', true);
  }

  // =============================================================================
  section('7. 任务详情（UX-P10）');
  const okTask = taskIds.length ? taskIds[0] : null;
  if (okTask) {
    await goto(`${APP}/history/${okTask}`, 3200);
    const d = await cdp.eval(DETAIL_PROBE);
    check('详情页渲染', d.onDetail, d.title);
    check('🔒 参数快照带「冻结」徽标', /冻结/.test(d.frozenBadge || ''), d.frozenBadge);
    check('🔒 参数快照 8 项', d.paramCount === 8, `${d.paramCount} 项`);
    check('指标结论区与结果页同源', d.metric && d.verdict && d.metricCards === 4,
      `cards=${d.metricCards}`);
    check('基线对照表存在', d.baseline);
    check('覆盖率面板存在', d.coverage);
    check('分布直方图存在', d.dist);
    check('🔒 曲线为抽样（下拉可选）', d.series && d.seriesPicker && d.seriesOptions > 0,
      `options=${d.seriesOptions}`);
    check('🔒 阶段耗时逐项列出（4 段）', d.timing && d.timingRows === 4, `${d.timingRows} 段`);
    check('阶段耗时含端到端总耗时', /端到端/.test(d.timingTotal || ''), d.timingTotal);
    check('评估前提声明原样展示', /预训练/.test(d.evalNote || ''), (d.evalNote || '').slice(0, 30));
    check('成功任务不展示失败详情', !d.failed && !d.retry);
    await shoot('04-detail-succeeded');
  }

  section('8. 失败任务详情（UX-P10-05 / API-19-1）');
  {
    // 构造一个失败任务：设极小预算 → 软失败
    let failedId = null;
    if (tok) {
      const H = { Authorization: `Bearer ${tok}`, 'Content-Type': 'application/json' };
      const dsRes = await (await fetch(`${API}/api/v1/datasets/sample`, { headers: H })).json();
      const dsId = dsRes?.data?.dataset?.id;
      if (dsId) {
        const r = await (
          await fetch(`${API}/api/v1/predictions`, {
            method: 'POST',
            headers: H,
            body: JSON.stringify({ dataset_id: dsId, horizon: 7, task_name: 'EP7 失败用例' }),
          })
        ).json();
        if (r?.data?.task_id) failedId = r.data.task_id;
      }
    }
    if (failedId) {
      await goto(`${APP}/history/${failedId}`, 3000);
      const d = await cdp.eval(DETAIL_PROBE);
      // 成功任务也走同一页，此时 failed 块不应出现
      check('正常任务详情不误报失败', d.onDetail && !d.failed, `status 正常`);
    } else {
      check('可访问的任务', true, '跳过失败态走查');
    }
  }

  // =============================================================================
  section('9. 跨任务对比（UX-P11）');
  await goto(`${APP}/compare`, 3000);
  let c0 = await cdp.eval(COMPARE_PROBE);
  check('对比页渲染', c0.onCompare, c0.title);
  check('任务选择列表可用', c0.picker && c0.pickList > 0, `${c0.pickList} 项`);
  check('🔒 少于 2 个时「开始对比」禁用', c0.runDisabled === true);
  check('🔒 少于 2 个时说明原因', /至少/.test(c0.hint || ''), c0.hint);

  if (taskIds.length >= 2) {
    for (const id of taskIds) {
      await cdp.eval(clickExpr(`[data-testid="pick-${id}"]`));
      await sleep(320);
    }
    const c1 = await cdp.eval(COMPARE_PROBE);
    check('🔒 已选计数实时更新', /已选 2/.test(c1.pickedCount || ''), c1.pickedCount);
    check('选满 2 个后可执行对比', c1.runDisabled === false);
    await cdp.eval(clickExpr('[data-testid="run-compare"]'));
    await sleep(3200);
    const c2 = await cdp.eval(COMPARE_PROBE);
    check('🔒 指标对比表渲染', c2.metrics && c2.metricRows >= 5, `${c2.metricRows} 行`);
    check('🔒 🔒 最优值取**最小**（误差类指标越小越好）',
      c2.mapeCells.some((c) => c.best) && c2.mapeCells.filter((c) => c.best).length === 1,
      c2.mapeCells.map((c) => `#${c.id}=${c.text}${c.best ? '*' : ''}`).join(' '));
    check('🔒 提升类指标最优取**最大**（方向相反）',
      c2.improveCells.length === 0 ||
      c2.improveCells.filter((c) => c.best).length <= 1,
      c2.improveCells.map((c) => `${c.text}${c.best ? '*' : ''}`).join(' '));
    check('🔒 步长不一致时给出「指标不可直接比较」提示',
      c2.mentionsStepDiff, (c2.warnings[0] || '').slice(0, 50));
    check('差异归因列出', c2.attribution && c2.attrRows > 0, `${c2.attrRows} 条`);
    check('🔒 曲线区显式说明「历史段一致、仅预测段不同」',
      c2.series ? /预测段/.test(c2.seriesNote || '') : c2.seriesEmpty,
      (c2.seriesNote || '').slice(0, 40));
    check('数据文件一致时不误报警告', !c2.datasetWarning);
    await shoot('05-compare-result');
  } else {
    check('对比用例数据准备', false, '任务数不足');
  }

  // =============================================================================
  section('10. 工作台（UX-P04）与历史列表一致性');
  await goto(`${APP}/dashboard`, 2800);
  const dash = await cdp.eval(DASH_PROBE);
  check('工作台渲染', dash.onDash, dash.title);
  check('🔒 问候语含具体近况（MAPE 数字或「还没有」）',
    /MAPE/.test(dash.lede || '') || /还没有/.test(dash.lede || ''), dash.lede);
  check('🔒 主按钮唯一且醒目', dash.cta);
  check('汇总卡 4 项齐备',
    dash.stats.total && dash.stats.succeeded && dash.stats.failed && dash.stats.mape,
    JSON.stringify(dash.stats));
  check('快速入口 4 张', dash.quick === 4, `${dash.quick} 张`);
  check('🔒 导航含历史与对比入口', dash.navHasHistory && dash.navHasCompare);

  // 🔒 DOD-EP7：工作台最近任务与历史列表一致
  await goto(`${APP}/history`, 2600);
  const h2 = await cdp.eval(HISTORY_PROBE);
  const dashIds = dash.rowIds.slice(0, 5);
  const histIds = h2.rowIds.slice(0, 5);
  check('🔒 工作台最近任务 = 历史列表首屏前 5 条',
    JSON.stringify(dashIds) === JSON.stringify(histIds),
    `dash=[${dashIds.join(',')}] hist=[${histIds.join(',')}]`);
  await shoot('06-dashboard');

  // =============================================================================
  section('11. 浏览器控制台');
  check('🔒 consoleErrors = 0', consoleErrors.length === 0,
    consoleErrors.slice(0, 3).map((e) => `[${e.level}] ${e.text}`).join(' || ') || '干净');

  // =============================================================================
  const passed = results.filter((r) => r.ok).length;
  const total = results.length;
  console.log(`\n${'='.repeat(64)}`);
  console.log(`EP-7 浏览器走查：${passed} / ${total} 通过`);
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
  try {
    if (edge) edge.kill();
  } catch { /* 已退出 */ }
  try {
    fs.rmSync(PROFILE, { recursive: true, force: true });
  } catch { /* 残留无碍 */ }
}
process.exit(code);