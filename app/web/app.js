const state = { tasks: [], nextOffset: null, selectedId: null, filter: 'all', messageCursor: 0, messageHasMore: false, requestId: 0, eventSource: null, refreshTimer: null, refreshingFor: null, refreshQueuedFor: null };
const $ = (id) => document.getElementById(id);
const api = async (path) => {
  const response = await fetch(`/api/v1${path}`, { headers: { Accept: 'application/json' } });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message || `HTTP ${response.status}`);
  return data;
};
const time = (value) => value ? new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value)) : '—';
const short = (value) => value ? value.slice(0, 8) : '—';
const stateNames = { completed: '已完成', failed: '失败', cancelled: '已取消', needs_human: '待人工处理', created: '已创建', planning: '规划中', implementing: '实现中', verifying: '验证中', reviewing: '评审中', reworking: '返工中' };
const terminal = new Set(['completed', 'failed', 'cancelled', 'needs_human']);
const statusNode = (taskState) => {
  const span = document.createElement('span');
  span.className = `status status-${taskState}`;
  span.textContent = stateNames[taskState] || taskState.replaceAll('_', ' ');
  return span;
};
const node = (tag, className, value) => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (value !== undefined) element.textContent = value;
  return element;
};
const notice = (message) => { $('notice').textContent = message; $('notice').hidden = !message; };

function renderTasks() {
  const list = $('task-list');
  list.replaceChildren();
  const tasks = state.tasks.filter((task) => state.filter === 'all' || (state.filter === 'terminal') === terminal.has(task.state));
  $('task-count').textContent = String(state.tasks.length);
  if (!tasks.length) list.append(node('div', 'empty-state', state.tasks.length ? '此筛选下暂无任务' : '暂无任务。可通过任务 API 创建。'));
  for (const task of tasks) {
    const card = node('button', `task-card${task.task_id === state.selectedId ? ' selected' : ''}`);
    card.type = 'button';
    const top = node('div', 'task-card-top');
    top.append(node('span', '', `#${short(task.task_id).toUpperCase()}`), node('span', '', time(task.created_at)));
    const bottom = node('div', 'task-card-bottom');
    bottom.append(statusNode(task.state), node('span', '', `↺ ${task.rework_rounds} 轮返工`));
    card.append(top, node('div', 'task-card-title', task.issue), bottom);
    card.addEventListener('click', () => selectTask(task.task_id));
    list.append(card);
  }
  $('load-more').hidden = state.nextOffset === null;
}

function showTask(task) {
  $('detail-short-id').textContent = `#${short(task.task_id).toUpperCase()}`;
  $('detail-status').replaceChildren(statusNode(task.state));
  $('detail-title').textContent = task.issue.split('\n')[0];
  $('detail-issue').textContent = task.issue;
  $('detail-trace').textContent = short(task.trace_id);
  $('detail-reworks').textContent = String(task.rework_rounds);
  $('detail-created').textContent = time(task.created_at);
  const index = state.tasks.findIndex((item) => item.task_id === task.task_id);
  if (index !== -1) state.tasks[index] = task;
  renderTasks();
}

function closeStream() {
  if (state.refreshTimer !== null) clearTimeout(state.refreshTimer);
  state.refreshTimer = null;
  if (state.eventSource !== null) state.eventSource.close();
  state.eventSource = null;
}

async function refreshSelected(taskId, requestId) {
  if (state.refreshingFor === requestId) { state.refreshQueuedFor = requestId; return; }
  state.refreshingFor = requestId;
  try {
    const task = await api(`/tasks/${encodeURIComponent(taskId)}`);
    if (requestId !== state.requestId) return;
    showTask(task);
    const [plans] = await Promise.all([
      api(`/tasks/${encodeURIComponent(taskId)}/plans`),
      loadMessages(taskId, true, requestId),
    ]);
    if (requestId !== state.requestId) return;
    renderPlans(plans.items, taskId);
    notice('');
    if (terminal.has(task.state)) {
      closeStream();
      $('live-status').textContent = '任务已结束 · 显示最终记录';
    }
  } catch (error) {
    if (requestId === state.requestId) notice(`实时数据刷新失败：${error.message}`);
  } finally {
    if (state.refreshingFor === requestId) state.refreshingFor = null;
    if (state.refreshQueuedFor === requestId) {
      state.refreshQueuedFor = null;
      if (requestId === state.requestId) scheduleRefresh(taskId, requestId);
    }
  }
}

