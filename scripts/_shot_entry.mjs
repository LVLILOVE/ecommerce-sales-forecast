/**
 * 入口页验证：真实浏览器渲染 + 本地链接可达性（辅助脚本，非交付物）
 * 用法：node scripts/_shot_entry.mjs
 */
import { spawn, execSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const EDGE = process.env.EDGE_BIN || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const PORT = Number(process.env.CDP_PORT || 9801);
const ROOT = path.resolve(process.env.PROJ_DIR || '.');
const OUT = process.env.OUT_DIR || 'outputs/review-probe';
const PROFILE = path.resolve(OUT, `.edge-entry-${Date.now()}`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
fs.mkdirSync(OUT, { recursive: true });

const PAGES = ['前端.html', '后端.html', '产品原型.html'];
const fileUrl = (name) => 'file:///' + encodeURI(path.join(ROOT, name).replace(/\\/g, '/'));

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
    const r = await this.send('Runtime.evaluate', { expression, returnByValue: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.text);
    return r.result.value;
  }
}
async function waitDevtools(t = 30000) {
  const t0 = Date.now();
  while (Date.now() - t0 < t) {
    try { const r = await fetch(`http://127.0.0.1:${PORT}/json/version`); if (r.ok) return await r.json(); } catch {}
    await sleep(400);
  }
  throw new Error('devtools not ready\n' + edgeErr.slice(-600));
}

const PROBE = `(() => {
  const q=(s)=>document.querySelector(s), qa=(s)=>[...document.querySelectorAll(s)];
  return {
    title: document.title,
    h1: q('h1') ? q('h1').innerText.replace(/\\s+/g,' ').trim().slice(0,80) : null,
    navHrefs: qa('header nav a').map(a=>a.getAttribute('href')),
    navOn: qa('header nav a.on').map(a=>a.textContent.trim()),
    sections: qa('main section').length,
    h2: qa('main section h2').map(h=>h.innerText.replace(/\\s+/g,' ').trim()),
    tables: qa('main table').length,
    totalRows: qa('main table tbody tr').length,
    hrefs: qa('main a[href]').map(a=>a.getAttribute('href')),
    bodyChars: document.body.innerText.length,
    styled: getComputedStyle(document.body).backgroundColor,
    gridOrFlex: (()=>{const e=q('main section'); return e?getComputedStyle(e).display:null;})(),
    svcClass: (()=>{const e=q('#svc'); return e?e.className:null;})(),
    svcState: qa('.svc-state').map(e=>e.textContent.trim()),
    svcDots: qa('.dot').map(e=>e.className),
    svcSummary: (()=>{const e=q('#svc-summary'); return e?e.innerText.trim():null;})(),
    guideShown: (()=>{const e=q('#svc-guide'); return e?e.className.includes('show'):null;})(),
  };
})()`;

(async () => {
  edge = spawn(EDGE, [
    '--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
    '--disable-dev-shm-usage','--hide-scrollbars','--disable-extensions',
    '--disable-component-extensions-with-background-pages','--disable-background-networking',
    '--no-proxy-server','--proxy-bypass-list=<-loopback>','--window-size=1280,2600',
    '--allow-file-access-from-files',
    `--remote-debugging-port=${PORT}`,`--user-data-dir=${PROFILE}`,'about:blank',
  ], { stdio: ['ignore', 'pipe', 'pipe'] });
  edge.stderr.on('data', (d) => (edgeErr += d.toString()));
  const ver = await waitDevtools();

  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const target = list.find((x) => x.type === 'page');
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.addEventListener('open', res, { once: true }); ws.addEventListener('error', () => rej(new Error('ws')), { once: true }); });
  const cdp = new CDP(ws);

  let msgs = [];
  cdp.on((m) => {
    if (m.method === 'Runtime.consoleAPICalled' && ['error', 'warning'].includes(m.params.type)) {
      const t = (m.params.args || []).map((a) => a.value ?? a.description ?? '').join(' ');
      if (!/chrome-extension:\/\/|\/hybridaction\//.test(t)) msgs.push(t.slice(0, 200));
    }
    if (m.method === 'Runtime.exceptionThrown') msgs.push('EXC: ' + String(m.params.exceptionDetails.text).slice(0, 200));
  });
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');

  console.log('browser:', ver.Browser);
  console.log('root   :', ROOT);
  let allOk = true;

  for (const name of PAGES) {
    const url = fileUrl(name);
    msgs = [];
    await cdp.send('Page.navigate', { url });
    const t0 = Date.now();
    while (Date.now() - t0 < 20000) {
      try { if ((await cdp.eval('document.readyState')) === 'complete') break; } catch {}
      await sleep(120);
    }
    await sleep(4200);
    const r = await cdp.eval(PROBE);

    console.log('\n' + '='.repeat(72));
    console.log('■ ' + name);
    console.log('='.repeat(72));
    console.log('  title      :', r.title);
    console.log('  h1         :', r.h1);
    console.log('  nav        :', JSON.stringify(r.navHrefs), '当前=', JSON.stringify(r.navOn));
    console.log('  sections   :', r.sections, '｜ tables:', r.tables, '｜ tbody 行合计:', r.totalRows);
    console.log('  body 字符数:', r.bodyChars, '｜ section display:', r.gridOrFlex);
    console.log('  服务状态卡 :', r.svcClass, '｜ 状态:', JSON.stringify(r.svcState));
    console.log('              圆点:', JSON.stringify(r.svcDots), '｜ 引导块显示:', r.guideShown);
    console.log('              摘要:', r.svcSummary);
    console.log('  console 消息:', msgs.length, msgs.length ? JSON.stringify(msgs).slice(0, 300) : '');

    // 本地相对链接可达性
    const local = [...new Set(r.hrefs)].filter((h) => h && !/^https?:|^#|^mailto:/.test(h));
    console.log('  本地链接可达性:');
    for (const h of local) {
      const abs = path.join(ROOT, decodeURIComponent(h));
      const ok = fs.existsSync(abs);
      if (!ok) allOk = false;
      console.log(`    ${ok ? '[ OK ]' : '[MISS]'} ${h}`);
    }
    const remote = [...new Set(r.hrefs)].filter((h) => /^https?:/.test(h));
    console.log('  服务链接:', JSON.stringify(remote));

    const shot = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    const out = path.join(OUT, `entry-${name.replace('.html', '')}.png`);
    fs.writeFileSync(out, Buffer.from(shot.data, 'base64'));
    console.log('  截图 ->', out);

    // ---- 强制失败分支：覆盖 fetch 后点「重新检测」，验证降级渲染与启动引导 ----
    //  仅对含「服务状态卡」(#svc) 的页面执行 —— 产品原型.html 无需服务，没有该卡
    if (r.svcClass !== null) {
      await cdp.eval(`window.__origFetch = window.fetch; window.fetch = function(){ return Promise.reject(new TypeError('Failed to fetch')); }; true`);
      await cdp.eval(`(function(){var b=document.getElementById('svc-recheck'); if(b) b.click(); return true;})()`);
      await sleep(1300);
      const r2 = await cdp.eval(PROBE);
      const downOk = r2.svcClass === 'svc bad' && r2.guideShown === true
        && r2.svcDots.every((c) => c === 'dot down');
      if (!downOk) allOk = false;
      console.log('  [强制失败分支] 卡片类:', r2.svcClass, '｜ 状态:', JSON.stringify(r2.svcState));
      console.log('              圆点:', JSON.stringify(r2.svcDots), '｜ 引导块显示:', r2.guideShown);
      console.log('              摘要:', r2.svcSummary);
      console.log('              判定:', downOk ? '✅ 降级渲染正确（全红 + 引导块展开）' : '❌ 降级渲染异常');
      const shot2 = await cdp.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true, clip: { x: 0, y: 0, width: 1280, height: 760, scale: 1 } });
      const out2 = path.join(OUT, `entry-${name.replace('.html', '')}-DOWN.png`);
      fs.writeFileSync(out2, Buffer.from(shot2.data, 'base64'));
      console.log('  降级截图 ->', out2);
      await cdp.eval(`window.fetch = window.__origFetch; true`);
    }
  }

  console.log('\n' + '='.repeat(72));
  console.log(allOk ? `结论：${PAGES.length} 个入口页渲染正常，所有本地链接均可达 ✅` : '结论：存在不可达的本地链接 / 降级渲染异常 ❌');
})()
  .catch((e) => console.error('[FATAL]', e))
  .finally(() => {
    try { if (edge) { const pid = edge.pid; edge.kill('SIGKILL'); try { execSync(`taskkill /PID ${pid} /T /F`, { stdio: 'ignore' }); } catch {} } } catch {}
    try { fs.rmSync(PROFILE, { recursive: true, force: true }); } catch {}
    process.exit(0);
  });
