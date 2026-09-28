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
  querySelector(selector) { return this.children.find((child) => child.className === selector.slice(1)) || null; }
  focus() {}
}

const elements = new Map();
const get = (id) => {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
};
get('empty-detail').append(new Element('h2'), new Element('p'));
get('human-recipient').value = 'planner';
get('control-role').value = 'planner';
const document = {
  getElementById: get,
  createElement(tag) { return new Element(tag); },
  querySelectorAll() { return []; },
};
const task = { task_id: 'task-a', trace_id: 'trace-a', issue: '实现一个安全变更',
  repository_path: '/tmp/fixture', state: 'needs_human', revision: 3,
  rework_rounds: 0, created_at: '2026-09-28T00:00:00Z' };
const human = (id, sequence) => ({ sequence, message_id: id, sender_role: 'human', sender_name: 'Human',
  type: 'message', content: `人工意图 ${id}`, created_at: '2026-09-28T00:00:00Z',
  recipient_ids: ['planner-id'], pending_for_human: false, pending_for_continuation: true,
  reply_to: null, artifacts: [] });
const messages = [human('human-1', 1)];
const policy = { max_agent_turns: 30, max_reported_tokens: 500000, max_agent_duration_ms: 7200000,
  max_room_messages: 200, max_repeated_messages: 3, max_questions_without_progress: 4 };
const usage = { agent_turns: 1, reported_input_tokens: 10, reported_output_tokens: 5,
  reported_total_tokens: 15, turns_without_token_usage: 0, agent_duration_ms: 1000, room_messages: 2 };
const control = { task_id: 'task-a', task_state: 'needs_human', task_revision: 3, runtime_revision: 2,
  rework_rounds: 0, max_rework_rounds: 2, budget_policy: policy, budget_usage: usage,
  budget_violation: null, latest_continuation: null, latest_workflow_outcome: null, latest_cancellation: null };
let nextUuid = 1;
let mode = 'normal';
let releasePreflight = null;
const preflights = [];
const authorizations = [];
const admissions = [];
const cancellations = [];
const response = (status, data) => ({ ok: status >= 200 && status < 300, status, json: async () => data });
const fetch = async (url, options = {}) => {
  if (url.startsWith('/api/v1/tasks?')) return response(200, { items: [task], next_offset: null });
  if (url === '/api/v1/tasks/task-a') return response(200, task);
  if (url.endsWith('/room')) return response(200, { room: { members: [
    { member_id: 'human-id', role: 'human', kind: 'human' },
    { member_id: 'planner-id', role: 'planner', kind: 'agent', name: '白金' },
    { member_id: 'implementer-id', role: 'implementer', kind: 'agent', name: '月见' },
  ] } });
  if (url.endsWith('/plans')) return response(200, { items: [] });
  if (url.includes('/messages?')) {
    const cursor = Number(new URL(url, 'http://local').searchParams.get('after_sequence'));
    return response(200, { items: messages.filter((item) => item.sequence > cursor), next_after_sequence: null });
  }
  if (url.endsWith('/control')) return response(200, control);
  if (url.endsWith('/continue/preflight')) {
    const body = JSON.parse(options.body);
    preflights.push(body);
    if (mode === 'preflight-conflict') return response(409, { error: { code: 'task_state_conflict', message: 'budget blocked' } });
    if (mode === 'preflight-pending') return new Promise((resolve) => {
      releasePreflight = () => resolve(response(200, { checks_passed: true, execution_ready: false,
        task_id: 'task-a', task_revision: body.expected_revision,
        message_id: body.message_id, target_role: body.target_role }));
    });
    return response(200, { checks_passed: true, execution_ready: false, task_id: 'task-a',
      task_revision: body.expected_revision, message_id: body.message_id, target_role: body.target_role });
  }
  if (url.endsWith('/authorize')) {
    const body = JSON.parse(options.body);
    authorizations.push(body);
    return response(200, { authorization_id: 'authorization-1', intent: {
      message_id: body.message_id, target_role: body.target_role, idempotency_key: body.idempotency_key,
    } });
  }
  if (url.endsWith('/continue/workflow')) {
    const body = JSON.parse(options.body);
    admissions.push(body);
    const latest = { receipt: { scope: 'controlled-workflow-continuation', state: 'claimed',
      request: { request_id: `continuation-${admissions.length}`, task_id: 'task-a',
        message_id: body.message_id, target_role: body.target_role, idempotency_key: body.idempotency_key } },
    updated_at: '2026-09-28T01:00:00Z', task_revision: 3, runtime_revision: 2, task_state: 'needs_human' };
    control.latest_continuation = latest;
    return response(202, latest);
  }
  if (url.endsWith('/cancel')) {
    const body = JSON.parse(options.body);
    cancellations.push(body);
    control.latest_cancellation = { request_id: control.latest_continuation.receipt.request.request_id,
      state: 'requested', claim_released: false, external_process_stopped_confirmed: false };
    return response(202, control.latest_cancellation);
  }
  if (url.includes('/continuations/')) return response(200, control.latest_continuation);
  throw new Error(`unexpected fetch ${url}`);
};
const context = vm.createContext({ document, fetch, Intl, Date, URL, encodeURIComponent,
  crypto: { randomUUID: () => `uuid-${nextUuid++}` },
  window: { addEventListener() {}, confirm() { return true; } }, console,
  setTimeout() { return 1; }, clearTimeout() {},
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/web/app.js'), 'utf8'), context);
const tick = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setImmediate(resolve)); };

