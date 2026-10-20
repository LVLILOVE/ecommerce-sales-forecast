/**
 * 个人资料页（`/profile`）真实浏览器验收 —— 辅助脚本，非交付物
 * 覆盖：页面可达 / 账号信息只读 / 改昵称 / 原密码错误分支 / 改密成功强制重新登录 / 新密码可登录
 * 用法：CDP_PORT=9981 node scripts/_verify_profile_browser.mjs
 */
import { spawn, execSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const EDGE = process.env.EDGE_BIN || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const PORT = Number(process.env.CDP_PORT || 9981);
const SITE = process.env.SITE || 'http://localhost:5173';
const OUT = process.env.OUT_DIR || 'outputs/review-probe';
const PROFILE = path.resolve(OUT, `.edge-profile-${Date.now()}`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
fs.mkdirSync(OUT, { recursive: true });

const stamp = Date.now().toString().slice(-6);
const EMAIL = `profile_${stamp}@deploy.cn`;
// 密码按运行时间派生：**源码里不留任何固定口令**（仓库是公开的），
// 且每次运行的账号都是临时账号，互不影响。
const PWD0 = `Temp@${stamp}abc`;
const PWD1 = `New@${stamp}xyz`;
const NICK1 = '资料页测试';

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
      setTimeout(() => { if (this.pending.has(id)) { this.pending.delete(id); reject(new Error('timeout ' + method)); } }, 120000);
    });
  }
  async eval(expression) {
    const r = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) return { __exc: r.exceptionDetails.text };
    return r.result.value;
  }
}

const results = [];
function check(name, ok, detail = '') {
  results.push({ name, ok });
  console.log(`  ${ok ? '✅' : '❌'} ${name}${detail ? ' — ' + detail : ''}`);
}

/** 等待某个选择器出现（冷启动时 readyState 可能早于 React 挂载） */
async function waitFor(cdp, sel, timeout = 60000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    try { if (await cdp.eval(`!!document.querySelector(${JSON.stringify(sel)})`)) return true; } catch {}
    await sleep(300);
  }
  return false;
}

/** React 受控输入赋值（直接 set value 不会触发 onChange） */
const setVal = (sel, val) => `(() => {
  const el = document.querySelector(${JSON.stringify(sel)});
  if (!el) return false;
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  setter.call(el, ${JSON.stringify(val)});
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return true;
})()`;

const clickByTestId = (id) => `(() => {
  const el = document.querySelector('[data-testid="${id}"]');
  if (!el) return false;
  el.click(); return true;
})()`;

const textOf = (sel) => `(() => { const e = document.querySelector(${JSON.stringify(sel)}); return e ? e.innerText.trim() : null; })()`;

