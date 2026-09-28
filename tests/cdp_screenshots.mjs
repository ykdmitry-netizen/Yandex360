// Снимки всех страниц консоли через Chrome DevTools Protocol:
// точный вьюпорт 1600px (иначе Quasar включает мобильный режим бокового меню).
// Требует Chrome с --remote-debugging-port=9222 и запущенную консоль на :8080.
// Запуск: node tests/cdp_screenshots.mjs  →  docs/renders/*.png
import { writeFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const BASE = 'http://127.0.0.1:8080';
const OUT = fileURLToPath(new URL('../docs/renders', import.meta.url));
const CDP = 'http://127.0.0.1:9222';
const WIDTH = 1600;
const MAX_HEIGHT = 1600;

const PAGES = [
  ['01-dashboard', '/'],
  ['02-sync', '/sync'],
  ['03-mismatches', '/mismatches'],
  ['04-users', '/users'],
  ['05-backup', '/backup'],
  ['06-restore', '/restore'],
  ['07-retention', '/retention'],
  ['08-logs', '/logs'],
  ['09-settings', '/settings'],
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  mkdirSync(OUT, { recursive: true });
  const targets = await (await fetch(`${CDP}/json/list`)).json();
  const page = targets.find((t) => t.type === 'page');
  if (!page) throw new Error('нет page-таргета CDP');

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  ws.addEventListener('message', (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  });
  await new Promise((res, rej) => {
    ws.addEventListener('open', res, { once: true });
    ws.addEventListener('error', rej, { once: true });
  });
  const send = (method, params = {}) => new Promise((resolve) => {
    const myId = ++id;
    pending.set(myId, resolve);
    ws.send(JSON.stringify({ id: myId, method, params }));
  });

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: WIDTH, height: 1000, deviceScaleFactor: 1, mobile: false });

  for (const [name, route] of PAGES) {
    await send('Page.navigate', { url: BASE + route });
    await sleep(4500);

    const probe = await send('Runtime.evaluate', {
      expression: `JSON.stringify({
        w: window.innerWidth, h: document.documentElement.scrollHeight,
        drawer: (document.querySelector('.q-drawer') || {}).className || '',
        banner: !!Array.from(document.querySelectorAll('*')).find(e => e.textContent === 'Снимок недоступен: tuple indices must be integers or slices, not str')
      })`,
      returnByValue: true,
    });
    const info = JSON.parse(probe.result.result.value);
    const height = Math.min(Math.max(info.h + 20, 1000), MAX_HEIGHT);
    await send('Emulation.setDeviceMetricsOverride', { width: WIDTH, height, deviceScaleFactor: 1, mobile: false });
    await sleep(700);

    const shot = await send('Page.captureScreenshot', { format: 'png' });
    const file = join(OUT, `${name}.png`);
    writeFileSync(file, Buffer.from(shot.result.data, 'base64'));
    console.log(`${name.padEnd(15)} viewport=${info.w}x${height}  drawer=${info.drawer.includes('--mobile') ? 'mobile' : 'standard'}  ${(Buffer.from(shot.result.data, 'base64').length / 1024).toFixed(0)} КБ`);
  }
  ws.close();
}

main().catch((e) => { console.error('ОШИБКА:', e.message); process.exit(1); });
