// Проверка всех имён иконок из icons.json: рендерится ли имя одним глифом
// (лигатурой) в шрифте Material Icons, который грузит NiceGUI.
// Требует Chrome, запущенный с --remote-debugging-port=9222.
// Запуск: node tests/cdp_iconaudit.mjs
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const CDP = 'http://127.0.0.1:9222';
const ICONS_PATH = fileURLToPath(new URL('./icons.json', import.meta.url));
const ICONS = JSON.parse(readFileSync(ICONS_PATH, 'utf8'));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const names = Object.keys(ICONS).sort();
  const targets = await (await fetch(`${CDP}/json/list`)).json();
  const page = targets.find((t) => t.type === 'page');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  });
  await new Promise((res) => ws.addEventListener('open', res, { once: true }));
  const send = (method, params = {}) => new Promise((resolve) => {
    const myId = ++id; pending.set(myId, resolve);
    ws.send(JSON.stringify({ id: myId, method, params }));
  });

  await send('Runtime.enable');
  await send('Page.navigate', { url: 'http://127.0.0.1:8080/' });
  await sleep(5000);

  const expr = `
    (() => {
      const icon = document.querySelector('.y-brand-mark .q-icon');
      const cs = getComputedStyle(icon);
      const ctx = document.createElement('canvas').getContext('2d');
      ctx.font = cs.fontSize + ' ' + cs.fontFamily;
      const unit = ctx.measureText('m').width;
      const out = { font: cs.fontFamily, unit, widths: {} };
      for (const n of ${JSON.stringify(names)}) out.widths[n] = +ctx.measureText(n).width.toFixed(1);
      return JSON.stringify(out);
    })()`;
  const res = await send('Runtime.evaluate', { expression: expr, returnByValue: true });
  const data = JSON.parse(res.result.result.value);

  const bad = [];
  for (const [name, width] of Object.entries(data.widths)) {
    const glyphs = Math.round(width / data.unit);
    if (glyphs > 1) bad.push({ name, width, glyphs, used: ICONS[name] });
  }
  console.log(`шрифт: ${data.font}, эталон одного глифа: ${data.unit}px, проверено имён: ${names.length}`);
  if (!bad.length) {
    console.log('ВСЕ иконки рендерятся одним глифом — сломанных нет');
  } else {
    console.log(`\nСЛОМАННЫЕ иконки (${bad.length}): имя разложилось на несколько глифов и вылезает из плашки:`);
    for (const b of bad) console.log(`  ${b.name.padEnd(18)} ${String(b.width).padStart(6)}px ≈ ${b.glyphs} глифа — ${b.used.join(', ')}`);
  }
  ws.close();
}
main().catch((e) => { console.error('ОШИБКА:', e.message); process.exit(1); });
