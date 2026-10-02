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
    this.attributes = new Map();
    this.listeners = new Map();
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(type, callback) { this.listeners.set(type, callback); }
  dispatch(type, event = {}) { return this.listeners.get(type)?.(event); }
  setAttribute(name, value) { this.attributes.set(name, value); }
  getAttribute(name) { return this.attributes.get(name); }
  get classList() { return {
    contains: (value) => this.className.split(' ').includes(value),
    toggle: (value, force) => {
      const classes = new Set(this.className.split(' ').filter(Boolean));
      if (force) classes.add(value); else classes.delete(value);
      this.className = [...classes].join(' ');
    },
  }; }
  querySelector(selector) { return this.children.find((child) => child.className === selector.slice(1)) || null; }
  focus() { document.activeElement = this; }
}

const elements = new Map();
const get = (id) => {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
};
get('empty-detail').append(new Element('h2'), new Element('p'));
get('human-recipient').value = 'planner';
get('control-role').value = 'planner';
const filters = ['all', 'active', 'terminal'].map((value) => {
  const element = new Element('button');
  element.className = 'filter';
  element.dataset.filter = value;
  return element;
});
const tabs = ['room', 'plans'].map((value) => {
  const element = get(`tab-${value}`);
  element.className = 'tab';
  element.dataset.tab = value;
  return element;
});
const document = {
  getElementById: get,
  createElement(tag) { return new Element(tag); },
  querySelectorAll(selector) { return selector === '.filter' ? filters : selector === '.tab' ? tabs : []; },
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
const delivery = { task_id: 'task-a', trace_id: 'trace-a', task_state: 'needs_human', task_revision: 3,
  verification: null, review: null, completion: null, patch: null, delivery_ready: false };
let nextUuid = 1;
let mode = 'normal';
let releasePreflight = null;
let confirmResult = true;
let confirmations = 0;
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
    { member_id: 'reviewer-id', role: 'reviewer', kind: 'agent', name: '鲸鲸' },
    { member_id: 'orchestrator-id', role: 'orchestrator', kind: 'system' },
  ] } });
  if (url.endsWith('/plans')) return response(200, { items: [] });
  if (url.includes('/messages?')) {
    const cursor = Number(new URL(url, 'http://local').searchParams.get('after_sequence'));
    return response(200, { items: messages.filter((item) => item.sequence > cursor), next_after_sequence: null });
  }
  if (url.endsWith('/control')) return mode === 'control-error'
    ? response(503, { error: { code: 'unavailable', message: 'control offline' } }) : response(200, control);
  if (url.endsWith('/delivery')) return mode === 'delivery-error'
    ? response(500, { error: { code: 'task_artifact_integrity_error', message: 'invalid evidence' } })
    : response(200, delivery);
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
  window: { addEventListener() {}, confirm() { confirmations++; return confirmResult; } }, console,
  setTimeout() { return 1; }, clearTimeout() {},
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/web/avatars.js'), 'utf8'), context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/web/app.js'), 'utf8'), context);
const tick = async () => { for (let i = 0; i < 8; i++) await new Promise((resolve) => setImmediate(resolve)); };
const findClass = (element, className) => element?.className.split(' ').includes(className) ? element
  : element?.children.map((child) => findClass(child, className)).find(Boolean);
const inlineButton = (index = 0) => findClass(get('message-list').children[index], 'message-continue-button');
const inlineStatus = (index = 0) => findClass(get('message-list').children[index], 'message-continuation-status');

