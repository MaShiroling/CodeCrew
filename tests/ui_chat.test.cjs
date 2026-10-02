const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') {
    this.tag = tag; this.children = []; this.listeners = new Map(); this.dataset = {};
    this.value = ''; this.textContent = ''; this.hidden = false; this.disabled = false;
    this.className = ''; this.selectionStart = 0; this.scrollHeight = 100;
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  addEventListener(type, callback) { this.listeners.set(type, callback); }
  dispatch(type, event = {}) { return this.listeners.get(type)?.(event); }
  setAttribute(name, value) { this[name] = value; }
  querySelector(selector) {
    if (selector.startsWith('.')) return this.children.find((child) =>
      child.className?.split(' ').includes(selector.slice(1))) || null;
    return null;
  }
  focus() { this.focused = true; }
  setSelectionRange(start) { this.selectionStart = start; }
}

const elements = new Map();
const get = (id) => {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
};
get('active-room').hidden = true;
get('reply-preview').hidden = true;
get('context-preview').hidden = true;
get('create-room-error').hidden = true;
get('conversation-error').hidden = true;
get('send-error').hidden = true;
get('coding-panel').hidden = true;
get('coding-preview').hidden = true;
get('coding-result').hidden = true;
get('coding-error').hidden = true;
const mentionButtons = ['@白金', '@月见', '@鲸鲸'].map((mention) => {
  const button = new Element('button'); button.dataset.mention = mention; return button;
});
const document = {
  getElementById: get,
  createElement: (tag) => new Element(tag),
  querySelectorAll: (selector) => selector === '[data-mention]' ? mentionButtons : [],
};