(async () => {
  edge = spawn(EDGE, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-dev-shm-usage', '--disable-extensions', '--disable-background-networking',
    '--no-proxy-server', '--proxy-bypass-list=<-loopback>',
    '--window-size=1280,900',
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${PROFILE}`, 'about:blank',
  ], { stdio: ['ignore', 'pipe', 'pipe'] });
  edge.stderr.on('data', (d) => (edgeErr += d.toString()));

  let ready = false; const t0 = Date.now();
  while (Date.now() - t0 < 30000) {
    try { const r = await fetch(`http://127.0.0.1:${PORT}/json/version`); if (r.ok) { ready = true; break; } } catch {}
    await sleep(400);
  }
  if (!ready) throw new Error('devtools not ready\n' + edgeErr.slice(-300));

  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const page = list.find((x) => x.type === 'page' && !x.url.startsWith('devtools://'));
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.addEventListener('open', res, { once: true }); ws.addEventListener('error', () => rej(new Error('ws')), { once: true }); });
  const cdp = new CDP(ws);

  const consoleErrs = [];
  cdp.on((m) => {
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
      const t = (m.params.args || []).map((a) => a.value ?? a.description ?? '').join(' ');
      if (!/chrome-extension:\/\/|\/hybridaction\//.test(t)) consoleErrs.push(t.slice(0, 160));
    }
    if (m.method === 'Runtime.exceptionThrown') consoleErrs.push('EXC: ' + String(m.params.exceptionDetails.text).slice(0, 160));
  });
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');

  // ---------- 1) 注册一个临时账号 ----------
  console.log('\n=== 1) 准备账号（注册） ===');
  await cdp.send('Page.navigate', { url: `${SITE}/register` });
  check('注册页表单挂载', await waitFor(cdp, 'input[type=email]'));
  await cdp.eval(setVal('#reg-nickname', '临时用户'));
  await cdp.eval(setVal('#reg-email', EMAIL));
  await cdp.eval(setVal('#reg-password', PWD0));
  await cdp.eval(setVal('#reg-confirm', PWD0));
  await cdp.eval(`(() => { const c=document.querySelector('input[type=checkbox]'); if(c) c.click(); return true; })()`);
  await sleep(300);
  await cdp.eval(`(() => { const b=[...document.querySelectorAll('button')].find(x=>x.type==='submit'); if(b) b.click(); return true; })()`);
  let path0 = '';
  for (let i = 0; i < 40; i++) {
    await sleep(500);
    path0 = await cdp.eval('location.pathname');
    if (path0 !== '/register') break;
  }
  check('注册后进入工作台', path0 === '/dashboard', `pathname=${path0}`);

  // ---------- 2) 顶栏入口 → 个人资料 ----------
  console.log('\n=== 2) 顶栏「个人资料」入口 ===');
  check('顶栏存在个人资料入口', await waitFor(cdp, '[data-testid="nav-profile"]'));
  await cdp.eval(clickByTestId('nav-profile'));
  check('点击后到达 /profile', await (async () => {
    for (let i = 0; i < 20; i++) { await sleep(300); if ((await cdp.eval('location.pathname')) === '/profile') return true; }
    return false;
  })());

  check('页面主体渲染', await waitFor(cdp, '[data-testid="profile-page"]'));
  check('三张卡片齐备', await (async () => {
    const n = await cdp.eval(`document.querySelectorAll('[data-testid^="card-"]').length`);
    return n === 3;
  })(), '期望 3');
  const emailShown = await cdp.eval(textOf('[data-testid="card-account"]'));
  check('账号信息含当前邮箱', typeof emailShown === 'string' && emailShown.includes(EMAIL));

  // ---------- 3) 改昵称 ----------
  console.log('\n=== 3) 修改昵称 ===');
  await cdp.eval(setVal('#profile-nickname', NICK1));
  await sleep(200);
  await cdp.eval(clickByTestId('nickname-save'));
  check('昵称保存成功提示出现', await waitFor(cdp, '[data-testid="nick-ok"]', 20000));
  await sleep(500);
  const headerText = await cdp.eval(`(() => { const d=document.querySelector('header'); return d ? d.innerText : ''; })()`);
  check('顶栏昵称已刷新', typeof headerText === 'string' && headerText.includes(NICK1), headerText.slice(0, 60));

  // 资料页截图（交付证据）
  {
    const shotP = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    const outP = path.join(OUT, 'profile-page.png');
    fs.writeFileSync(outP, Buffer.from(shotP.data, 'base64'));
    console.log('  资料页截图 ->', outP);
  }

  // ---------- 4) 原密码错误分支 ----------
  console.log('\n=== 4) 原密码错误提示 ===');
  await cdp.eval(setVal('#profile-old-pwd', 'WrongPwd@123'));
  await cdp.eval(setVal('#profile-new-pwd', PWD1));
  await cdp.eval(setVal('#profile-confirm-pwd', PWD1));
  await sleep(200);
  await cdp.eval(clickByTestId('pwd-save'));
  check('出现原密码错误提示', await waitFor(cdp, '[data-testid="pwd-alert"]', 30000));
  const alertText = await cdp.eval(textOf('[data-testid="pwd-alert"]'));
  check('提示文案指出「原密码不正确」', typeof alertText === 'string' && alertText.includes('原密码'), (alertText || '').slice(0, 40));
  check('失败后仍停留在 /profile', (await cdp.eval('location.pathname')) === '/profile');

  // ---------- 5) 改密成功 → 强制重新登录 ----------
  console.log('\n=== 5) 改密成功 → 强制重新登录 ===');
  await cdp.eval(setVal('#profile-old-pwd', PWD0));
  await cdp.eval(setVal('#profile-new-pwd', PWD1));
  await cdp.eval(setVal('#profile-confirm-pwd', PWD1));
  await sleep(200);
  await cdp.eval(clickByTestId('pwd-save'));
  let path1 = '';
  for (let i = 0; i < 40; i++) {
    await sleep(500);
    path1 = await cdp.eval('location.pathname');
    if (path1 === '/login') break;
  }
  check('改密后跳转登录页', path1 === '/login', `pathname=${path1}`);
  check('登录页显示成功提示', await waitFor(cdp, '[data-testid="login-notice"]', 15000));
  const notice = await cdp.eval(textOf('[data-testid="login-notice"]'));
  check('提示文案说明需用新密码登录', typeof notice === 'string' && notice.includes('新密码'), (notice || '').slice(0, 40));
  const tokenCleared = await cdp.eval(`(() => { try { return !localStorage.getItem('sp_token'); } catch(e) { return true; } })()`);
  check('本地 Token 已清除', tokenCleared === true);

  // ---------- 6) 旧密码失效 / 新密码可登录 ----------
  console.log('\n=== 6) 新密码登录 ===');
  await cdp.eval(setVal('input[type=email]', EMAIL));
  await cdp.eval(setVal('input[type=password]', PWD1));
  await sleep(200);
  await cdp.eval(`(() => { const b=[...document.querySelectorAll('button')].find(x=>x.type==='submit'); if(b) b.click(); return true; })()`);
  let path2 = '';
  for (let i = 0; i < 40; i++) {
    await sleep(500);
    path2 = await cdp.eval('location.pathname');
    if (path2 !== '/login') break;
  }
  check('新密码登录成功进入工作台', path2 === '/dashboard', `pathname=${path2}`);

  check('全程 console 无错误', consoleErrs.length === 0, consoleErrs.slice(0, 2).join(' | '));

  const shot = await cdp.send('Page.captureScreenshot', { format: 'png' });
  const outPng = path.join(OUT, 'profile-after.png');
  fs.writeFileSync(outPng, Buffer.from(shot.data, 'base64'));
  console.log('\n  截图 ->', outPng);

  const passed = results.filter((r) => r.ok).length;
  console.log(`\n结论：通过 ${passed}/${results.length}`);
  if (passed !== results.length) process.exitCode = 1;
})()
  .catch((e) => { console.error('[FATAL]', e); process.exitCode = 1; })
  .finally(() => {
    try { if (edge) { const pid = edge.pid; edge.kill('SIGKILL'); try { execSync(`taskkill /PID ${pid} /T /F`, { stdio: 'ignore' }); } catch {} } } catch {}
    try { fs.rmSync(PROFILE, { recursive: true, force: true }); } catch {}
    process.exit(process.exitCode || 0);
  });