function scheduleRefresh(taskId, requestId) {
  if (requestId !== state.requestId) return;
  if (state.refreshTimer !== null) clearTimeout(state.refreshTimer);
  state.refreshTimer = setTimeout(() => {
    state.refreshTimer = null;
    void refreshSelected(taskId, requestId);
  }, 200);
}

function followTask(taskId, requestId) {
  if (typeof EventSource === 'undefined') {
    $('live-status').textContent = '浏览器不支持实时连接 · 可手动刷新';
    return;
  }
  const source = new EventSource(`/api/v1/tasks/${encodeURIComponent(taskId)}/events`);
  state.eventSource = source;
  $('live-status').textContent = '正在连接实时事件…';
  source.onopen = () => {
    if (requestId === state.requestId) $('live-status').textContent = '实时连接中 · 自动更新';
  };
  for (const type of ['chat_message_persisted', 'workflow_decision', 'task_state_changed', 'agent_turn_started', 'agent_turn_completed', 'agent_turn_failed', 'verification_completed', 'review_decided', 'completion_decided', 'recovery_decided', 'budget_exceeded', 'human_input_requested', 'system_error']) {
    source.addEventListener(type, () => scheduleRefresh(taskId, requestId));
  }
  source.onerror = () => {
    if (requestId !== state.requestId || state.eventSource !== source) return;
    $('live-status').textContent = '连接中断 · 正在重连';
    scheduleRefresh(taskId, requestId);
  };
}

async function loadTasks(more = false) {
  try {
    const page = await api(`/tasks?limit=30&offset=${more ? state.nextOffset : 0}`);
    state.tasks = more ? [...state.tasks, ...page.items] : page.items;
    state.nextOffset = page.next_offset;
    renderTasks();
    notice('');
    if (!state.selectedId && state.tasks.length) await selectTask(state.tasks[0].task_id);
  } catch (error) {
    notice(`任务列表读取失败：${error.message}`);
    if (!more) $('task-list').replaceChildren(node('div', 'empty-state', '暂时无法获取任务'));
  }
}

function renderMessage(message, taskId) {
  const item = node('article', 'message');
  const head = node('div', 'message-head');
  const role = message.sender_role;
  head.append(node('span', `avatar ${['planner', 'implementer', 'reviewer'].includes(role) ? role : 'system'}`, message.sender_name.slice(0, 1)), node('span', 'message-name', message.sender_name), node('span', 'message-role', role), node('time', 'message-time', time(message.created_at)));
  item.append(head, node('span', 'message-type', message.type.replaceAll('_', ' ')), node('p', 'message-content', message.content));
  if (message.artifacts.length) {
    const links = node('div', 'artifact-links');
    for (const artifact of message.artifacts) {
      const button = node('button', 'artifact-link', `↗ ${artifact.type} · ${artifact.summary}`);
      button.type = 'button';
      button.addEventListener('click', () => loadArtifact(taskId, artifact.artifact_id));
      links.append(button);
    }
    item.append(links);
  }
  return item;
}

async function loadMessages(taskId, more = false, requestId = state.requestId) {
  try {
    const page = await api(`/tasks/${encodeURIComponent(taskId)}/messages?limit=50&after_sequence=${more ? state.messageCursor : 0}`);
    if (requestId !== state.requestId) return;
    if (!more) $('message-list').replaceChildren();
    const messages = more ? page.items.filter((message) => message.sequence > state.messageCursor) : page.items;
    if (messages.length && more && $('message-list').querySelector('.empty-state')) $('message-list').replaceChildren();
    for (const message of messages) $('message-list').append(renderMessage(message, taskId));
    if (!page.items.length && !more) $('message-list').append(node('div', 'empty-state', '暂无对话'));
    if (messages.length) state.messageCursor = messages.at(-1).sequence;
    state.messageHasMore = page.next_after_sequence !== null;
    $('load-messages').hidden = !state.messageHasMore;
  } catch (error) { if (requestId === state.requestId) notice(`对话读取失败：${error.message}`); }
}

