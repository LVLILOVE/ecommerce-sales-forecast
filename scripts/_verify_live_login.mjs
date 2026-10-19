/**
 * 线上登录流程验证：真实浏览器走完「登录 → 是否被弹回登录页」
 * 辅助脚本，非交付物。用法：node scripts/_verify_live_login.mjs
 */
import { spawn, execSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const EDGE = process.env.EDGE_BIN || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const PORT = Number(process.env.CDP_PORT || 9911);
const SITE = process.env.SITE || 'https://sales-forecast-27153.app.workbuddy.host';
const OUT = process.env.OUT_DIR || 'outputs/review-probe';
const PROFILE = path.resolve(OUT, `.edge-live-${Date.now()}`);
// 凭据一律由环境变量传入 —— 本文件会入库，**源码里不留任何默认账号/密码**
const EMAIL = process.env.EMAIL || '';
const PWD = process.env.PWD_ || '';
if (!EMAIL || !PWD) {
  console.error('用法：EMAIL=<邮箱> PWD_=<密码> node scripts/_verify_live_login.mjs');
  console.error('（刻意的：仓库是公开的，账号密码不写进源码）');
  process.exit(2);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
fs.mkdirSync(OUT, { recursive: true });

let edge = null;
let edgeErr = '';

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
      setTimeout(() => { if (this.pending.has(id)) { this.pending.delete(id); reject(new Error('timeout ' + method)); } }, 180000);
    });
  }
  async eval(expression) {
    const r = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) return { __exc: r.exceptionDetails.text };
    return r.result.value;
  }
}
async function waitDevtools(t = 30000) {
  const t0 = Date.now();
  while (Date.now() - t0 < t) {
    try { const r = await fetch(`http://127.0.0.1:${PORT}/json/version`); if (r.ok) return await r.json(); } catch {}
    await sleep(400);
  }
  throw new Error('devtools not ready\n' + edgeErr.slice(-400));
}

function findTarget(list) {
  const page = list.find((x) => x.type === 'page' && !x.url.startsWith('devtools://'));
  if (!page) throw new Error('no page target');
  return page;
}

(async () => {
  edge = spawn(EDGE, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-dev-shm-usage', '--disable-extensions', '--disable-background-networking',
    '--no-proxy-server', '--proxy-bypass-list=<-loopback>',
    '--window-size=1280,900',
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${PROFILE}`, 'about:blank',
  ], { stdio: ['ignore', 'pipe', 'pipe'] });
  edge.stderr.on('data', (d) => (edgeErr += d.toString()));
  const ver = await waitDevtools();
  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const ws = new WebSocket(findTarget(list).webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.addEventListener('open', res, { once: true }); ws.addEventListener('error', () => rej(new Error('ws')), { once: true }); });
  const cdp = new CDP(ws);

  let msgs = [];
  cdp.on((m) => {
    if (m.method === 'Runtime.consoleAPICalled' && ['error', 'warning'].includes(m.params.type)) {
      const t = (m.params.args || []).map((a) => a.value ?? a.description ?? '').join(' ');
      if (!/chrome-extension:\/\/|\/hybridaction\//.test(t)) msgs.push(t.slice(0, 160));
    }
    if (m.method === 'Runtime.exceptionThrown') msgs.push('EXC: ' + String(m.params.exceptionDetails.text).slice(0, 160));
  });
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');
  await cdp.send('Network.enable');

  // 记录 /api/v1/users/me 的响应状态
  const meCalls = [];
  cdp.on((m) => {
    if (m.method === 'Network.responseReceived' && m.params.response.url.includes('/users/me')) {
      meCalls.push(m.params.response.status);
    }
  });

  console.log('browser:', ver.Browser);
  console.log('site   :', SITE);

  await cdp.send('Page.navigate', { url: `${SITE}/login` });
  let t0 = Date.now();
  while (Date.now() - t0 < 60000) {
    try { if ((await cdp.eval('document.readyState')) === 'complete') break; } catch {}
    await sleep(150);
  }
  await sleep(1200);
  // 冷启动时 SPA 挂载可能晚于 readyState=complete —— 必须等表单真的出现
  let formReady = false;
  const tf = Date.now();
  while (Date.now() - tf < 90000) {
    try {
      if (await cdp.eval("!!document.querySelector('input[type=email]')")) { formReady = true; break; }
    } catch {}
    await sleep(500);
  }
  console.log('表单已挂载:', formReady ? '✅' : '❌（等待 90s 超时）');
  console.log('登录页 pathname:', await cdp.eval('location.pathname'));
  console.log('表单字段:', await cdp.eval(`JSON.stringify({
    email: !!document.querySelector('input[type=email]'),
    pwd: !!document.querySelector('input[type=password]'),
    buttons: document.querySelectorAll('button').length
  })`));

  // 填表
  await cdp.eval(`(() => {
    const setVal = (el, v) => {
      const s = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      s.call(el, v);
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    };
    setVal(document.querySelector('input[type=email]'), ${JSON.stringify(EMAIL)});
    setVal(document.querySelector('input[type=password]'), ${JSON.stringify(PWD)});
    return true;
  })()`);
  await sleep(400);
  // 提交（找 type=submit 或第一个含「登录」的按钮）
  await cdp.eval(`(() => {
    const btns = [...document.querySelectorAll('button')];
    const b = btns.find(x => x.type === 'submit') || btns.find(x => /登录/.test(x.textContent || '')) || btns[0];
    b.click(); return true;
  })()`);

  console.log('已提交，等待跳转…');
  let bounced = false, finalPath = '', tokenLen = 0;
  for (let i = 0; i < 40; i++) {
    await sleep(700);
    finalPath = await cdp.eval('location.pathname');
    tokenLen = await cdp.eval(`(() => { try { const raw = localStorage.getItem('sf_token') || localStorage.getItem('token') || ''; return raw.length; } catch(e) { return -1; } })()`);
    if (finalPath !== '/login') break;
  }
  await sleep(2500);
  finalPath = await cdp.eval('location.pathname');
  console.log('最终 pathname :', finalPath);
  bounced = finalPath === '/login';
  console.log('是否被弹回登录:', bounced ? '❌ 是（缺陷仍在）' : '✅ 否');
  console.log('/users/me 响应码序列:', JSON.stringify(meCalls));
  console.log('console 错误:', msgs.length, msgs.length ? JSON.stringify(msgs).slice(0, 300) : '');

  const shot = await cdp.send('Page.captureScreenshot', { format: 'png' });
  const out = path.join(OUT, 'live-login-after.png');
  fs.writeFileSync(out, Buffer.from(shot.data, 'base64'));
  console.log('截图 ->', out);
})()
  .catch((e) => console.error('[FATAL]', e))
  .finally(() => {
    try { if (edge) { const pid = edge.pid; edge.kill('SIGKILL'); try { execSync(`taskkill /PID ${pid} /T /F`, { stdio: 'ignore' }); } catch {} } } catch {}
    try { fs.rmSync(PROFILE, { recursive: true, force: true }); } catch {}
    process.exit(0);
  });
