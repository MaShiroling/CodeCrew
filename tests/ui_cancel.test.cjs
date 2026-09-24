const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag = '') {
    this.tag = tag;
    this.children = [];
    this.hidden = false;
    this.disabled = false;
    this.textContent = '';
    this.listeners = new Map();
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(type, callback) { this.listeners.set(type, callback); }
  dispatch(type) { return this.listeners.get(type)?.(); }
  setAttribute() {}
  querySelector(selector) { return this.children.find((child) => child.tag === selector) || null; }
}

const elements = new Map();
const get = (id) => {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
};
get('empty-detail').append(new Element('h2'), new Element('p'));
const document = {
  getElementById: get,
  createElement(tag) { return new Element(tag); },
  querySelectorAll() { return []; },
};
const streams = [];
class FakeEventSource {
  constructor(url) { this.url = url; this.closed = false; streams.push(this); }
  addEventListener() {}
  close() { this.closed = true; }
}
const task = (id, revision) => ({
  task_id: id, trace_id: `trace-${id}`, issue: `Issue ${id}`, repository_path: '/tmp/fixture',
  state: 'planning', revision, rework_rounds: 0, created_at: '2026-09-24T00:00:00Z',
});
const tasks = new Map([
  ['task-a', task('task-a', 3)],
  ['task-b', task('task-b', 6)],
  ['task-c', task('task-c', 2)],
]);
let mode = 'success';
let confirmation = false;
let confirmations = 0;
let releasePost = null;
const posts = [];
const reads = [];
const response = (status, data) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => data,
});
const fetch = async (url, options = {}) => {
  if (url.endsWith('/cancel') && options.method === 'POST') {
    const taskId = url.split('/').at(-2);
    posts.push({ taskId, body: JSON.parse(options.body), headers: options.headers });
    if (mode === 'pending') return new Promise((resolve) => { releasePost = resolve; });
    if (mode === 'network') throw new TypeError('Failed to fetch');
    if (mode === 'missing') { tasks.delete(taskId); return response(404, { error: { code: 'task_not_found' } }); }
    if (mode === 'conflict') {
      tasks.set(taskId, { ...tasks.get(taskId), revision: tasks.get(taskId).revision + 1, state: 'reviewing' });
      return response(409, { error: { code: 'task_state_conflict' } });
    }
    const updated = { ...tasks.get(taskId), state: 'cancelled', revision: tasks.get(taskId).revision + 1 };
    tasks.set(taskId, updated);
    return response(200, updated);
  }
  if (url.startsWith('/api/v1/tasks?')) return response(200, { items: [...tasks.values()], next_offset: null });
  const match = url.match(/^\/api\/v1\/tasks\/(task-[abc])$/);
  if (match) {
    reads.push(match[1]);
    return tasks.has(match[1])
      ? response(200, tasks.get(match[1]))
      : response(404, { error: { code: 'task_not_found' } });
  }
  if (url.endsWith('/room')) return response(200, { room: { members: [] } });
  if (url.endsWith('/plans')) return response(200, { items: [] });
  if (url.includes('/messages?')) return response(200, { items: [], next_after_sequence: null });
  throw new Error(`unexpected fetch ${url}`);
};
const context = vm.createContext({
  document,
  window: { addEventListener() {}, confirm() { confirmations += 1; return confirmation; } },
  fetch, EventSource: FakeEventSource, Intl, Date, URL, encodeURIComponent, console,
  setTimeout, clearTimeout,
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/web/app.js'), 'utf8'), context);
const tick = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setImmediate(resolve)); };

(async () => {
  await tick();
  assert.equal(get('detail-title').textContent, 'Issue task-a');
  assert.equal(get('cancel-task').hidden, false);
  assert.equal(streams.length, 1);

  // Cancelling needs explicit confirmation and the current optimistic revision.
  get('cancel-task').dispatch('click');
  await tick();
  assert.equal(confirmations, 1);
  assert.equal(posts.length, 0);
  confirmation = true;
  mode = 'pending';
  const first = vm.runInContext('cancelTask()', context);
  await tick();
  assert.equal(get('cancel-task').disabled, true);
  assert.equal(posts.length, 1);
  assert.equal(posts[0].taskId, 'task-a');
  assert.equal(posts[0].body.expected_revision, 3);
  assert.equal(posts[0].headers['Content-Type'], 'application/json');
  await vm.runInContext('cancelTask()', context);
  assert.equal(posts.length, 1);
  const cancelledA = { ...tasks.get('task-a'), state: 'cancelled', revision: 4 };
  tasks.set('task-a', cancelledA);
  releasePost(response(200, cancelledA));
  await first;
  assert.equal(get('detail-status').children[0].textContent, '已取消');
  assert.equal(get('cancel-task').hidden, true);
  assert.equal(streams[0].closed, true);
  assert.match(get('notice').textContent, /任务已取消/);
  vm.runInContext('showTask({ ...state.selectedTask, state: "planning", revision: 3 })', context);
  assert.equal(get('detail-status').children[0].textContent, '已取消');
  assert.equal(get('cancel-task').hidden, true);
  await vm.runInContext('cancelTask()', context);
  assert.equal(posts.length, 1);

  // A stale revision refreshes details, but never retries the POST.
  await vm.runInContext('selectTask("task-b")', context);
  assert.equal(get('cancel-task').hidden, false);
  mode = 'conflict';
  const readCount = reads.length;
  await vm.runInContext('cancelTask()', context);
  assert.equal(posts.length, 2);
  assert.equal(posts[1].body.expected_revision, 6);
  assert.equal(reads.length, readCount + 1);
  assert.equal(get('detail-status').children[0].textContent, '评审中');
  assert.equal(get('cancel-task').hidden, false);
  assert.equal(get('cancel-task').disabled, false);
  assert.match(get('notice').textContent, /状态已变化/);
  assert.equal(vm.runInContext('state.selectedTask.revision', context), 7);

  // An unknown network result leaves the task visible and does not retry.
  mode = 'network';
  await vm.runInContext('cancelTask()', context);
  assert.equal(posts.length, 3);
  assert.match(get('notice').textContent, /结果未确认/);
  assert.equal(get('cancel-task').hidden, false);

  // A late response for another task may update the list, not the selected detail or SSE.
  mode = 'pending';
  const pendingB = vm.runInContext('cancelTask()', context);
  await tick();
  assert.equal(posts.length, 4);
  assert.equal(posts[3].body.expected_revision, 7);
  await vm.runInContext('selectTask("task-c")', context);
  const streamC = streams.at(-1);
  const cancelledB = { ...tasks.get('task-b'), state: 'cancelled', revision: 8 };
  tasks.set('task-b', cancelledB);
  releasePost(response(200, cancelledB));
  await pendingB;
  assert.equal(get('detail-title').textContent, 'Issue task-c');
  assert.equal(streamC.closed, false);
  assert.equal(get('cancel-task').hidden, false);
  assert.equal(vm.runInContext('state.tasks.find((item) => item.task_id === "task-b").state', context), 'cancelled');

  mode = 'missing';
  await vm.runInContext('cancelTask()', context);
  assert.equal(posts.length, 5);
  assert.equal(vm.runInContext('state.selectedId', context), null);
  assert.equal(get('task-detail').hidden, true);
  assert.equal(get('cancel-task').hidden, true);
  assert.match(get('notice').textContent, /任务不存在/);
  assert.equal(streamC.closed, true);
})().catch((error) => { console.error(error); process.exitCode = 1; });
