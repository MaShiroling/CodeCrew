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
    this.dataset = {};
    this.listeners = new Map();
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(type, callback) { this.listeners.set(type, callback); }
  dispatch(type, event = {}) { return this.listeners.get(type)?.(event); }
  setAttribute() {}
  focus() {}
  setSelectionRange(start) { this.selectionStart = start; this.selectionEnd = start; }
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
const mentionButtons = ['@白金', '@月见', '@鲸鲸'].map((mention) => {
  const button = new Element('button');
  button.dataset.discussionMention = mention;
  return button;
});
const document = {
  getElementById: get,
  createElement(tag) { return new Element(tag); },
  querySelectorAll(selector) { return selector === '[data-discussion-mention]' ? mentionButtons : []; },
};
const task = (id, state) => ({
  task_id: id, trace_id: `trace-${id}`, issue: `Issue ${id}`, repository_path: '/tmp/fixture',
  state, revision: 3, rework_rounds: 0, created_at: '2026-09-28T00:00:00Z',
});
const tasks = new Map([
  ['task-a', task('task-a', 'needs_human')],
  ['task-b', task('task-b', 'planning')],
  ['task-c', task('task-c', 'completed')],
]);
const agentMessage = {
  sequence: 1, message_id: 'agent-1', sender_role: 'planner', sender_name: '白金',
  created_at: '2026-09-28T00:00:00Z', type: 'discussion', content: '请说明目标。',
  correlation_id: 'thread-1', pending_for_human: false, reply_to: null, artifacts: [],
};
const messages = new Map([['task-a', [agentMessage]], ['task-b', []], ['task-c', []]]);
const discussionPosts = [];
const workflowPosts = [];
let postMode = 'success';
let releasePost;
let nextId = 1;
const response = (status, data) => ({ ok: status >= 200 && status < 300, status, json: async () => data });
const fetch = async (url, options = {}) => {
  if (url.startsWith('/api/v1/tasks?')) return response(200, { items: [...tasks.values()], next_offset: null });
  const discussion = url.match(/^\/api\/v1\/tasks\/(task-[abc])\/messages\/discussion$/);
  if (discussion && options.method === 'POST') {
    const body = JSON.parse(options.body);
    discussionPosts.push({ taskId: discussion[1], body });
    if (postMode === 'network') throw new TypeError('Failed to fetch');
    if (postMode === 'pending') return new Promise((resolve) => { releasePost = resolve; });
    const message = {
      sequence: messages.get(discussion[1]).length + 1,
      message_id: `human-${nextId++}`, sender_role: 'human', sender_name: 'human',
      created_at: '2026-09-28T01:00:00Z', type: 'discussion', content: body.content,
      correlation_id: body.reply_to ? 'thread-1' : `thread-${nextId++}`,
      reply_to: body.reply_to || null, artifacts: [], pending_for_human: false,
    };
    messages.get(discussion[1]).push(message);
    return response(201, {
      scope: 'discussion', message, target_roles: ['planner'], task_revision: body.expected_revision,
      execution_authorized: false, agent_dispatched: false, discussion_queued: true,
    });
  }
  if (/\/messages$/.test(url) && options.method === 'POST') {
    workflowPosts.push(JSON.parse(options.body));
    throw new Error('discussion must not use controlled workflow endpoint');
  }
  const detail = url.match(/^\/api\/v1\/tasks\/(task-[abc])$/);
  if (detail) return response(200, tasks.get(detail[1]));
  if (url.endsWith('/room')) return response(200, { room: { members: [
    { role: 'human', kind: 'human' }, { role: 'planner', kind: 'agent', name: '白金' },
    { role: 'implementer', kind: 'agent', name: '月见' },
  ] } });
  if (url.endsWith('/plans')) return response(200, { items: [] });
  const listing = url.match(/^\/api\/v1\/tasks\/(task-[abc])\/messages\?/);
  if (listing) {
    const cursor = Number(new URL(url, 'http://local').searchParams.get('after_sequence'));
    return response(200, { items: messages.get(listing[1]).filter((item) => item.sequence > cursor),
      next_after_sequence: null });
  }
  throw new Error(`unexpected fetch ${url}`);
};
class EventSource {
  constructor(url) { this.url = url; this.closed = false; this.listeners = new Map(); EventSource.instances.push(this); }
  addEventListener(type, callback) { this.listeners.set(type, callback); }
  close() { this.closed = true; }
}
EventSource.instances = [];
const context = vm.createContext({
  document, window: { addEventListener() {} }, fetch, EventSource, Intl, Date, URL,
  encodeURIComponent, crypto: { randomUUID: () => `key-${nextId++}` },
  console, setTimeout, clearTimeout,
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/web/app.js'), 'utf8'), context);
const tick = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setImmediate(resolve)); };
const discussionReply = (index) => get('message-list').children[index].children
  .find((child) => child.className === 'discussion-reply-action');