(async () => {
  await tick();
  assert.match(get('control-budget').children[0].textContent, /回合 1\/30/);
  assert.equal(get('continue-workflow').disabled, false);
  assert.equal(get('control-message').value, 'human-1');
  control.budget_violation = { code: 'agent_turns', actual: 30, limit: 30, detail: 'blocked' };
  await vm.runInContext('loadControl("task-a")', context);
  assert.equal(get('continue-workflow').disabled, true);
  assert.match(get('control-blocker').textContent, /预算阻塞/);
  control.budget_violation = null;
  await vm.runInContext('loadControl("task-a")', context);

  mode = 'preflight-conflict';
  await vm.runInContext('continueWorkflow()', context);
  assert.equal(preflights.length, 1);
  assert.equal(admissions.length, 0);
  assert.match(get('control-error').textContent, /不会自动重发/);
  mode = 'preflight-pending';
  const stale = vm.runInContext('continueWorkflow()', context);
  await tick();
  await vm.runInContext('selectTask("task-a")', context);
  releasePreflight();
  await stale;
  assert.equal(admissions.length, 0);
  mode = 'normal';
  await vm.runInContext('continueWorkflow()', context);
  assert.equal(admissions.length, 1);
  assert.equal(admissions[0].message_id, 'human-1');
  assert.equal(admissions[0].target_role, 'planner');
  assert.equal(authorizations.length, 0);
  assert.equal(get('continue-workflow').disabled, true);
  assert.equal(get('cancel-continuation').hidden, false);

  get('control-reason').value = '用户请求取消';
  await vm.runInContext('cancelContinuation()', context);
  assert.equal(cancellations.length, 1);
  assert.equal(cancellations[0].expected_revision, 3);
  assert.equal(cancellations[0].expected_runtime_revision, 2);
  assert.equal(cancellations[0].expected_claim_updated_at, '2026-09-28T01:00:00Z');
  assert.equal(get('cancel-continuation').hidden, true);
  assert.match(get('control-summary').textContent, /不证明全部外部进程停止/);

  messages[0].pending_for_continuation = false;
  messages.push(human('human-2', 2));
  control.latest_cancellation = null;
  control.latest_continuation = { ...control.latest_continuation,
    receipt: { ...control.latest_continuation.receipt, state: 'succeeded' },
    updated_at: '2026-09-28T02:00:00Z' };
  await vm.runInContext('loadMessages("task-a")', context);
  await vm.runInContext('loadControl("task-a")', context);
  assert.equal(get('control-message').value, 'human-2');
  get('control-reason').value = '';
  vm.runInContext('renderControl()', context);
  assert.equal(get('continue-workflow').disabled, true);
  get('control-reason').value = '新需求已确认，批准再次继续';
  vm.runInContext('renderControl()', context);
  await vm.runInContext('continueWorkflow()', context);
  assert.equal(authorizations.length, 1);
  assert.equal(admissions.length, 2);
  assert.equal(authorizations[0].message_id, 'human-2');
  assert.equal(authorizations[0].idempotency_key, admissions[1].idempotency_key);
  assert.equal(admissions[1].authorization_id, 'authorization-1');
  assert.equal(get('continue-workflow').disabled, true);
})().catch((error) => { console.error(error); process.exitCode = 1; });
