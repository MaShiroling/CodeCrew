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
    this.value = '';
    this.className = '';
    this.listeners = new Map();
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(type, callback) { this.listeners.set(type, callback); }
  dispatch(type, event = {}) { return this.listeners.get(type)?.(event); }
  setAttribute() {}
  focus() {}
  querySelector(selector) {
    return this.children.find((child) => selector.startsWith('.')
      ? child.className === selector.slice(1) : child.tag === selector) || null;
  }
}

const elements = new Map();
const get = (id) => {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
};
get('empty-detail').append(new Element('h2'), new Element('p'));
get('human-recipient').value = 'planner';
const document = {
  getElementById: get,
  createElement(tag) { return new Element(tag); },
  querySelectorAll() { return []; },
};
const task = (id, state, revision) => ({
  task_id: id, trace_id: `trace-${id}`, issue: `Issue ${id}`, repository_path: '/tmp/fixture',
  state, revision, rework_rounds: 0, created_at: '2026-09-28T00:00:00Z',
});
const tasks = new Map([
  ['task-a', task('task-a', 'needs_human', 3)],
  ['task-b', task('task-b', 'planning', 2)],
]);
const question = {
  sequence: 1, message_id: 'question-1', sender_role: 'planner', sender_name: '白金',
  created_at: '2026-09-28T00:00:00Z', type: 'question', content: '是否保留兼容行为？',
  pending_for_human: true, reply_to: null, artifacts: [],
};
const oldQuestion = { ...question, sequence: 2, message_id: 'question-2',
  content: '已处理的问题', pending_for_human: false };
const messages = new Map([['task-a', [question, oldQuestion]], ['task-b', []]]);
const posts = [];
let mode = 'success';
let releasePost = null;
let nextId = 1;
const response = (status, data) => ({ ok: status >= 200 && status < 300, status, json: async () => data });
const fetch = async (url, options = {}) => {
  if (url.startsWith('/api/v1/tasks?')) return response(200, { items: [...tasks.values()], next_offset: null });
  const post = url.match(/^\/api\/v1\/tasks\/(task-[ab])\/messages$/);
  if (post && options.method === 'POST') {
    const body = JSON.parse(options.body);
    posts.push({ taskId: post[1], body });
    if (mode === 'network') throw new TypeError('Failed to fetch');
    if (mode === 'pending') return new Promise((resolve) => { releasePost = resolve; });
    if (mode === 'conflict') {
      tasks.set(post[1], { ...tasks.get(post[1]), revision: tasks.get(post[1]).revision + 1 });
      return response(409, { error: { code: 'task_state_conflict' } });
    }
    const message = {
      sequence: messages.get(post[1]).length + 1,
      message_id: `human-${nextId++}`, sender_role: 'human', sender_name: 'human',
      created_at: '2026-09-28T01:00:00Z', type: body.reply_to ? 'answer' : 'message',
      content: body.content, reply_to: body.reply_to || null, artifacts: [],
      pending_for_human: false,
    };
    messages.get(post[1]).push(message);
    return response(201, { message, task_revision: body.expected_revision, agent_dispatched: false });
  }
  const detail = url.match(/^\/api\/v1\/tasks\/(task-[ab])$/);
  if (detail) return response(200, tasks.get(detail[1]));
  if (url.endsWith('/room')) return response(200, { room: { members: [
    { role: 'human', kind: 'human' }, { role: 'planner', kind: 'agent' },
  ] } });
  if (url.endsWith('/plans')) return response(200, { items: [] });
  const listing = url.match(/^\/api\/v1\/tasks\/(task-[ab])\/messages\?/);
  if (listing) {
    const cursor = Number(new URL(url, 'http://local').searchParams.get('after_sequence'));
    return response(200, { items: messages.get(listing[1]).filter((item) => item.sequence > cursor),
      next_after_sequence: null });
  }
  throw new Error(`unexpected fetch ${url}`);
};
const context = vm.createContext({
  document, window: { addEventListener() {} }, fetch, Intl, Date, URL, encodeURIComponent,
  crypto: { randomUUID: () => `idempotency-${nextId++}` }, console, setTimeout, clearTimeout,
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/web/app.js'), 'utf8'), context);
const tick = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setImmediate(resolve)); };
const replyButton = (index) => get('message-list').children[index].children.find((child) => child.className === 'reply-action');

