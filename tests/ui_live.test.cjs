const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag = '') { this.tag = tag; this.children = []; this.hidden = false; this.textContent = ''; this.className = ''; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener() {}
  setAttribute() {}
  querySelector(selector) { return this.children.find((child) => selector.startsWith('.') ? child.className === selector.slice(1) : child.tag === selector) || null; }
}

const elements = new Map();
const document = {
  getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
  createElement(tag) { return new Element(tag); },
  querySelectorAll() { return []; },
};
const get = (id) => document.getElementById(id);
get('empty-detail').append(new Element('h2'), new Element('p'));
const timers = [];
const streams = [];
let taskState = 'planning';
let messages = [];
let failRoomFor = null;
let failList = false;
class FakeEventSource {
  constructor(url) { this.url = url; this.listeners = new Map(); this.closed = false; streams.push(this); }
  addEventListener(type, callback) { this.listeners.set(type, callback); }
  close() { this.closed = true; }
  emit(type) { this.listeners.get(type)?.({ type }); }
}
const task = (id, state) => ({ task_id: id, trace_id: `trace-${id}`, issue: `Issue ${id}`, state, rework_rounds: 0, created_at: '2026-09-24T00:00:00Z' });
const fetch = async (url) => {
  let data;
  if (url.startsWith('/api/v1/tasks?')) {
    if (failList) return { ok: false, json: async () => ({ error: { code: 'task_service_unavailable', message: 'Service unavailable' } }) };
    data = { items: [task('task-a', taskState), task('task-b', 'planning')], next_offset: null };
  }
  else if (url.endsWith('/room')) {
    if (url.includes(failRoomFor)) return { ok: false, json: async () => ({ error: { message: 'Room unavailable' } }) };
    data = { room: { members: [] } };
  }
  else if (url.endsWith('/plans')) data = { items: [] };
  else if (url.includes('/messages?')) {
    const cursor = Number(new URL(url, 'http://localhost').searchParams.get('after_sequence'));
    data = { items: messages.filter((item) => item.sequence > cursor), next_after_sequence: null };
  } else if (url.endsWith('/task-a')) data = task('task-a', taskState);
  else if (url.endsWith('/task-b')) data = task('task-b', 'planning');
  else throw new Error(`unexpected fetch ${url}`);
  return { ok: true, json: async () => data };
};
const context = vm.createContext({
  document, window: { addEventListener() {}, location: {search: ''} }, fetch, EventSource: FakeEventSource,
  Intl, Date, URL, URLSearchParams, encodeURIComponent, console,
  setTimeout(callback) { timers.push(callback); return timers.length; },
  clearTimeout() {},
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/web/avatars.js'), 'utf8'), context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/web/app.js'), 'utf8'), context);
const tick = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setImmediate(resolve)); };
const runTimers = async () => { while (timers.length) { timers.shift()(); await tick(); } };

(async () => {
  await tick();
  assert.equal(streams.length, 1);
  assert.match(streams[0].url, /task-a\/events$/);
  assert.equal(get('live-status').textContent, '正在连接实时事件…');
  assert.equal(get('inspector-workflow').hidden, false);

  messages = [{ sequence: 1, sender_role: 'planner', sender_name: '白金', created_at: '2026-09-24T00:00:00Z', type: 'message', content: 'Plan ready', artifacts: [] }];
  streams[0].emit('chat_message_persisted');
  await runTimers();
  assert.equal(get('message-list').children.length, 1);
  assert.equal(get('message-list').children[0].children[2].textContent, 'Plan ready');

  await vm.runInContext('selectTask("task-b")', context);
  assert.equal(streams[0].closed, true);
  assert.equal(streams.length, 2);
  assert.match(streams[1].url, /task-b\/events$/);
  streams[0].emit('chat_message_persisted');
  await runTimers();
  assert.equal(get('detail-title').textContent, 'Issue task-b');

  taskState = 'rework';
  await vm.runInContext('selectTask("task-a")', context);
  assert.equal(get('detail-status').children[0].textContent, '返工中');
  taskState = 'completed';
  streams[2].emit('completion_decided');
  await runTimers();
  assert.equal(get('detail-status').children[0].textContent, '已完成');
  assert.equal(streams[2].closed, true);
  assert.match(get('live-status').textContent, /任务已结束/);

  failList = true;
  assert.equal(await vm.runInContext('loadTasks()', context), false);
  assert.equal(get('task-list').children.length, 2);
  assert.match(get('notice').textContent, /任务服务尚未配置/);
  failList = false;

  failRoomFor = 'task-b';
  await vm.runInContext('selectTask("task-b")', context);
  assert.equal(get('task-detail').hidden, true);
  assert.equal(get('inspector-workflow').hidden, true);
  assert.equal(get('empty-detail').hidden, false);
  assert.equal(get('empty-detail').querySelector('h2').textContent, '任务详情暂不可用');
  assert.match(get('notice').textContent, /Room unavailable/);
  assert.equal(streams.length, 3);

  // The chat authorization link selects its exact Task, not merely the first card.
  vm.runInContext('state.selectedId = null', context);
  context.window.location.search = '?task=task-a';
  assert.equal(await vm.runInContext('loadTasks()', context), true);
  assert.equal(get('detail-title').textContent, 'Issue task-a');
})().catch((error) => { console.error(error); process.exitCode = 1; });