(async () => {
  await tick();
  assert.match(get('control-budget').children[0].textContent, /回合 1\/30/);
  assert.equal(get('overview-phase').textContent, '待人工处理');
  assert.equal(get('overview-agent').textContent, '等待人工');
  assert.match(get('overview-agent-note').textContent, /不证明 Agent 正在运行/);
  assert.match(get('overview-blocking').textContent, /等待人工显式预检并继续/);
  assert.match(get('overview-recent').textContent, /任务已创建/);
  assert.match(get('delivery-status').textContent, /尚未形成可交付结论/);
  assert.equal(get('delivery-patch-download').hidden, true);
  assert.equal(get('continue-workflow').disabled, false);
  assert.equal(inlineButton().disabled, false);
  assert.equal(get('control-message').value, 'human-1');
  messages.push({ ...human('reviewer-only', 2), recipient_ids: ['reviewer-id'] });
  await vm.runInContext('loadMessages("task-a")', context);
  assert.equal(inlineButton(1), undefined);
  messages.pop();
  messages.push({ ...human('route-both', 2), recipient_ids: ['orchestrator-id'] });
  await vm.runInContext('loadMessages("task-a")', context);
  const targetSelect = findClass(get('message-list').children[1], 'message-continuation-target');
  assert.deepEqual(targetSelect.children.map((option) => option.value), ['planner', 'implementer']);
  messages.pop();
  await vm.runInContext('loadMessages("task-a")', context);
  vm.runInContext('state.control = null; renderMessages("task-a")', context);
  assert.equal(inlineButton().disabled, true);
  assert.match(inlineStatus().textContent, /尚未就绪/);
  assert.match(get('overview-blocking').textContent, /正在读取控制状态/);
  await vm.runInContext('loadControl("task-a")', context);
  control.task_revision = 2;
  vm.runInContext('renderControl()', context);
  assert.match(get('overview-blocking').textContent, /修订不一致/);
  control.task_revision = 3;
  vm.runInContext('renderControl()', context);
  mode = 'control-error';
  await vm.runInContext('loadControl("task-a")', context);
  assert.match(get('overview-blocking').textContent, /旧快照不可用于判断或继续/);
  assert.match(get('overview-recent').textContent, /上次快照/);
  assert.equal(get('continue-workflow').disabled, true);
  assert.equal(inlineButton().disabled, true);
  assert.equal(get('human-composer').hidden, false);
  mode = 'normal';
  await vm.runInContext('loadControl("task-a")', context);
  assert.equal(get('control-error').hidden, true);
  assert.equal(get('human-composer').hidden, false);
  control.budget_violation = { code: 'agent_turns', actual: 30, limit: 30, detail: 'blocked' };
  await vm.runInContext('loadControl("task-a")', context);
  assert.equal(get('continue-workflow').disabled, true);
  assert.equal(inlineButton().disabled, true);
  assert.match(inlineStatus().textContent, /预算阻塞/);
  assert.match(get('control-blocker').textContent, /预算阻塞/);
  assert.match(get('overview-blocking').textContent, /预算阻塞/);
  control.budget_violation = null;
  await vm.runInContext('loadControl("task-a")', context);

  confirmResult = false;
  inlineButton().dispatch('click');
  await tick();
  assert.equal(confirmations, 1);
  assert.equal(preflights.length, 0);
  confirmResult = true;

  mode = 'preflight-conflict';
  inlineButton().dispatch('click');
  await tick();
  assert.equal(preflights.length, 1);
  assert.equal(admissions.length, 0);
  assert.match(get('control-error').textContent, /不会自动重发/);
  assert.match(inlineStatus().textContent, /不会自动重发/);
  mode = 'preflight-pending';
  inlineButton().dispatch('click');
  await tick();
  await vm.runInContext('selectTask("task-a")', context);
  releasePreflight();
  await tick();
  assert.equal(admissions.length, 0);
  mode = 'normal';
  inlineButton().dispatch('click');
  await tick();
  assert.equal(admissions.length, 1);
  assert.equal(admissions[0].message_id, 'human-1');
  assert.equal(admissions[0].target_role, 'planner');
  assert.equal(authorizations.length, 0);
  assert.equal(get('continue-workflow').disabled, true);
  assert.equal(get('cancel-continuation').hidden, false);
  assert.match(get('overview-agent-note').textContent, /本次继续入口目标：白金 · Planner/);
  assert.match(get('overview-agent-note').textContent, /不含实时执行者/);
  assert.match(get('overview-recent').textContent, /继续请求.*已受理/);
  assert.match(get('overview-blocking').textContent, /执行占用尚未解除/);

  get('control-reason').value = '用户请求取消';
  await vm.runInContext('cancelContinuation()', context);
  assert.equal(cancellations.length, 1);
  assert.equal(cancellations[0].expected_revision, 3);
  assert.equal(cancellations[0].expected_runtime_revision, 2);
  assert.equal(cancellations[0].expected_claim_updated_at, '2026-09-28T01:00:00Z');
  assert.equal(get('cancel-continuation').hidden, true);
  assert.match(get('control-summary').textContent, /不证明全部外部进程停止/);
  assert.match(get('overview-recent').textContent, /取消请求.*不证明外部进程已停止/);

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
  assert.equal(inlineButton(1).disabled, true);
  const inlineReason = findClass(get('message-list').children[1], 'message-continuation-reason');
  inlineReason.value = '新需求已确认，批准再次继续';
  inlineReason.dispatch('input');
  assert.equal(inlineButton(1).disabled, false);
  inlineButton(1).dispatch('click');
  await tick();
  assert.equal(authorizations.length, 1);
  assert.equal(admissions.length, 2);
  assert.equal(authorizations[0].message_id, 'human-2');
  assert.equal(authorizations[0].idempotency_key, admissions[1].idempotency_key);
  assert.equal(admissions[1].authorization_id, 'authorization-1');
  assert.equal(get('continue-workflow').disabled, true);
  control.latest_continuation.receipt.state = 'succeeded';
  control.latest_workflow_outcome = { request_id: 'continuation-2', success: true, reason: null };
  task.state = 'completed';
  control.task_state = 'completed';
  vm.runInContext('renderControl()', context);
  assert.equal(get('overview-phase').textContent, '已完成');
  assert.equal(get('overview-agent').textContent, '—');
  assert.match(get('overview-recent').textContent, /完成守卫通过/);
  task.state = 'reviewing';
  control.task_state = 'reviewing';
  control.latest_continuation = null;
  control.latest_workflow_outcome = null;
  vm.runInContext('renderControl()', context);
  assert.equal(get('overview-agent').textContent, '鲸鲸 · Reviewer');
  assert.match(get('overview-blocking').textContent, /快照未报告已知阻塞/);

  mode = 'delivery-error';
  await vm.runInContext('loadDelivery("task-a")', context);
  assert.match(get('delivery-status').textContent, /证据读取失败/);
  assert.equal(get('delivery-content').hidden, true);
  assert.equal(get('delivery-patch-download').hidden, true);
  mode = 'normal';
  task.state = 'completed';
  delivery.task_state = 'completed';
  delivery.verification = { artifact: { artifact_id: 'verify-id' }, passed: true,
    changed_files: ['src/app.py'], checks: [
      { kind: 'build', name: 'compile', status: 'passed', detail: 'compile passed' },
      { kind: 'public_tests', name: 'pytest', status: 'passed', detail: 'tests passed' },
      { kind: 'hidden_tests', name: 'hidden_tests', status: 'passed', detail: null },
    ] };
  delivery.review = { artifact: { artifact_id: 'review-id' }, verdict: 'approved',
    summary: '证据充分', issues: [], follows_latest_verification: true };
  delivery.completion = { artifact: { artifact_id: 'guard-id' }, passed: true,
    conditions: [{ kind: 'valid_diff', passed: true, detail: 'valid patch' }] };
  delivery.patch = { artifact_id: 'patch-id', sha256: 'a'.repeat(64) };
  delivery.delivery_ready = true;
  await vm.runInContext('loadDelivery("task-a")', context);
  assert.match(get('delivery-status').textContent, /交付就绪/);
  assert.equal(get('delivery-content').hidden, false);
  assert.equal(get('delivery-patch-download').hidden, false);
  assert.match(get('delivery-patch-download').href, /task-a\/delivery\/patch\/patch-id$/);
  assert.match(get('delivery-diff').children[0].textContent, /1 个变更文件/);
  assert.match(get('delivery-checks').children[0].textContent, /Verifier 总结：通过/);
  assert.match(get('delivery-guard').children[0].textContent, /CompletionGuard：通过/);
  delivery.task_revision = 4;
  await vm.runInContext('loadDelivery("task-a")', context);
  assert.match(get('delivery-status').textContent, /修订不一致/);
  assert.equal(get('delivery-patch-download').hidden, true);

  let prevented = 0;
  tabs[0].dispatch('keydown', { key: 'ArrowRight', preventDefault() { prevented++; } });
  assert.equal(prevented, 1);
  assert.equal(get('plans-pane').hidden, false);
  assert.equal(get('room-pane').hidden, true);
  assert.equal(tabs[1].getAttribute('aria-selected'), 'true');
  assert.equal(tabs[0].tabIndex, -1);
  assert.equal(document.activeElement, tabs[1]);
  tabs[1].dispatch('keydown', { key: 'Home', preventDefault() { prevented++; } });
  assert.equal(get('room-pane').hidden, false);
  assert.equal(tabs[0].tabIndex, 0);
  assert.equal(document.activeElement, tabs[0]);
  tabs[0].dispatch('keydown', { key: 'End', preventDefault() { prevented++; } });
  assert.equal(document.activeElement, tabs[1]);
  filters[2].dispatch('click');
  assert.equal(filters[2].getAttribute('aria-pressed'), 'true');
  assert.equal(filters[0].getAttribute('aria-pressed'), 'false');
  filters[0].dispatch('click');
  const focusedCard = get('task-list').children[0];
  assert.equal(focusedCard.getAttribute('aria-current'), 'true');
  focusedCard.focus();
  vm.runInContext('renderTasks()', context);
  assert.equal(document.activeElement, get('task-list').children[0]);
})().catch((error) => { console.error(error); process.exitCode = 1; });