function renderPlans(plans, taskId) {
  const list = $('plan-list');
  list.replaceChildren();
  if (!plans.length) list.append(node('div', 'empty-state', '暂无方案版本'));
  for (const plan of plans) {
    const card = node('article', 'plan-card');
    card.append(node('h3', '', `方案版本 v${plan.version}`), node('p', '', `${time(plan.created_at)} · ${short(plan.artifact_id)}`));
    const button = node('button', 'plan-open', '查看方案证据 ↗');
    button.type = 'button';
    button.addEventListener('click', () => loadArtifact(taskId, plan.artifact_id));
    card.append(button);
    list.append(card);
  }
}

async function loadArtifact(taskId, artifactId) {
  try {
    const artifact = await api(`/tasks/${encodeURIComponent(taskId)}/artifacts/${encodeURIComponent(artifactId)}`);
    if (taskId !== state.selectedId) return;
    $('artifact-placeholder').hidden = true;
    $('artifact-detail').hidden = false;
    $('artifact-type').textContent = artifact.metadata.type;
    $('artifact-size').textContent = `${artifact.metadata.size_bytes.toLocaleString()} B`;
    $('artifact-name').textContent = artifact.metadata.filename || `Artifact ${short(artifactId)}`;
    $('artifact-hash').textContent = artifact.metadata.sha256;
    $('artifact-preview').hidden = artifact.preview === null;
    $('artifact-preview').textContent = artifact.preview || '';
    $('artifact-unavailable').hidden = artifact.preview_unavailable_reason === null;
    $('artifact-unavailable').textContent = `此证据不支持在线预览（${artifact.preview_unavailable_reason || ''}）。`;
    notice('');
  } catch (error) { notice(`证据读取失败：${error.message}`); }
}

async function selectTask(taskId) {
  closeStream();
  state.selectedId = taskId;
  state.requestId += 1;
  const requestId = state.requestId;
  state.messageCursor = 0;
  renderTasks();
  $('empty-detail').hidden = true;
  $('task-detail').hidden = false;
  $('artifact-placeholder').hidden = false;
  $('artifact-detail').hidden = true;
  $('message-list').replaceChildren(node('div', 'empty-state', '正在加载对话…'));
  $('plan-list').replaceChildren(node('div', 'empty-state', '正在加载方案…'));
  try {
    const [task, room, plans] = await Promise.all([
      api(`/tasks/${encodeURIComponent(taskId)}`),
      api(`/tasks/${encodeURIComponent(taskId)}/room`),
      api(`/tasks/${encodeURIComponent(taskId)}/plans`),
    ]);
    if (requestId !== state.requestId) return;
    showTask(task);
    $('room-members').replaceChildren(...room.room.members.filter((member) => member.kind === 'agent').map((member) => {
      const chip = node('span', 'member-chip');
      chip.append(node('b', '', member.name), node('span', '', member.role));
      return chip;
    }));
    renderPlans(plans.items, taskId);
    await loadMessages(taskId, false, requestId);
    notice('');
    if (terminal.has(task.state)) $('live-status').textContent = '任务已结束 · 显示最终记录';
    else followTask(taskId, requestId);
  } catch (error) { if (requestId === state.requestId) notice(`任务详情读取失败：${error.message}`); }
}

document.querySelectorAll('.filter').forEach((button) => button.addEventListener('click', () => {
  document.querySelectorAll('.filter').forEach((item) => item.classList.toggle('active', item === button));
  state.filter = button.dataset.filter;
  renderTasks();
}));
document.querySelectorAll('.tab').forEach((button) => button.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach((item) => {
    item.classList.toggle('active', item === button);
    item.setAttribute('aria-selected', String(item === button));
  });
  $('room-pane').hidden = button.dataset.tab !== 'room';
  $('plans-pane').hidden = button.dataset.tab !== 'plans';
}));
$('refresh-button').addEventListener('click', async () => { await loadTasks(); if (state.selectedId) await selectTask(state.selectedId); });
$('load-more').addEventListener('click', () => loadTasks(true));
$('load-messages').addEventListener('click', () => loadMessages(state.selectedId, true));
$('clock').textContent = new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
window.addEventListener('beforeunload', closeStream);
loadTasks();