const roomId = 'room-1';
const members = [
  {member_id: 'human', role: 'human', name: 'human'},
  {member_id: 'planner', role: 'planner', name: '白金'},
  {member_id: 'implementer', role: 'implementer', name: '月见'},
  {member_id: 'reviewer', role: 'reviewer', name: '鲸鲸'},
];
const rooms = [];
const messages = [];
const turns = [];
const posts = [];
const preflightPosts = [];
const authorizationPosts = [];
let nextKey = 1;
let postMode = 'ok';
let authorizationMode = 'ok';
let holdNextTurns = null;
const response = (status, data) => ({ok: status >= 200 && status < 300, status, json: async () => data});
const fetch = async (url, options = {}) => {
  if (url.includes('/api/v1/tasks')) throw new Error('chat UI must not call task API');
  if (url === '/api/v1/chats/coding-capability') {
    return response(200, {available: true, allowed_paths: ['src'],
      demo_repository_path: '/tmp/example-repo', demo_issue: 'Set value to two'});
  }
  if (url === `/api/v1/chats/${roomId}/coding-task-preflight` && options.method === 'POST') {
    const body = JSON.parse(options.body);
    preflightPosts.push(body);
    return response(200, {room_id: roomId, trace_id: 'trace-1', ...body,
      base_commit: 'a'.repeat(40), execution_authorized: false, task_created: false});
  }
  if (url === `/api/v1/chats/${roomId}/coding-tasks` && options.method === 'POST') {
    const body = JSON.parse(options.body);
    authorizationPosts.push(body);
    if (authorizationMode === 'network') throw new TypeError('connection reset');
    return response(201, {room_id: roomId, source_message_id: body.source_message_id,
      task_id: 'task-123', task_trace_id: 'task-trace', repository_path: body.repository_path,
      base_commit: body.expected_base_commit, allowed_paths: body.allowed_paths,
      execution_authorized: true, task_created: true});
  }
  if (url === '/api/v1/chats?limit=100&offset=0') return response(200, {items: [...rooms]});
  if (url === '/api/v1/chats' && options.method === 'POST') {
    const body = JSON.parse(options.body);
    const room = {room_id: roomId, trace_id: 'trace-1', title: body.title, status: 'active',
      members, created_at: '2026-09-30T12:00:00Z'};
    if (!rooms.length) rooms.push(room);
    return response(201, room);
  }
  if (url.startsWith('/api/v1/chats/room-2/messages?')) return response(200, {items: []});
  if (url === '/api/v1/chats/room-2/turns') return response(200, {items: []});
  if (url.startsWith(`/api/v1/chats/${roomId}/messages?`)) {
    const cursor = Number(new URL(url, 'http://local').searchParams.get('after_sequence'));
    return response(200, {items: messages.filter((item) => item.sequence > cursor)});
  }
  if (url === `/api/v1/chats/${roomId}/turns`) {
    if (holdNextTurns) {
      const gate = holdNextTurns;
      holdNextTurns = null;
      const snapshot = turns.map((turn) => ({...turn}));
      gate.started();
      await gate.wait;
      return response(200, {items: snapshot});
    }
    return response(200, {items: [...turns]});
  }
  if (url === `/api/v1/chats/${roomId}/messages` && options.method === 'POST') {
    const body = JSON.parse(options.body);
    posts.push(body);
    if (postMode === 'network') throw new TypeError('connection reset');
    const message = {message_id: `human-${messages.length + 1}`, room_id: roomId,
      sender_id: 'human', content: body.content, reply_to: body.reply_to,
      context_anchor_id: body.context_anchor_id,
      created_at: '2026-09-30T12:00:00Z'};
    messages.push({sequence: messages.length + 1, message, deliveries: []});
    turns.push({turn_id: `turn-${turns.length + 1}`, room_id: roomId,
      message_id: message.message_id, recipient_id: body.content.includes('月见') ? 'implementer' : 'planner',
      status: 'running', updated_at: '2026-09-30T12:00:00Z'});
    return response(201, {message: messages.at(-1), discussion_queued: true, execution_authorized: false});
  }
  if (url.match(/\/turns\/[^/]+\/cancel$/) && options.method === 'POST') {
    const turn = turns.find((item) => url.includes(`/${item.turn_id}/cancel`));
    turn.status = 'cancelled';
    return response(200, turn);
  }
  throw new Error(`unexpected fetch ${url}`);
};
const location = {href: 'http://local/ui/chat/', search: ''};
const window = {location, history: {replaceState(_state, _title, next) {
  location.href = String(next); location.search = new URL(location.href).search;
}}, setInterval(callback, delay) { this.poll = callback; this.pollDelay = delay; }, addEventListener() {}};
class EventSource {
  constructor(url) { this.url = url; this.listeners = new Map(); this.closed = false; EventSource.instances.push(this); }
  addEventListener(type, callback) { this.listeners.set(type, callback); }
  emit(type) { return this.listeners.get(type)?.(); }
  close() { this.closed = true; }
}
EventSource.instances = [];
const context = vm.createContext({document, window, fetch, crypto: {randomUUID: () => `key-${nextKey++}`},
  URL, URLSearchParams, Intl, Date, EventSource, console});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/web/avatars.js'), 'utf8'), context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/web/chat.js'), 'utf8'), context);
const tick = async () => { for (let i = 0; i < 10; i++) await new Promise((resolve) => setImmediate(resolve)); };