(async () => {
  await tick();
  assert.equal(get('human-composer').hidden, false);
  assert.match(get('live-status').textContent, /等待人工输入/);
  vm.runInContext('state.filter = "active"; renderTasks()', context);
  assert.equal(get('task-list').children.length, 2);
  vm.runInContext('state.filter = "terminal"; renderTasks()', context);
  assert.equal(get('task-list').children[0].textContent, '此筛选下暂无任务');
  vm.runInContext('state.filter = "all"; renderTasks()', context);
  assert.equal(get('human-recipient-row').hidden, false);
  assert.ok(replyButton(0));
  assert.equal(replyButton(1), undefined);

  replyButton(0).dispatch('click');
  assert.equal(get('human-reply').hidden, false);
  assert.equal(get('human-recipient-row').hidden, true);
  assert.match(get('human-reply-summary').textContent, /白金/);
  get('human-content').value = '  保留兼容行为  ';
  let prevented = false;
  get('human-form').dispatch('submit', { preventDefault() { prevented = true; } });
  await tick();
  assert.equal(prevented, true);
  assert.equal(posts.length, 1);
  assert.equal(posts[0].body.reply_to, 'question-1');
  assert.equal(posts[0].body.recipient_role, undefined);
  assert.equal(posts[0].body.content, '保留兼容行为');
  assert.equal(posts[0].body.expected_revision, 3);
  assert.equal(get('human-content').value, '');
  assert.match(get('human-status').textContent, /尚未启动/);
  assert.equal(replyButton(0), undefined);
  assert.equal(get('message-list').children.at(-1).children[2].textContent, '保留兼容行为');

  get('human-recipient').value = 'implementer';
  get('human-content').value = '补充实现边界';
  await vm.runInContext('postHumanMessage()', context);
  assert.equal(posts.length, 2);
  assert.equal(posts[1].body.recipient_role, 'implementer');
  assert.equal(posts[1].body.reply_to, undefined);
  assert.equal(posts[1].body.expected_revision, 3);
  assert.equal(posts[0].body.idempotency_key === posts[1].body.idempotency_key, false);

  get('human-content').value = '   ';
  await vm.runInContext('postHumanMessage()', context);
  assert.equal(posts.length, 2);
  assert.match(get('human-error').textContent, /请填写/);

  mode = 'network';
  get('human-content').value = '保留未确认草稿';
  await vm.runInContext('postHumanMessage()', context);
  assert.equal(posts.length, 3);
  assert.equal(get('human-content').value, '保留未确认草稿');
  assert.match(get('human-error').textContent, /未确认/);
  mode = 'success';
  await vm.runInContext('postHumanMessage()', context);
  assert.equal(posts.length, 4);
  assert.equal(posts[2].body.idempotency_key, posts[3].body.idempotency_key);

  mode = 'conflict';
  get('human-content').value = '冲突时不自动重发';
  await vm.runInContext('postHumanMessage()', context);
  assert.equal(posts.length, 5);
  assert.equal(get('human-content').value, '冲突时不自动重发');
  assert.match(get('human-error').textContent, /不会自动重试/);
  assert.equal(vm.runInContext('state.selectedTask.revision', context), 4);

  mode = 'pending';
  get('human-content').value = '迟到响应';
  const pending = vm.runInContext('postHumanMessage()', context);
  await tick();
  assert.equal(get('human-submit').disabled, true);
  assert.equal(posts.length, 6);
  await vm.runInContext('selectTask("task-b")', context);
  assert.equal(get('human-composer').hidden, true);
  releasePost(response(201, {
    message: { sequence: 9, message_id: 'late', sender_role: 'human', content: '迟到响应',
      reply_to: null, sender_name: 'human', type: 'message', artifacts: [] },
    task_revision: 4, agent_dispatched: false,
  }));
  await pending;
  assert.equal(get('detail-title').textContent, 'Issue task-b');
  assert.equal(get('human-composer').hidden, true);
  assert.equal(get('message-list').children[0].textContent, '暂无对话');
})().catch((error) => { console.error(error); process.exitCode = 1; });
