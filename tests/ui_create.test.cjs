const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag = '') {
    this.tag = tag;
    this.children = [];
    this.hidden = false;
    this.textContent = '';
    this.value = '';
    this.disabled = false;
    this.listeners = new Map();
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(type, callback) { this.listeners.set(type, callback); }
  dispatch(type, event = {}) { return this.listeners.get(type)?.(event); }
  setAttribute() {}
  focus() {}
  querySelector(selector) { return this.children.find((child) => child.tag === selector) || null; }
}

const elements = new Map();
const get = (id) => {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
};
get('empty-detail').append(new Element('h2'), new Element('p'));
get('create-form').hidden = true;
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

let mode = 'success';
let createdTask = null;
let releasePost = null;
const posts = [];
const response = (status, data) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => data,
});
const makeTask = (issue) => ({
  task_id: 'created-task', trace_id: 'trace-created', issue, repository_path: '/tmp/codecrew-fixture',
  state: 'planning', revision: 1, rework_rounds: 0, created_at: '2026-09-24T00:00:00Z',
});
const fetch = async (url, options = {}) => {
  if (url === '/api/v1/tasks' && options.method === 'POST') {
    posts.push(options);
    if (mode === 'validation') return response(422, { error: { code: 'validation_error' } });
    if (mode === 'invalid-repository') return response(422, { error: { code: 'invalid_repository' } });
    if (mode === 'unavailable') return response(503, { error: { code: 'task_service_unavailable' } });
    if (mode === 'network') throw new TypeError('Failed to fetch');
    if (mode === 'non-json') return { ok: true, status: 201, json: async () => { throw new SyntaxError('bad JSON'); } };
    if (mode === 'pending') return new Promise((resolve) => { releasePost = resolve; });
    const body = JSON.parse(options.body);
    createdTask = makeTask(body.issue);
    return response(201, createdTask);
  }
  if (url.startsWith('/api/v1/tasks?')) return response(200, { items: createdTask ? [createdTask] : [], next_offset: null });
  if (url === '/api/v1/tasks/created-task') return response(200, createdTask);
  if (url.endsWith('/room')) return response(200, { room: { members: [] } });
  if (url.endsWith('/plans')) return response(200, { items: [] });
  if (url.includes('/messages?')) return response(200, { items: [], next_after_sequence: null });
  throw new Error(`unexpected fetch ${url}`);
};
const context = vm.createContext({
  document, window: { addEventListener() {} }, fetch, EventSource: FakeEventSource,
  Intl, Date, URL, encodeURIComponent, console, setTimeout, clearTimeout,
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/web/avatars.js'), 'utf8'), context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/web/app.js'), 'utf8'), context);
const tick = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setImmediate(resolve)); };

(async () => {
  await tick();
  assert.equal(get('task-list').children[0].textContent, '暂无任务。点击“新建任务”开始。');
  get('create-toggle').dispatch('click');
  assert.equal(get('create-form').hidden, false);

  get('create-repository').value = '   ';
  get('create-issue').value = 'Fix parser';
  let prevented = false;
  get('create-form').dispatch('submit', { preventDefault() { prevented = true; } });
  await tick();
  assert.equal(prevented, true);
  assert.equal(posts.length, 0);
  assert.match(get('create-error').textContent, /请填写/);

  get('create-repository').value = '  /tmp/codecrew-fixture  ';
  get('create-issue').value = '  Fix parser  ';
  await vm.runInContext('createTask()', context);
  assert.equal(posts.length, 1);
  assert.equal(posts[0].method, 'POST');
  assert.equal(posts[0].headers['Content-Type'], 'application/json');
  assert.equal(posts[0].headers.Accept, 'application/json');
  assert.equal(posts[0].body, JSON.stringify({ repository_path: '/tmp/codecrew-fixture', issue: 'Fix parser' }));
  assert.equal(get('create-repository').value, '');
  assert.equal(get('create-issue').value, '');
  assert.equal(get('create-form').hidden, true);
  assert.equal(vm.runInContext('state.selectedId', context), 'created-task');
  assert.equal(get('detail-title').textContent, 'Fix parser');
  assert.equal(streams.length, 1);
  assert.match(streams[0].url, /created-task\/events$/);

  get('create-repository').value = '/tmp/codecrew-fixture';
  get('create-issue').value = 'Another fix';
  mode = 'validation';
  await vm.runInContext('createTask()', context);
  assert.match(get('create-error').textContent, /输入不符合要求/);
  assert.equal(get('create-issue').value, 'Another fix');

  mode = 'invalid-repository';
  await vm.runInContext('createTask()', context);
  assert.match(get('create-error').textContent, /仓库路径无效/);
  assert.equal(get('create-repository').value, '/tmp/codecrew-fixture');

  get('create-issue').value = 'x'.repeat(16001);
  const beforeLengthCheck = posts.length;
  await vm.runInContext('createTask()', context);
  assert.equal(posts.length, beforeLengthCheck);
  assert.match(get('create-error').textContent, /超过长度限制/);
  get('create-issue').value = 'Another fix';

  mode = 'unavailable';
  await vm.runInContext('createTask()', context);
  assert.match(get('create-error').textContent, /任务服务尚未配置/);
  assert.doesNotMatch(get('create-error').textContent, /任务可能已创建/);

  mode = 'network';
  const beforeNetwork = posts.length;
  await vm.runInContext('createTask()', context);
  assert.equal(posts.length, beforeNetwork + 1);
  assert.match(get('create-error').textContent, /请先刷新列表确认/);

  mode = 'non-json';
  await vm.runInContext('createTask()', context);
  assert.match(get('create-error').textContent, /请先刷新列表确认/);

  mode = 'pending';
  const first = vm.runInContext('createTask()', context);
  await tick();
  const pendingCount = posts.length;
  assert.equal(get('create-submit').disabled, true);
  await vm.runInContext('createTask()', context);
  assert.equal(posts.length, pendingCount);
  releasePost(response(422, { error: { code: 'validation_error' } }));
  await first;
  assert.equal(get('create-submit').disabled, false);
})().catch((error) => { console.error(error); process.exitCode = 1; });