(async () => {
  const reviewerAvatar = window.CodeCrewAvatars.create('reviewer', '鲸鲸');
  assert.equal(reviewerAvatar.className, 'avatar reviewer');
  assert.equal(reviewerAvatar.children[0].textContent, '鲸');
  assert.equal(reviewerAvatar.children[1].src, '/ui/assets/avatars/reviewer.png');
  assert.notEqual(reviewerAvatar.children[1].loading, 'lazy');
  assert.equal(reviewerAvatar.children[1].hidden, true);
  reviewerAvatar.children[1].dispatch('load');
  assert.equal(reviewerAvatar.children[0].hidden, true);
  assert.equal(reviewerAvatar.children[1].hidden, false);
  reviewerAvatar.children[1].dispatch('error');
  assert.equal(reviewerAvatar.children[0].hidden, false);
  assert.equal(reviewerAvatar.children[1].hidden, true);
  assert.equal(window.CodeCrewAvatars.create('planner', '白金').children.length, 1);
  assert.equal(window.CodeCrewAvatars.create('implementer', '月见').children.length, 1);

  await tick();
  assert.match(get('room-list').children[0].textContent, /还没有房间/);
  assert.equal(window.pollDelay, 10000);

  get('room-title').value = '输入校验讨论';
  await get('create-room-form').dispatch('submit', {preventDefault() {}});
  assert.equal(get('conversation-title').textContent, '输入校验讨论');
  assert.equal(get('empty-room').hidden, true);
  assert.match(location.search, /room=room-1/);
  assert.equal(get('member-list').children.length, 3);
  assert.equal(get('member-list').children[2].children[0].className, 'avatar reviewer');
  assert.equal(authorizationPosts.length, 0);
  assert.match(EventSource.instances[0].url, /room-1\/events$/);
  EventSource.instances[0].emit('open');
  assert.match(get('room-subtitle').textContent, /实时连接中/);
  EventSource.instances[0].emit('error');
  assert.match(get('room-subtitle').textContent, /低频轮询/);
  EventSource.instances[0].emit('open');
  assert.match(get('room-subtitle').textContent, /实时连接中/);

  mentionButtons[0].dispatch('click');
  get('message-content').value += '讨论兼容性';
  await get('message-form').dispatch('submit', {preventDefault() {}});
  assert.equal(posts.length, 1);
  assert.equal(posts[0].content, '@白金 讨论兼容性');
  assert.equal(get('message-list').children.length, 1);
  assert.match(get('turn-list').children[0].children[0].children[1].textContent, /正在回复/);
  assert.equal(get('message-content').value, '');
  assert.equal(authorizationPosts.length, 0, 'chat message must not authorize code');

  const codingAction = get('message-list').children[0].children[1].children[2];
  assert.match(codingAction.textContent, /受控编码任务/);
  codingAction.dispatch('click');
  assert.equal(get('coding-panel').hidden, false);
  assert.equal(get('coding-scope').value, 'src');
  assert.equal(get('coding-repository').value, '/tmp/example-repo');
  assert.equal(get('coding-issue').value, 'Set value to two');
  get('coding-repository').value = '/tmp/example-repo';
  get('coding-issue').value = 'Set value to two';
  get('coding-issue').dispatch('input');
  await get('coding-form').dispatch('submit', {preventDefault() {}});
  assert.equal(preflightPosts.length, 1);
  assert.equal(preflightPosts[0].source_message_id, 'human-1');
  assert.equal(get('coding-preview').hidden, false);
  assert.equal(get('coding-authorize').disabled, true);
  assert.equal(authorizationPosts.length, 0);
  get('coding-issue').value = 'Set value to two and verify';
  get('coding-issue').dispatch('input');
  assert.equal(get('coding-preview').hidden, true, 'editing the goal invalidates preflight');
  await get('coding-form').dispatch('submit', {preventDefault() {}});
  assert.equal(preflightPosts.length, 2);
  get('coding-confirm').checked = true;
  get('coding-confirm').dispatch('change');
  authorizationMode = 'network';
  await get('coding-authorize').dispatch('click');
  assert.match(get('coding-error').textContent, /同一授权重试/);
  const authorizationKey = authorizationPosts[0].idempotency_key;
  authorizationMode = 'ok';
  await get('coding-authorize').dispatch('click');
  assert.equal(authorizationPosts[1].idempotency_key, authorizationKey);
  assert.equal(get('coding-result').hidden, false);
  assert.equal(get('coding-task-link').href, '/ui/?task=task-123');
  assert.match(get('coding-result-text').textContent, /不代表代码已完成/);

  messages.push({sequence: 2, message: {message_id: 'agent-1', room_id: roomId,
    sender_id: 'planner', content: '白金：先确定边界。', reply_to: 'human-1',
    created_at: '2026-09-30T12:01:00Z'}, deliveries: []});
  turns[0].status = 'succeeded';
  EventSource.instances[0].emit('chat_changed');
  await tick();
  assert.equal(get('message-list').children.length, 2);
  assert.equal(get('send-status').textContent, 'Agent 已回复，可以继续讨论。');
  assert.match(get('message-list').children[1].children[1].children[2].textContent, /白金/);
  get('message-list').children[1].children[1].children.at(-1).dispatch('click');
  assert.equal(get('reply-preview').hidden, false);
  get('message-content').value = '再解释一下';
  await get('message-form').dispatch('submit', {preventDefault() {}});
  assert.equal(posts[1].reply_to, 'agent-1');

  get('message-list').children[0].children[1].children.at(-1).dispatch('click');
  assert.equal(get('context-preview').hidden, false);
  assert.equal(get('reply-preview').hidden, true);
  get('message-content').value = '@月见 接着最初的目标讨论';
  await get('message-form').dispatch('submit', {preventDefault() {}});
  assert.equal(posts[2].context_anchor_id, 'human-1');
  assert.equal(posts[2].reply_to, null);
  assert.equal(get('context-preview').hidden, true);
  assert.match(get('message-list').children.at(-1).children[1].children[1].textContent, /背景/);

  get('message-content').value = '@月见 请补充测试边界';
  postMode = 'network';
  await get('message-form').dispatch('submit', {preventDefault() {}});
  assert.match(get('send-error').textContent, /网络未确认/);
  const retryKey = posts.at(-1).idempotency_key;
  postMode = 'ok';
  await get('message-form').dispatch('submit', {preventDefault() {}});
  assert.equal(posts.at(-1).idempotency_key, retryKey);
  assert.equal(get('message-content').value, '');

  await get('turn-list').children[0].children.at(-1).dispatch('click');
  assert.equal(turns.at(-1).status, 'cancelled');
  assert.match(get('turn-list').children[0].children[0].children[1].textContent, /已取消/);

  turns.forEach((turn) => { turn.status = 'succeeded'; });
  turns.at(-1).status = 'failed';
  turns.at(-1).error = 'Agent process ended: failed, exit=1';
  await window.poll();
  assert.equal(get('room-status').textContent, '失败');
  assert.match(get('turn-list').children[0].children[2].textContent, /exit=1/);

  let releaseOld;
  let oldStarted;
  const started = new Promise((resolve) => { oldStarted = resolve; });
  turns.at(-1).status = 'running';
  turns.at(-1).error = null;
  holdNextTurns = {started: oldStarted, wait: new Promise((resolve) => { releaseOld = resolve; })};
  const staleRefresh = window.poll();
  await started;
  turns.at(-1).status = 'succeeded';
  EventSource.instances[0].emit('chat_changed');
  await tick();
  assert.equal(get('room-status').textContent, '已回复');
  releaseOld();
  await staleRefresh;
  assert.equal(get('room-status').textContent, '已回复', 'old poll must not overwrite SSE state');

  EventSource.instances[0].emit('error');
  messages.push({sequence: messages.length + 1, message: {message_id: 'fallback-agent',
    room_id: roomId, sender_id: 'reviewer', content: '轮询也能看到新消息', reply_to: 'human-1',
    created_at: '2026-09-30T12:03:00Z'}, deliveries: []});
  const beforeFallback = get('message-list').children.length;
  await window.poll();
  assert.equal(get('message-list').children.length, beforeFallback + 1);

  const count = get('message-list').children.length;
  await window.poll();
  assert.equal(get('message-list').children.length, count);
  assert.equal(get('room-list').children.length, 1);

  rooms.push({...rooms[0], room_id: 'room-2', title: '另一个房间'});
  await get('refresh-rooms').dispatch('click');
  await get('room-list').children[1].dispatch('click');
  assert.equal(EventSource.instances[0].closed, true);
  assert.match(EventSource.instances[1].url, /room-2\/events$/);
  assert.equal(get('conversation-title').textContent, '另一个房间');
  assert.match(get('message-list').children[0].textContent, /还没有消息/);
  await get('room-list').children[0].dispatch('click');
  assert.equal(get('conversation-title').textContent, '输入校验讨论');
  assert.equal(get('message-list').children.length, count);
})().catch((error) => { console.error(error); process.exitCode = 1; });
