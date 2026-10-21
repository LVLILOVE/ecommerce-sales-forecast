/**
 * 线上 `/profile` 冒烟验证 —— 辅助脚本，非交付物
 * 只做「登录 → 点个人资料 → 页面渲染」三件事，**不改任何数据**（不提交表单）。
 * 用法：EMAIL=<邮箱> PWD_=<密码> CDP_PORT=9987 node scripts/_verify_profile_live.mjs
 */
import { spawn, execSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const EDGE = process.env.EDGE_BIN || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const PORT = Number(process.env.CDP_PORT || 9987);
const SITE = process.env.SITE || 'https://sales-forecast-27153.app.workbuddy.host';
const OUT = process.env.OUT_DIR || 'outputs/review-probe';
const PROFILE = path.resolve(OUT, `.edge-live-profile-${Date.now()}`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const EMAIL = process.env.EMAIL || '';
const PWD = process.env.PWD_ || '';
if (!EMAIL || !PWD) { console.error('用法：EMAIL=<邮箱> PWD_=<密码> node scripts/_verify_profile_live.mjs'); process.exit(2); }
fs.mkdirSync(OUT, { recursive: true });

let edge = null, edgeErr = '';
class CDP {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.pending = new Map(); this.listeners = [];
    ws.addEventListener('message', (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id && this.pending.has(m.id)) {
        const { resolve, reject } = this.pending.get(m.id);
        this.pending.delete(m.id);
        if (m.error) reject(new Error(JSON.stringify(m.error))); else resolve(m.result);
      } else if (m.method) for (const l of [...this.listeners]) l(m);
    });
  }
  on(fn) { this.listeners.push(fn); }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => { if (this.pending.has(id)) { this.pending.delete(id); reject(new Error('timeout ' + method)); } }, 150000);
    });
  }
  async eval(e) {
    const r = await this.send('Runtime.evaluate', { expression: e, returnByValue: true, awaitPromise: true });
    return r.exceptionDetails ? { __exc: r.exceptionDetails.text } : r.result.value;
  }
}
async function waitFor(cdp, sel, timeout = 90000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    try { if (await cdp.eval(`!!document.querySelector(${JSON.stringify(sel)})`)) return true; } catch {}
    await sleep(400);
  }
  return false;
}
const setVal = (sel, val) => `(() => {
  const el = document.querySelector(${JSON.stringify(sel)}); if (!el) return false;
  const s = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  s.call(el, ${JSON.stringify(val)});
  el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true }));
  return true; })()`;

const results = [];
const check = (n, ok, d = '') => { results.push({ n, ok }); console.log(`  ${ok ? '✅' : '❌'} ${n}${d ? ' — ' + d : ''}`); };

(async () => {
  edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-dev-shm-usage', '--disable-extensions', '--no-proxy-server', '--proxy-bypass-list=<-loopback>',
    '--window-size=1280,900', `--remote-debugging-port=${PORT}`, `--user-data-dir=${PROFILE}`, 'about:blank'],
    { stdio: ['ignore', 'pipe', 'pipe'] });
  edge.stderr.on('data', (d) => (edgeErr += d.toString()));
  let ready = false; const t0 = Date.now();
  while (Date.now() - t0 < 30000) {
    try { const r = await fetch(`http://127.0.0.1:${PORT}/json/version`); if (r.ok) { ready = true; break; } } catch {}
    await sleep(400);
  }
  if (!ready) throw new Error('devtools not ready');

  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const page = list.find((x) => x.type === 'page' && !x.url.startsWith('devtools://'));
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.addEventListener('open', res, { once: true }); ws.addEventListener('error', () => rej(new Error('ws')), { once: true }); });
  const cdp = new CDP(ws);
  const errs = [];
  cdp.on((m) => {
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') errs.push(String((m.params.args || []).map((a) => a.value ?? a.description ?? '').join(' ')).slice(0, 140));
    if (m.method === 'Runtime.exceptionThrown') errs.push('EXC: ' + String(m.params.exceptionDetails.text).slice(0, 140));
  });
  await cdp.send('Page.enable'); await cdp.send('Runtime.enable');

  console.log('site:', SITE);
  await cdp.send('Page.navigate', { url: `${SITE}/login` });
  check('登录页挂载', await waitFor(cdp, 'input[type=email]'));

  await cdp.eval(setVal('input[type=email]', EMAIL));
  await cdp.eval(setVal('input[type=password]', PWD));
  await sleep(300);
  await cdp.eval(`(() => { const b=[...document.querySelectorAll('button')].find(x=>x.type==='submit'); if(b) b.click(); return true; })()`);

  let p1 = '';
  for (let i = 0; i < 60; i++) { await sleep(500); p1 = await cdp.eval('location.pathname'); if (p1 !== '/login') break; }
  check('登录成功离开登录页', p1 !== '/login', `pathname=${p1}`);

  check('顶栏出现「个人资料」入口', await waitFor(cdp, '[data-testid="nav-profile"]', 30000));
  await cdp.eval(`(() => { const e=document.querySelector('[data-testid="nav-profile"]'); if(e) e.click(); return true; })()`);

  let p2 = '';
  for (let i = 0; i < 40; i++) { await sleep(400); p2 = await cdp.eval('location.pathname'); if (p2 === '/profile') break; }
  check('到达 /profile', p2 === '/profile', `pathname=${p2}`);

  check('资料页主体渲染', await waitFor(cdp, '[data-testid="profile-page"]', 30000));
  const cards = await cdp.eval(`document.querySelectorAll('[data-testid^="card-"]').length`);
  check('三张卡片齐备', cards === 3, `实际 ${cards}`);
  const acct = await cdp.eval(`(() => { const e=document.querySelector('[data-testid="card-account"]'); return e ? e.innerText : ''; })()`);
  check('账号信息显示当前邮箱', typeof acct === 'string' && acct.includes(EMAIL));
  check('修改密码区存在', await cdp.eval(`!!document.querySelector('#profile-old-pwd') && !!document.querySelector('#profile-new-pwd')`));
  check('console 无错误', errs.length === 0, errs.slice(0, 2).join(' | '));

  const shot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
  const outPng = path.join(OUT, 'live-profile.png');
  fs.writeFileSync(outPng, Buffer.from(shot.data, 'base64'));
  console.log('  截图 ->', outPng);

  const passed = results.filter((r) => r.ok).length;
  console.log(`\n结论：通过 ${passed}/${results.length}`);
  if (passed !== results.length) process.exitCode = 1;
})().catch((e) => { console.error('[FATAL]', e); process.exitCode = 1; })
  .finally(() => {
    try { if (edge) { const pid = edge.pid; edge.kill('SIGKILL'); try { execSync(`taskkill /PID ${pid} /T /F`, { stdio: 'ignore' }); } catch {} } } catch {}
    try { fs.rmSync(PROFILE, { recursive: true, force: true }); } catch {}
    process.exit(process.exitCode || 0);
  });