(async () => {
  await tick();
  assert.equal(get('discussion-composer').hidden, false);
  assert.equal(get('human-composer').hidden, false);
  assert.equal(EventSource.instances.length, 1);
  assert.equal(EventSource.instances[0].closed, false);
  assert.match(EventSource.instances[0].url, /task-a/);

  mentionButtons[0].dispatch('click');
  assert.equal(get('discussion-content').value, '@白金 ');
  get('discussion-content').value += '请解释方案';
  get('discussion-content').dispatch('input');
  let prevented = false;
  get('discussion-form').dispatch('submit', { preventDefault() { prevented = true; } });
  await tick();
  assert.equal(prevented, true);
  assert.equal(discussionPosts.length, 1);
  assert.equal(discussionPosts[0].body.content, '@白金 请解释方案');
  assert.equal(discussionPosts[0].body.expected_revision, 3);
  assert.equal(workflowPosts.length, 0);
  assert.equal(get('discussion-content').value, '');
  assert.match(get('discussion-status').textContent, /等待 Agent 回复/);
  assert.equal(EventSource.instances[0].closed, false);

  const human = messages.get('task-a').at(-1);
  messages.get('task-a').push({ ...agentMessage, sequence: 3, message_id: 'agent-2',
    content: '可以先做方案。', correlation_id: human.correlation_id, reply_to: human.message_id });
  await vm.runInContext('refreshSelected("task-a", state.requestId)', context);
  assert.match(get('discussion-status').textContent, /Agent 已回复/);
  assert.ok(discussionReply(2));
  assert.equal(EventSource.instances[0].closed, false);

  discussionReply(2).dispatch('click');
  assert.equal(get('discussion-reply').hidden, false);
  get('discussion-content').value = '继续说明';
  await vm.runInContext('postDiscussionMessage()', context);
  assert.equal(discussionPosts.length, 2);
  assert.equal(discussionPosts[1].body.reply_to, 'agent-2');
  assert.equal(workflowPosts.length, 0);

  postMode = 'network';
  get('discussion-content').value = '@月见 先讨论测试';
  await vm.runInContext('postDiscussionMessage()', context);
  assert.equal(get('discussion-content').value, '@月见 先讨论测试');
  assert.match(get('discussion-error').textContent, /未确认/);
  const uncertainKey = discussionPosts.at(-1).body.idempotency_key;
  postMode = 'success';
  await vm.runInContext('postDiscussionMessage()', context);
  assert.equal(discussionPosts.at(-1).body.idempotency_key, uncertainKey);

  postMode = 'pending';
  get('discussion-content').value = '@白金 迟到响应';
  const pending = vm.runInContext('postDiscussionMessage()', context);
  await tick();
  assert.equal(get('discussion-submit').disabled, true);
  await vm.runInContext('selectTask("task-b")', context);
  assert.equal(EventSource.instances[0].closed, true);
  assert.equal(get('discussion-composer').hidden, false);
  assert.equal(get('discussion-content').value, '');
  releasePost(response(201, { scope: 'discussion', message: { ...agentMessage, sender_role: 'human',
    type: 'discussion', content: '@白金 迟到响应', reply_to: null }, task_revision: 3,
  execution_authorized: false, agent_dispatched: false, discussion_queued: true }));
  await pending;
  assert.equal(get('detail-title').textContent, 'Issue task-b');
  assert.equal(get('message-list').children[0].textContent, '暂无对话');
  await vm.runInContext('selectTask("task-a")', context);
  assert.equal(get('discussion-content').value, '');

  await vm.runInContext('selectTask("task-c")', context);
  assert.equal(get('discussion-composer').hidden, true);
  assert.equal(EventSource.instances.at(-1).closed, true);
  assert.equal(workflowPosts.length, 0);
})().catch((error) => { console.error(error); process.exitCode = 1; });
