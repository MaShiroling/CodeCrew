const state = { tasks: [], nextOffset: null, selectedId: null, selectedTask: null, filter: 'all', messageCursor: 0, messageHasMore: false, messages: [], roomMembers: [], replyTarget: null, humanAttempts: new Map(), postingHuman: false, discussionReplyTarget: null, discussionDrafts: new Map(), discussionAttempts: new Map(), postingDiscussion: false, awaitingDiscussion: null, control: null, controlLoadError: null, controlTimer: null, controlMessageId: null, controlBusy: false, controlAttempts: new Map(), cancelAttempts: new Map(), inlineRoles: new Map(), inlineReasons: new Map(), inlineFeedback: null, delivery: null, deliveryError: null, deliveryRequestId: 0, requestId: 0, eventSource: null, refreshTimer: null, refreshingFor: null, refreshQueuedFor: null, creating: false, cancelling: false };
const $ = (id) => document.getElementById(id);
const api = async (path, options = {}) => {
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    headers: { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}) },
  });
  let data;
  try { data = await response.json(); }
  catch { throw new Error('服务返回的响应无法读取'); }
  if (!response.ok) {
    const knownErrors = {
      task_service_unavailable: '任务服务尚未配置，请使用运行时配置启动服务',
      task_not_found: '任务不存在或已被移除',
      task_detail_unavailable: '任务详情暂不可用',
      task_artifact_not_found: '证据不存在或不属于当前任务',
      task_message_not_found: '回复目标不在当前任务中',
      invalid_human_message: '回复目标已失效或消息不符合要求',
      task_message_conflict: '该消息标识已用于不同内容，请核对对话记录',
      task_state_conflict: '任务状态或修订号已变化',
      invalid_repository: '仓库路径无效或无法创建独立工作区',
      validation_error: '输入不符合要求，请检查仓库路径和开发需求',
    };
    throw Object.assign(new Error(knownErrors[data?.error?.code] || data?.error?.message || `HTTP ${response.status}`), { status: response.status, code: data?.error?.code, detail: data?.error?.message });
  }
  return data;
};
const time = (value) => value ? new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value)) : '—';
const short = (value) => value ? value.slice(0, 8) : '—';
const stateNames = { completed: '已完成', failed: '失败', cancelled: '已取消', needs_human: '待人工处理', created: '已创建', planning: '规划中', implementing: '实现中', verifying: '验证中', reviewing: '评审中', rework: '返工中' };
const terminal = new Set(['completed', 'failed', 'cancelled', 'needs_human']);
const finishedStates = new Set(['completed', 'failed', 'cancelled']);
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
  const focusedCard = document.activeElement?.classList?.contains('task-card');
  let selectedCard = null;
  list.replaceChildren();
  const tasks = state.tasks.filter((task) => state.filter === 'all'
    || (state.filter === 'terminal') === finishedStates.has(task.state));
  $('task-count').textContent = String(state.tasks.length);
  if (!tasks.length) list.append(node('div', 'empty-state', state.tasks.length ? '此筛选下暂无任务' : '暂无任务。点击“新建任务”开始。'));
  for (const task of tasks) {
    const card = node('button', `task-card${task.task_id === state.selectedId ? ' selected' : ''}`);
    card.type = 'button';
    if (task.task_id === state.selectedId) {
      card.setAttribute('aria-current', 'true');
      selectedCard = card;
    }
    const top = node('div', 'task-card-top');
    top.append(node('span', '', `#${short(task.task_id).toUpperCase()}`), node('span', '', time(task.created_at)));
    const bottom = node('div', 'task-card-bottom');
    bottom.append(statusNode(task.state), node('span', '', `↺ ${task.rework_rounds} 轮返工`));
    card.append(top, node('div', 'task-card-title', task.issue), bottom);
    card.addEventListener('click', () => selectTask(task.task_id));
    list.append(card);
  }
  if (focusedCard && selectedCard) selectedCard.focus({ preventScroll: true });
  $('load-more').hidden = state.nextOffset === null;
}

function showTask(task) {
  if (state.selectedTask?.task_id === task.task_id && task.revision < state.selectedTask.revision) return;
  state.selectedTask = task;
  if (task.state !== 'needs_human') state.replyTarget = null;
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
  renderCancelAction();
  renderDiscussionComposer();
  renderHumanComposer();
  renderControl();
  renderDelivery();
  if (state.messages.length) renderMessages(task.task_id);
}

function humanError(message) {
  $('human-error').textContent = message;
  $('human-error').hidden = !message;
}

function humanStatus(message) {
  $('human-status').textContent = message;
  $('human-status').hidden = !message;
}

function discussionError(message) {
  $('discussion-error').textContent = message;
  $('discussion-error').hidden = !message;
}

function discussionStatus(message) {
  $('discussion-status').textContent = message;
  $('discussion-status').hidden = !message;
}

function renderDiscussionComposer() {
  const available = !!state.selectedTask && !finishedStates.has(state.selectedTask.state)
    && state.roomMembers.some((member) => member.role === 'human' && member.kind === 'human')
    && state.roomMembers.some((member) => ['planner', 'implementer', 'reviewer'].includes(member.role));
  $('discussion-composer').hidden = !available;
  $('discussion-submit').disabled = state.postingDiscussion || !available;
  $('discussion-submit').textContent = state.postingDiscussion ? '发送中…' : '发送讨论';
  $('discussion-reply').hidden = !state.discussionReplyTarget;
  if (state.discussionReplyTarget) {
    $('discussion-reply-summary').textContent = `讨论回复 ${state.discussionReplyTarget.sender_name}：${state.discussionReplyTarget.content.slice(0, 120)}`;
  }
  document.querySelectorAll('[data-discussion-mention]').forEach((button) => { button.disabled = !available || state.postingDiscussion; });
}

function selectDiscussionReply(message) {
  if (state.postingDiscussion || finishedStates.has(state.selectedTask?.state)
      || !['planner', 'implementer', 'reviewer'].includes(message.sender_role)) return;
  state.discussionReplyTarget = { message_id: message.message_id, sender_name: message.sender_name, content: message.content };
  discussionError('');
  discussionStatus('');
  renderDiscussionComposer();
  $('discussion-content').focus();
}

function insertDiscussionMention(mention) {
  if (state.postingDiscussion || $('discussion-composer').hidden) return;
  const input = $('discussion-content');
  const start = Number.isInteger(input.selectionStart) ? input.selectionStart : input.value.length;
  const end = Number.isInteger(input.selectionEnd) ? input.selectionEnd : start;
  const prefix = start > 0 && !/\s/.test(input.value[start - 1]) ? ' ' : '';
  const insertion = `${prefix}${mention} `;
  input.value = `${input.value.slice(0, start)}${insertion}${input.value.slice(end)}`;
  state.discussionDrafts.set(state.selectedId, input.value);
  input.focus();
  if (typeof input.setSelectionRange === 'function') input.setSelectionRange(start + insertion.length, start + insertion.length);
}

async function postDiscussionMessage() {
  const task = state.selectedTask;
  if (state.postingDiscussion || !task || task.task_id !== state.selectedId || finishedStates.has(task.state)) return;
  const content = $('discussion-content').value.trim();
  const replyTo = state.discussionReplyTarget?.message_id || null;
  if (!content || content.length > 16000) {
    discussionError('请填写 1～16000 字的讨论内容。');
    return;
  }
  if (!replyTo && !/@(白金|codex|platinum|月见|kimi|yuejian|鲸鲸|jingjing|whale|deepseek)(?!\w)/i.test(content)) {
    discussionError('请 @白金、@月见或@鲸鲸，或选择一条 Agent 消息进行回复。');
    return;
  }
  const taskId = task.task_id;
  const requestId = state.requestId;
  const signature = JSON.stringify({ expected_revision: task.revision, content, reply_to: replyTo });
  const prior = state.discussionAttempts.get(taskId);
  const idempotencyKey = prior?.signature === signature ? prior.key : crypto.randomUUID();
  state.discussionAttempts.set(taskId, { signature, key: idempotencyKey });
  state.postingDiscussion = true;
  renderDiscussionComposer();
  discussionError('');
  discussionStatus('');
  try {
    const receipt = await api(`/tasks/${encodeURIComponent(taskId)}/messages/discussion`, {
      method: 'POST', body: JSON.stringify({ expected_revision: task.revision, idempotency_key: idempotencyKey, content, reply_to: replyTo }),
    });
    if (receipt.scope !== 'discussion' || receipt.execution_authorized !== false
        || receipt.agent_dispatched !== false || receipt.task_revision !== task.revision
        || receipt.message?.type !== 'discussion' || receipt.message.sender_role !== 'human'
        || receipt.message.content !== content || receipt.message.reply_to !== replyTo) {
      throw new Error('服务回执与本次讨论消息不一致');
    }
    state.discussionAttempts.delete(taskId);
    if (state.selectedId !== taskId || state.requestId !== requestId) {
      if (state.discussionDrafts.get(taskId)?.trim() === content) state.discussionDrafts.delete(taskId);
      if (state.selectedId === taskId) {
        if ($('discussion-content').value.trim() === content) $('discussion-content').value = '';
        scheduleRefresh(taskId, state.requestId);
      }
      return;
    }
    state.messages = [...new Map([...state.messages, receipt.message].map((item) => [item.message_id, item])).values()]
      .sort((a, b) => a.sequence - b.sequence);
    state.discussionReplyTarget = null;
    state.awaitingDiscussion = receipt.discussion_queued
      ? { taskId, sequence: receipt.message.sequence, correlationId: receipt.message.correlation_id } : null;
    $('discussion-content').value = '';
    state.discussionDrafts.delete(taskId);
    renderMessages(taskId);
    discussionStatus(receipt.discussion_queued
      ? '讨论已排队，等待 Agent 回复；这不会启动改代码。'
      : '讨论已保存；本次回执未确认新排队，请查看对话确认后续回复。');
  } catch (error) {
    if (state.selectedId !== taskId || state.requestId !== requestId) return;
    if (error.status === 409 && error.code !== 'task_message_conflict') {
      try {
        const latest = await api(`/tasks/${encodeURIComponent(taskId)}`);
        if (state.selectedId === taskId && state.requestId === requestId) {
          showTask(latest);
          await loadMessages(taskId, false, requestId);
        }
      } catch { /* Keep the draft and idempotency key. */ }
      if (state.selectedId === taskId && state.requestId === requestId) {
        discussionError(`讨论未确认：${error.detail || error.message}。请先核对对话，不会自动重发。`);
      }
    } else if (error.status === 404 && error.code === 'task_not_found') {
      if (clearMissingTask(taskId)) notice('任务不存在，请刷新任务列表。');
    } else {
      discussionError(`讨论结果未确认：${error.detail || error.message}。草稿已保留；请先核对对话，不会自动重发。`);
    }
  } finally {
    state.postingDiscussion = false;
    renderDiscussionComposer();
  }
}

function renderHumanComposer() {
  const available = state.selectedTask?.state === 'needs_human'
    && !controlUnresolved()
    && state.roomMembers.some((member) => member.role === 'human' && member.kind === 'human');
  $('human-composer').hidden = !available;
  $('human-submit').disabled = state.postingHuman || !available;
  $('human-submit').textContent = state.postingHuman ? '发送中…' : '发送人工消息';
  $('human-reply').hidden = !state.replyTarget;
  $('human-recipient-row').hidden = !!state.replyTarget;
  if (state.replyTarget) $('human-reply-summary').textContent = `回复 ${state.replyTarget.sender_name}：${state.replyTarget.content.slice(0, 120)}`;
}

function selectHumanReply(message) {
  if (state.postingHuman || state.selectedTask?.state !== 'needs_human'
      || controlUnresolved()
      || !message.pending_for_human || !['question', 'human_input_request'].includes(message.type)) return;
  state.replyTarget = { message_id: message.message_id, sender_name: message.sender_name, content: message.content };
  humanError('');
  humanStatus('');
  renderHumanComposer();
  $('human-content').focus();
}

async function postHumanMessage() {
  const task = state.selectedTask;
  if (state.postingHuman || !task || task.task_id !== state.selectedId || task.state !== 'needs_human') return;
  const content = $('human-content').value.trim();
  if (!content || content.length > 16000) {
    humanError('请填写 1～16000 字的人工消息。');
    return;
  }
  const role = $('human-recipient').value;
  if (!state.replyTarget && !['planner', 'implementer', 'reviewer', 'orchestrator'].includes(role)) {
    humanError('请选择有效的收件角色。');
    return;
  }
  const taskId = task.task_id;
  const requestId = state.requestId;
  const destination = state.replyTarget ? { reply_to: state.replyTarget.message_id } : { recipient_role: role };
  const signature = JSON.stringify({ expected_revision: task.revision, content, ...destination });
  const prior = state.humanAttempts.get(taskId);
  const idempotencyKey = prior?.signature === signature ? prior.key : crypto.randomUUID();
  state.humanAttempts.set(taskId, { signature, key: idempotencyKey });
  const body = { expected_revision: task.revision, idempotency_key: idempotencyKey, content, ...destination };
  state.postingHuman = true;
  renderHumanComposer();
  humanError('');
  humanStatus('');
  try {
    const receipt = await api(`/tasks/${encodeURIComponent(taskId)}/messages`, {
      method: 'POST', body: JSON.stringify(body),
    });
    if (receipt.agent_dispatched !== false || receipt.task_revision !== task.revision
        || receipt.message?.sender_role !== 'human' || receipt.message.content !== content
        || receipt.message.reply_to !== (destination.reply_to || null)) {
      throw new Error('服务回执与本次人工消息不一致');
    }
    state.humanAttempts.delete(taskId);
    if (state.selectedId !== taskId || state.requestId !== requestId) return;
    state.messages = [...new Map([...state.messages, receipt.message].map((item) => [item.message_id, item])).values()]
      .sort((a, b) => a.sequence - b.sequence);
    state.replyTarget = null;
    state.controlMessageId = receipt.message.message_id;
    state.control = null;
    state.controlLoadError = null;
    state.inlineFeedback = null;
    $('human-content').value = '';
    renderMessages(taskId);
    humanStatus('人工消息已保存；Agent 尚未启动。请在这条消息旁明确预检并继续。');
    void loadControl(taskId, requestId);
  } catch (error) {
    if (state.selectedId !== taskId || state.requestId !== requestId) return;
    if (error.status === 409 && error.code !== 'task_message_conflict') {
      try {
        const latest = await api(`/tasks/${encodeURIComponent(taskId)}`);
        if (state.selectedId === taskId && state.requestId === requestId) {
          showTask(latest);
          await loadMessages(taskId, false, requestId);
        }
      } catch { /* Preserve the draft and original idempotency key. */ }
      if (state.selectedId === taskId && state.requestId === requestId) {
        humanError('消息未确认：任务或回复目标已变化。已尝试刷新对话；请核对后再提交，不会自动重试。');
      }
    } else if (error.status === 404 && error.code === 'task_not_found') {
      if (clearMissingTask(taskId)) notice('任务不存在，请刷新任务列表。');
    } else {
      humanError(`发送结果未确认：${error.message}。草稿已保留；请先核对对话，再决定是否重试。`);
    }
  } finally {
    state.postingHuman = false;
    renderHumanComposer();
  }
}

const budgetLabels = {
  agent_turns: 'Agent 回合', reported_tokens: '已报告 Token', agent_duration: 'Agent 耗时',
  room_messages: '房间消息', repeated_message: '重复消息', questions_without_progress: '无进展提问',
};

function controlUnresolved() {
  return ['pending', 'claimed', 'needs_human'].includes(state.control?.latest_continuation?.receipt?.state);
}

function controlError(message) {
  $('control-error').textContent = message;
  $('control-error').hidden = !message;
}

function candidateTargets(message) {
  if (!message.pending_for_continuation || message.sender_role !== 'human') return [];
  const recipients = state.roomMembers.filter((member) => message.recipient_ids?.includes(member.member_id));
  if (recipients.some((member) => member.role === 'orchestrator')) return ['planner', 'implementer'];
  return recipients.filter((member) => ['planner', 'implementer'].includes(member.role))
    .map((member) => member.role);
}

function continuationBlocker(message, role, reason) {
  const control = state.control?.task_id === state.selectedId ? state.control : null;
  if (state.controlLoadError) return '控制状态读取失败，请刷新后再继续。';
  if (!control || !state.selectedTask) return '控制状态尚未就绪，不能启动回合。';
  if (control.task_revision !== state.selectedTask.revision || control.task_state !== state.selectedTask.state) return '任务修订已变化，请刷新状态。';
  if (control.task_state !== 'needs_human') return '当前任务不是待人工状态，不能继续。';
  const latest = control.latest_continuation;
  if (controlUnresolved()) return latest.receipt.state === 'claimed'
    ? (control.latest_cancellation ? '取消请求已记录；等待本地回合停止，未知占用仍保留。'
      : '当前回合正在执行。取消仅请求本地 owner 停止，不能证明所有外部进程已停止。')
    : '上次继续请求仍占用任务；失败、取消或未知执行不能从页面重新认领。';
  if (control.budget_violation) {
    const violation = control.budget_violation;
    return `预算阻塞：${budgetLabels[violation.code] || violation.code} ${violation.actual}/${violation.limit}。`;
  }
  if (control.rework_rounds >= control.max_rework_rounds) return '返工预算已耗尽，需人工处理。';
  if (!message || !candidateTargets(message).includes(role)) return '先发送一条待处理的人工消息；发给 Reviewer 的消息不能启动完整工作流。';
  if (latest?.receipt.state === 'succeeded' && (!reason || reason.length > 1000)) return '再次继续需要填写 1～1000 字授权原因。';
  return null;
}

function setInlineFeedback(taskId, messageId, text, kind) {
  if (taskId !== state.selectedId) return;
  state.inlineFeedback = { taskId, messageId, text, kind };
  if (state.messages.length) renderMessages(taskId);
}

const stageRoles = { planning: 'planner', implementing: 'implementer', reviewing: 'reviewer', rework: 'implementer' };
const roleNames = { planner: 'Planner', implementer: 'Implementer', reviewer: 'Reviewer' };
const continuationNames = { pending: '等待认领', claimed: '已受理', succeeded: '已提交', needs_human: '转人工' };
const agentLabel = (role) => {
  const member = state.roomMembers.find((item) => item.role === role && item.kind === 'agent');
  return member?.name ? `${member.name} · ${roleNames[role] || role}` : roleNames[role] || role;
};

function renderWorkflowOverview(control) {
  const task = state.selectedTask;
  $('overview-phase').textContent = task ? stateNames[task.state] || task.state : '未选择任务';
  $('overview-agent').textContent = !task ? '—' : stageRoles[task.state] ? agentLabel(stageRoles[task.state])
    : task.state === 'verifying' ? '确定性 Verifier' : task.state === 'needs_human' ? '等待人工'
      : task.state === 'created' ? '待调度' : '—';
  const latest = control?.latest_continuation;
  const receipt = latest?.receipt;
  const target = receipt?.request?.target_role;
  $('overview-agent-note').textContent = target && ['pending', 'claimed'].includes(receipt.state)
    ? `本次继续入口目标：${agentLabel(target)}；只读快照不含实时执行者，后续阶段可能已切换。`
    : '按 Task 阶段推断责任方；只读快照不证明 Agent 正在运行。';

  let blocking = state.controlLoadError ? '控制状态读取失败；旧快照不可用于判断或继续，请刷新状态。'
    : '正在读取控制状态；不能据此判断是否可继续。';
  if (task && control && !state.controlLoadError) {
    if (control.task_revision !== task.revision || control.task_state !== task.state) {
      blocking = '控制快照与任务修订不一致，请刷新状态。';
    } else {
      const parts = [];
      if (controlUnresolved()) parts.push(receipt.state === 'claimed'
        ? '继续回合已受理；执行占用尚未解除。' : '上次继续请求仍占用任务，不能重新认领。');
      if (control.budget_violation) {
        const violation = control.budget_violation;
        parts.push(`预算阻塞：${budgetLabels[violation.code] || violation.code} ${violation.actual}/${violation.limit}。`);
      }
      if (control.rework_rounds >= control.max_rework_rounds && task.state === 'needs_human') {
        parts.push('返工轮次已耗尽，需要人工处理。');
      }
      if (parts.length) blocking = parts.join(' ');
      else if (task.state === 'needs_human') blocking = state.messages.some((message) => candidateTargets(message).length)
        ? '等待人工显式预检并继续；发送消息不会自动启动 Agent。' : '等待人工消息或澄清。';
      else if (finishedStates.has(task.state)) blocking = '任务已结束；成功与否以完成守卫证据为准。';
      else blocking = '只读快照未报告已知阻塞；具体执行状态以服务端事件为准。';
    }
  }
  $('overview-blocking').textContent = blocking;

  let recent = task ? `任务已创建 · ${time(task.created_at)}` : '未选择任务';
  if (control?.latest_cancellation) {
    const cancellation = control.latest_cancellation;
    recent = `取消请求 #${short(cancellation.request_id)}：${cancellation.state === 'observed'
      ? `已记录本地观察（${cancellation.observation?.outcome || '未知'}）` : '已记录，等待观察'}`
      + `${cancellation.requested_at ? ` · ${time(cancellation.requested_at)}` : ''}；不证明外部进程已停止。`;
  } else if (control?.latest_workflow_outcome) {
    const outcome = control.latest_workflow_outcome;
    recent = `工作流 #${short(outcome.request_id)}：${outcome.success ? '完成守卫通过' : '未完成'}`
      + `${outcome.reason ? ` · ${outcome.reason}` : ''}${latest?.updated_at ? ` · ${time(latest.updated_at)}` : ''}`;
  } else if (receipt) {
    recent = `继续请求 #${short(receipt.request.request_id)}：${continuationNames[receipt.state] || receipt.state}`
      + `${receipt.failure_code ? ` · ${receipt.failure_code}` : ''} · ${time(latest.updated_at)}`;
  }
  $('overview-recent').textContent = `${state.controlLoadError ? '上次快照 · ' : ''}${recent}`;
}

function renderControl() {
  const control = state.control?.task_id === state.selectedId ? state.control : null;
  const messageSelect = $('control-message');
  const candidates = state.messages.filter((message) => candidateTargets(message).length).reverse();
  if (!candidates.some((message) => message.message_id === state.controlMessageId)) {
    state.controlMessageId = candidates[0]?.message_id || null;
  }
  messageSelect.replaceChildren(...candidates.map((message) => {
    const option = node('option', '', `#${short(message.message_id)} · ${message.content.slice(0, 55)}`);
    option.value = message.message_id;
    return option;
  }));
  messageSelect.value = state.controlMessageId || '';
  const selected = candidates.find((message) => message.message_id === state.controlMessageId);
  const roles = selected ? candidateTargets(selected) : [];
  if (!roles.includes($('control-role').value)) $('control-role').value = roles[0] || 'planner';
  $('control-role').disabled = roles.length < 2 || state.controlBusy;
  messageSelect.disabled = !candidates.length || state.controlBusy;
  $('control-budget').replaceChildren();
  if (control) {
    const usage = control.budget_usage;
    const policy = control.budget_policy;
    const items = [
      `回合 ${usage.agent_turns}/${policy.max_agent_turns}`,
      `Token ${usage.reported_total_tokens}/${policy.max_reported_tokens}`,
      `未知 Token ${usage.turns_without_token_usage} 回合`,
      `耗时 ${Math.ceil(usage.agent_duration_ms / 1000)}/${Math.ceil(policy.max_agent_duration_ms / 1000)} 秒`,
      `消息 ${usage.room_messages}/${policy.max_room_messages}`,
      `返工 ${control.rework_rounds}/${control.max_rework_rounds}`,
    ];
    $('control-budget').replaceChildren(...items.map((item) => node('span', '', item)));
  } else $('control-budget').append(node('span', '', '等待服务端预算快照'));
  const latest = control?.latest_continuation;
  const outcome = control?.latest_workflow_outcome;
  const cancellation = control?.latest_cancellation;
  $('control-summary').textContent = !control ? '正在读取控制状态…'
    : cancellation ? `取消请求：${cancellation.state === 'observed' ? `已记录本地观察（${cancellation.observation?.outcome || '未知'}）` : '已记录，等待观察'}；不证明全部外部进程停止。`
      : outcome ? `最近工作流：${outcome.success ? '完成守卫通过' : '未完成'}${outcome.reason ? ` · ${outcome.reason}` : ''}`
      : latest ? `最近回合 #${short(latest.receipt.request.request_id)} · ${latest.receipt.state}${latest.receipt.failure_code ? ` · ${latest.receipt.failure_code}` : ''}`
        : '尚无继续回合；发送人工消息后可预检并启动。';
  const blocker = continuationBlocker(selected, $('control-role').value, ($('control-reason').value || '').trim());
  $('control-blocker').textContent = blocker || '可预检并继续；预检通过不代表任务成功，最终仍由 Verifier、Reviewer 和 CompletionGuard 判定。';
  $('continue-workflow').disabled = state.controlBusy || !!blocker;
  $('continue-workflow').textContent = state.controlBusy ? '处理中…' : '预检并继续工作流';
  $('cancel-continuation').hidden = latest?.receipt.state !== 'claimed' || !!cancellation;
  $('cancel-continuation').disabled = state.controlBusy || !!state.controlLoadError;
  renderWorkflowOverview(control);
}

function closeControlPoll() {
  if (state.controlTimer !== null) clearTimeout(state.controlTimer);
  state.controlTimer = null;
}

function scheduleControlPoll(taskId, requestId, delay = 3000) {
  closeControlPoll();
  if (taskId !== state.selectedId || requestId !== state.requestId) return;
  state.controlTimer = setTimeout(() => {
    state.controlTimer = null;
    void loadControl(taskId, requestId);
  }, delay);
}

async function loadControl(taskId, requestId = state.requestId) {
  try {
    const previousState = state.control?.latest_continuation?.receipt?.state;
    const control = await api(`/tasks/${encodeURIComponent(taskId)}/control`);
    if (taskId !== state.selectedId || requestId !== state.requestId) return;
    if (state.controlLoadError) controlError('');
    state.control = control;
    state.controlLoadError = null;
    if (state.inlineFeedback?.kind === 'progress'
        && control.latest_continuation?.receipt.request.message_id === state.inlineFeedback.messageId) {
      state.inlineFeedback = null;
    }
    renderControl();
    renderHumanComposer();
    if (state.messages.length) renderMessages(taskId);
    if (control.task_revision !== state.selectedTask?.revision || control.task_state !== state.selectedTask?.state) {
      const task = await api(`/tasks/${encodeURIComponent(taskId)}`);
      if (taskId !== state.selectedId || requestId !== state.requestId) return;
      showTask(task);
      void loadDelivery(taskId, requestId);
    }
    if (previousState === 'claimed' && control.latest_continuation?.receipt.state !== 'claimed') {
      await loadMessages(taskId, false, requestId);
    }
    if (control.latest_continuation?.receipt.state === 'claimed') scheduleControlPoll(taskId, requestId);
    else closeControlPoll();
  } catch (error) {
    if (taskId !== state.selectedId || requestId !== state.requestId) return;
    state.controlLoadError = error.message;
    controlError(`控制状态读取失败：${error.message}。请手动刷新；不会自动提交操作。`);
    renderControl();
    renderHumanComposer();
    if (state.messages.length) renderMessages(taskId);
    if (state.control?.latest_continuation?.receipt.state === 'claimed') scheduleControlPoll(taskId, requestId, 5000);
  }
}

async function continueWorkflow(intent = null) {
  const task = state.selectedTask;
  const control = state.control?.task_id === state.selectedId ? state.control : null;
  const message = state.messages.find((item) => item.message_id === (intent?.messageId || state.controlMessageId));
  const role = intent?.targetRole || $('control-role').value;
  const reason = intent ? intent.reason.trim() : ($('control-reason').value || '').trim();
  const blocker = continuationBlocker(message, role, reason);
  if (state.controlBusy || !task || task.task_id !== state.selectedId || blocker) {
    if (intent && blocker && task) setInlineFeedback(task.task_id, intent.messageId, blocker, 'error');
    return;
  }
  const previous = control.latest_continuation;
  if (!window.confirm(`确认让 ${role} 处理人工消息 #${short(message.message_id)}，并启动后续验证与评审？`)) return;
  const taskId = task.task_id;
  const requestId = state.requestId;
  state.controlMessageId = message.message_id;
  $('control-role').value = role;
  if (intent) $('control-reason').value = reason;
  const signature = JSON.stringify({ revision: task.revision, runtime: control.runtime_revision,
    message_id: message.message_id, target_role: role, previous: previous?.receipt.request.request_id || null,
    previous_updated_at: previous?.updated_at || null, reason });
  const prior = state.controlAttempts.get(taskId);
  const attempt = prior?.signature === signature ? prior : { signature, key: crypto.randomUUID(), grant: null };
  state.controlAttempts.set(taskId, attempt);
  state.controlBusy = true;
  controlError('');
  if (intent) setInlineFeedback(taskId, message.message_id, '正在预检；尚未派发 Agent。', 'progress');
  renderControl();
  try {
    const selection = { expected_revision: task.revision, message_id: message.message_id, target_role: role };
    const check = await api(`/tasks/${encodeURIComponent(taskId)}/continue/preflight`, {
      method: 'POST', body: JSON.stringify(selection),
    });
    if (check.checks_passed !== true || check.execution_ready !== false
        || check.message_id !== message.message_id || check.task_revision !== task.revision
        || check.target_role !== role) throw new Error('预检回执与选择不一致');
    if (taskId !== state.selectedId || requestId !== state.requestId) return;
    if (previous?.receipt.state === 'succeeded' && !attempt.grant) {
      const grant = await api(`/tasks/${encodeURIComponent(taskId)}/continuations/${encodeURIComponent(previous.receipt.request.request_id)}/authorize`, {
        method: 'POST', body: JSON.stringify({ ...selection, idempotency_key: attempt.key,
          expected_runtime_revision: control.runtime_revision,
          expected_claim_updated_at: previous.updated_at, reason }),
      });
      if (grant.intent?.message_id !== message.message_id || grant.intent?.target_role !== role
          || grant.intent?.idempotency_key !== attempt.key || !grant.authorization_id) {
        throw new Error('授权回执与当前意图不一致');
      }
      attempt.grant = grant.authorization_id;
    }
    if (taskId !== state.selectedId || requestId !== state.requestId) return;
    const accepted = await api(`/tasks/${encodeURIComponent(taskId)}/continue/workflow`, {
      method: 'POST', body: JSON.stringify({ ...selection, idempotency_key: attempt.key,
        authorization_id: attempt.grant }),
    });
    if (accepted.receipt?.scope !== 'controlled-workflow-continuation'
        || accepted.receipt.request?.message_id !== message.message_id
        || accepted.receipt.request?.idempotency_key !== attempt.key) {
      throw new Error('继续回执与当前意图不一致');
    }
    state.controlAttempts.delete(taskId);
    if (taskId === state.selectedId && requestId === state.requestId) {
      $('control-summary').textContent = `请求 #${short(accepted.receipt.request.request_id)} 已受理；正在读取运行状态。`;
      if (intent) setInlineFeedback(taskId, message.message_id, '继续请求已受理；正在读取运行状态。', 'progress');
      await loadControl(taskId, requestId);
    }
  } catch (error) {
    if (taskId === state.selectedId && requestId === state.requestId) {
      const errorMessage = `继续结果未确认：${error.detail || error.message}。不会自动重发；请核对状态后手动处理。`;
      controlError(errorMessage);
      if (intent) setInlineFeedback(taskId, message.message_id, errorMessage, 'error');
      await loadControl(taskId, requestId);
    }
  } finally {
    state.controlBusy = false;
    renderControl();
    if (intent && taskId === state.selectedId && requestId === state.requestId) renderMessages(taskId);
  }
}

async function cancelContinuation() {
  const task = state.selectedTask;
  const latest = state.control?.latest_continuation;
  const reason = $('control-reason').value.trim();
  if (state.controlBusy || state.controlLoadError || !task || task.task_id !== state.selectedId || latest?.receipt.state !== 'claimed'
      || state.control?.latest_cancellation) return;
  if (!reason || reason.length > 1000) { controlError('取消需填写 1～1000 字原因。'); return; }
  if (!window.confirm('确认请求取消当前回合？这不会证明所有外部进程或远端请求已停止，也不会释放未知占用。')) return;
  const taskId = task.task_id;
  const requestId = state.requestId;
  const continuationId = latest.receipt.request.request_id;
  state.controlBusy = true;
  controlError('');
  renderControl();
  try {
    const current = await api(`/tasks/${encodeURIComponent(taskId)}/continuations/${encodeURIComponent(continuationId)}`);
    if (current.receipt.state !== 'claimed') throw new Error('回合状态已变化，请刷新后核对');
    if (taskId !== state.selectedId || requestId !== state.requestId) return;
    const signature = JSON.stringify({ continuationId, revision: current.task_revision,
      runtime: current.runtime_revision, updated_at: current.updated_at, reason });
    const prior = state.cancelAttempts.get(taskId);
    const key = prior?.signature === signature ? prior.key : crypto.randomUUID();
    state.cancelAttempts.set(taskId, { signature, key });
    const receipt = await api(`/tasks/${encodeURIComponent(taskId)}/continuations/${encodeURIComponent(continuationId)}/cancel`, {
      method: 'POST', body: JSON.stringify({ idempotency_key: key, expected_revision: current.task_revision,
        expected_runtime_revision: current.runtime_revision, expected_claim_updated_at: current.updated_at, reason }),
    });
    if (receipt.request_id !== continuationId || receipt.claim_released !== false
        || receipt.external_process_stopped_confirmed !== false) throw new Error('取消回执与当前回合不一致');
    state.cancelAttempts.delete(taskId);
    if (taskId === state.selectedId && requestId === state.requestId) {
      $('control-summary').textContent = '取消请求已记录；等待本地回合停止观察，未知占用仍保留。';
      await loadControl(taskId, requestId);
    }
  } catch (error) {
    if (taskId === state.selectedId && requestId === state.requestId) {
      controlError(`取消结果未确认：${error.detail || error.message}。请核对状态，不会自动重发。`);
      await loadControl(taskId, requestId);
    }
  } finally {
    state.controlBusy = false;
    renderControl();
  }
}

function renderCancelAction() {
  const button = $('cancel-task');
  button.hidden = !state.selectedTask || terminal.has(state.selectedTask.state);
  button.disabled = state.cancelling;
  button.textContent = state.cancelling ? '取消中…' : '取消任务';
}

function clearMissingTask(taskId) {
  const wasSelected = state.selectedId === taskId;
  state.tasks = state.tasks.filter((task) => task.task_id !== taskId);
  if (!wasSelected) { renderTasks(); return false; }
  closeStream();
  state.requestId += 1;
  state.selectedId = null;
  state.selectedTask = null;
  state.messages = [];
  state.roomMembers = [];
  state.replyTarget = null;
  state.control = null;
  state.controlLoadError = null;
  state.controlMessageId = null;
  state.inlineFeedback = null;
  state.delivery = null;
  state.deliveryError = null;
  closeControlPoll();
  renderTasks();
  renderCancelAction();
  renderHumanComposer();
  renderControl();
  $('task-detail').hidden = true;
  $('inspector-workflow').hidden = true;
  $('delivery-panel').hidden = true;
  $('empty-detail').hidden = false;
  $('empty-detail').querySelector('h2').textContent = '任务已不存在';
  $('empty-detail').querySelector('p').textContent = '请刷新任务列表后重新选择。';
  return true;
}

async function cancelTask() {
  const task = state.selectedTask;
  if (state.cancelling || !task || task.task_id !== state.selectedId || terminal.has(task.state)) return;
  if (!window.confirm('确定取消当前任务？正在运行的 Agent 会被停止。')) return;
  const taskId = task.task_id;
  state.cancelling = true;
  renderCancelAction();
  try {
    const updated = await api(`/tasks/${encodeURIComponent(taskId)}/cancel`, {
      method: 'POST',
      body: JSON.stringify({ expected_revision: task.revision }),
    });
    if (state.selectedId === taskId) {
      showTask(updated);
      if (terminal.has(updated.state)) {
        state.requestId += 1;
        closeStream();
        $('live-status').textContent = updated.state === 'needs_human'
          ? '等待人工输入 · 可发送消息' : '任务已结束 · 显示最终记录';
      }
      notice(updated.state === 'cancelled' ? '任务已取消。' : '取消请求已处理，请检查最新任务状态。');
    } else {
      const index = state.tasks.findIndex((item) => item.task_id === taskId);
      if (index !== -1) state.tasks[index] = updated;
      renderTasks();
    }
  } catch (error) {
    if (error.status === 409) {
      try {
        const latest = await api(`/tasks/${encodeURIComponent(taskId)}`);
        if (state.selectedId === taskId) {
          showTask(latest);
          if (terminal.has(latest.state)) {
            state.requestId += 1;
            closeStream();
            $('live-status').textContent = latest.state === 'needs_human'
              ? '等待人工输入 · 可发送消息' : '任务已结束 · 显示最终记录';
          }
          notice('任务状态已变化，已刷新详情；如仍需取消，请确认后重新操作。');
        } else {
          const index = state.tasks.findIndex((item) => item.task_id === taskId);
          if (index !== -1) state.tasks[index] = latest;
          renderTasks();
        }
      } catch (refreshError) {
        const missingSelected = refreshError.status === 404 && clearMissingTask(taskId);
        if (state.selectedId === taskId || missingSelected) notice(`任务状态已变化，但刷新失败：${refreshError.message}`);
      }
    } else if (error.status === 404) {
      if (clearMissingTask(taskId)) notice('任务不存在，请刷新任务列表。');
    } else if (state.selectedId === taskId) {
      notice(`取消请求结果未确认：${error.message}。请刷新任务状态后再决定是否重试。`);
    }
  } finally {
    state.cancelling = false;
    renderCancelAction();
  }
}

function createError(message) {
  $('create-error').textContent = message;
  $('create-error').hidden = !message;
}

function setCreateOpen(open) {
  if (state.creating && !open) return;
  $('create-form').hidden = !open;
  $('create-toggle').setAttribute('aria-expanded', String(open));
  if (open) $('create-repository').focus();
  else { createError(''); $('create-toggle').focus(); }
}

async function createTask() {
  if (state.creating) return;
  const repositoryPath = $('create-repository').value.trim();
  const issue = $('create-issue').value.trim();
  if (!repositoryPath || !issue) {
    createError('请填写 Git 仓库路径和开发需求。');
    return;
  }
  if (repositoryPath.length > 4096 || issue.length > 16000) {
    createError('仓库路径或开发需求超过长度限制。');
    return;
  }
  state.creating = true;
  $('create-submit').disabled = true;
  $('create-submit').textContent = '正在创建…';
  $('create-close').disabled = true;
  createError('');
  let created;
  try {
    created = await api('/tasks', {
      method: 'POST',
      body: JSON.stringify({ repository_path: repositoryPath, issue }),
    });
  } catch (error) {
    const uncertain = error.status === undefined || (error.status >= 500 && error.status !== 503);
    createError(uncertain ? `提交结果不明：${error.message}。任务可能已创建，请先刷新列表确认。` : error.message);
    return;
  } finally {
    state.creating = false;
    $('create-submit').disabled = false;
    $('create-submit').textContent = '创建并查看任务';
    $('create-close').disabled = false;
  }
  if (!created?.task_id) {
    createError('服务未返回任务 ID；任务可能已创建，请先刷新列表确认。');
    return;
  }
  $('create-repository').value = '';
  $('create-issue').value = '';
  setCreateOpen(false);
  state.filter = 'all';
  document.querySelectorAll('.filter').forEach((button) => button.classList.toggle('active', button.dataset.filter === 'all'));
  state.tasks = [created, ...state.tasks.filter((item) => item.task_id !== created.task_id)];
  renderTasks();
  await selectTask(created.task_id);
  await loadTasks();
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
    const [plans, messagesLoaded] = await Promise.all([
      api(`/tasks/${encodeURIComponent(taskId)}/plans`),
      loadMessages(taskId, true, requestId),
    ]);
    if (requestId !== state.requestId) return;
    renderPlans(plans.items, taskId);
    void loadControl(taskId, requestId);
    void loadDelivery(taskId, requestId);
    if (messagesLoaded) notice('');
    if (finishedStates.has(task.state)) {
      closeStream();
      $('live-status').textContent = '任务已结束 · 显示最终记录';
    } else if (task.state === 'needs_human') {
      $('live-status').textContent = '等待人工输入 · 讨论实时更新';
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
    $('live-status').textContent = state.selectedTask?.state === 'needs_human'
      ? '等待人工输入 · 浏览器不支持实时连接，可手动刷新'
      : '浏览器不支持实时连接 · 可手动刷新';
    return;
  }
  const source = new EventSource(`/api/v1/tasks/${encodeURIComponent(taskId)}/events`);
  state.eventSource = source;
  $('live-status').textContent = '正在连接实时事件…';
  source.onopen = () => {
    if (requestId === state.requestId) $('live-status').textContent = state.selectedTask?.state === 'needs_human'
      ? '等待人工输入 · 讨论实时更新' : '实时连接中 · 自动更新';
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
    if (!state.selectedId) {
      const requested = window.location?.search
        ? new URLSearchParams(window.location.search).get('task') : null;
      if (requested) await selectTask(requested);
      else if (state.tasks.length) await selectTask(state.tasks[0].task_id);
    }
    return true;
  } catch (error) {
    notice(`任务列表读取失败：${error.message}`);
    if (!more && !state.tasks.length) $('task-list').replaceChildren(node('div', 'empty-state', '暂时无法获取任务'));
    return false;
  }
}

function renderContinuationAction(message, taskId) {
  const targets = candidateTargets(message);
  if (!targets.length) return null;
  const key = `${taskId}:${message.message_id}`;
  const box = node('div', 'message-continuation');
  const intro = node('p', 'message-continuation-intro');
  box.append(intro);
  const fields = node('div', 'message-continuation-fields');
  let roleSelect = null;
  if (targets.length > 1) {
    const label = node('label', 'message-continuation-label', '目标 Agent');
    roleSelect = node('select', 'message-continuation-target');
    for (const role of targets) {
      const option = node('option', '', role === 'planner' ? '白金 · Planner' : '月见 · Implementer');
      option.value = role;
      roleSelect.append(option);
    }
    roleSelect.value = targets.includes(state.inlineRoles.get(key)) ? state.inlineRoles.get(key) : targets[0];
    label.append(roleSelect);
    fields.append(label);
  }
  let reasonInput = null;
  if (state.control?.latest_continuation?.receipt.state === 'succeeded') {
    const label = node('label', 'message-continuation-label', '再次继续的授权原因');
    reasonInput = node('textarea', 'message-continuation-reason');
    reasonInput.rows = 2;
    reasonInput.maxLength = 1000;
    reasonInput.placeholder = '说明为何再次继续当前任务';
    reasonInput.value = state.inlineReasons.get(key) || '';
    label.append(reasonInput);
    fields.append(label);
  }
  box.append(fields);
  const button = node('button', 'message-continue-button', '预检并继续工作流');
  button.type = 'button';
  const status = node('p', 'message-continuation-status');
  status.setAttribute('role', 'status');
  const update = () => {
    const role = roleSelect?.value || targets[0];
    const reason = reasonInput?.value.trim() || '';
    const blocker = continuationBlocker(message, role, reason);
    const latest = state.control?.latest_continuation?.receipt;
    intro.textContent = latest?.request.message_id === message.message_id && latest.state === 'claimed'
      ? '继续请求已受理；当前回合正在执行。' : '人工消息已保存；Agent 尚未启动。';
    const feedback = state.inlineFeedback?.taskId === taskId
      && state.inlineFeedback.messageId === message.message_id ? state.inlineFeedback : null;
    button.disabled = state.controlBusy || !!blocker;
    status.textContent = feedback?.text || (state.controlBusy ? '正在处理继续请求…'
      : blocker || '点击后将请求确认，并由服务端预检；预检通过不代表任务完成。');
    status.className = `message-continuation-status${feedback?.kind === 'error' || blocker ? ' is-blocked' : ''}`;
  };
  roleSelect?.addEventListener('change', () => {
    state.inlineRoles.set(key, roleSelect.value);
    update();
  });
  reasonInput?.addEventListener('input', () => {
    state.inlineReasons.set(key, reasonInput.value);
    if (state.inlineFeedback?.taskId === taskId && state.inlineFeedback.messageId === message.message_id) state.inlineFeedback = null;
    update();
  });
  button.addEventListener('click', () => {
    void continueWorkflow({ messageId: message.message_id, targetRole: roleSelect?.value || targets[0],
      reason: reasonInput?.value || '' });
  });
  box.append(button, status);
  update();
  return box;
}

function renderMessage(message, taskId) {
  const item = node('article', `message message-${message.sender_role === 'human' ? 'human' : 'agent'}`);
  const head = node('div', 'message-head');
  const role = message.sender_role;
  head.append(window.CodeCrewAvatars.create(role, message.sender_name), node('span', 'message-name', message.sender_name), node('span', 'message-role', role), node('time', 'message-time', time(message.created_at)));
  item.append(head, node('span', 'message-type', message.type.replaceAll('_', ' ')), node('p', 'message-content', message.content));
  if (message.reply_to) item.append(node('p', 'message-reply-link', `↳ 回复 #${short(message.reply_to)}`));
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
  if (state.selectedTask?.state === 'needs_human' && !controlUnresolved() && message.pending_for_human
      && !state.messages.some((candidate) => candidate.sender_role === 'human' && candidate.reply_to === message.message_id)
      && ['question', 'human_input_request'].includes(message.type)) {
    const reply = node('button', 'reply-action', '回复这条消息');
    reply.type = 'button';
    reply.addEventListener('click', () => selectHumanReply(message));
    item.append(reply);
  }
  if (['planner', 'implementer', 'reviewer'].includes(role)
      && !finishedStates.has(state.selectedTask?.state)) {
    const reply = node('button', 'discussion-reply-action', '在讨论中回复');
    reply.type = 'button';
    reply.addEventListener('click', () => selectDiscussionReply(message));
    item.append(reply);
  }
  const continuation = renderContinuationAction(message, taskId);
  if (continuation) item.append(continuation);
  return item;
}

function renderMessages(taskId) {
  const selectedParent = state.messages.find((message) => message.message_id === state.replyTarget?.message_id);
  if (selectedParent && !selectedParent.pending_for_human) {
    state.replyTarget = null;
    renderHumanComposer();
  }
  const discussionParent = state.messages.find((message) => message.message_id === state.discussionReplyTarget?.message_id);
  if (state.discussionReplyTarget && !discussionParent) {
    state.discussionReplyTarget = null;
    renderDiscussionComposer();
  }
  if (state.awaitingDiscussion?.taskId === taskId
      && state.messages.some((message) => message.sequence > state.awaitingDiscussion.sequence
        && message.correlation_id === state.awaitingDiscussion.correlationId
        && message.type === 'discussion'
        && ['planner', 'implementer', 'reviewer'].includes(message.sender_role))) {
    state.awaitingDiscussion = null;
    discussionStatus('Agent 已回复。');
  }
  const list = $('message-list');
  list.replaceChildren();
  if (!state.messages.length) list.append(node('div', 'empty-state', '暂无对话'));
  for (const message of state.messages) list.append(renderMessage(message, taskId));
  renderControl();
}

async function loadMessages(taskId, more = false, requestId = state.requestId) {
  try {
    const page = await api(`/tasks/${encodeURIComponent(taskId)}/messages?limit=50&after_sequence=${more ? state.messageCursor : 0}`);
    if (requestId !== state.requestId) return;
    state.messages = [...new Map([...(more ? state.messages : []), ...page.items]
      .map((message) => [message.message_id || `sequence:${message.sequence}`, message])).values()]
      .sort((a, b) => a.sequence - b.sequence);
    if (page.items.length) state.messageCursor = page.items.at(-1).sequence;
    state.messageHasMore = page.next_after_sequence !== null;
    $('load-messages').hidden = !state.messageHasMore;
    renderMessages(taskId);
    return true;
  } catch (error) {
    if (requestId === state.requestId) notice(`对话读取失败：${error.message}`);
    return false;
  }
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

const checkNames = {
  diff: '有效 Diff', permission: '目录权限', command_policy: '命令策略',
  static_analysis: '静态检查', build: '编译', public_tests: '公开测试', hidden_tests: '隐藏测试',
};
const checkStates = { passed: '通过', failed: '失败', blocked: '受阻' };
const conditionNames = {
  valid_diff: '有效 Diff', static_or_build: '编译或静态检查', public_tests: '公开测试',
  hidden_tests: '隐藏测试', permissions: '目录权限', command_policy: '命令策略',
  verification: '确定性验证', review_approval: '独立评审批准',
  high_priority_issues: '高优先级问题', evidence_integrity: '证据完整性',
};

function deliveryArtifactButton(taskId, artifactId, label) {
  const button = node('button', 'delivery-artifact-button', label);
  button.type = 'button';
  button.addEventListener('click', () => { void loadArtifact(taskId, artifactId); });
  return button;
}

function deliveryFact(label, result, passed) {
  const row = node('div', 'delivery-fact');
  row.append(node('span', '', label), node('strong', passed ? '' : 'is-failed', result));
  return row;
}

function renderDelivery() {
  const task = state.selectedTask;
  const delivery = state.delivery?.task_id === state.selectedId ? state.delivery : null;
  const status = $('delivery-status');
  const content = $('delivery-content');
  const download = $('delivery-patch-download');
  content.hidden = true;
  download.hidden = true;
  download.href = '';
  status.className = 'delivery-status';
  if (!task) { status.textContent = '正在读取任务与交付证据…'; return; }
  if (state.deliveryError) {
    status.className = 'delivery-status is-error';
    status.textContent = `交付证据读取失败：${state.deliveryError}。不能根据旧记录判断结果，请刷新。`;
    return;
  }
  if (!delivery) { status.textContent = '正在读取交付证据…'; return; }
  if (delivery.task_revision !== task.revision || delivery.trace_id !== task.trace_id
      || delivery.task_state !== task.state) {
    status.textContent = '交付证据与当前任务修订不一致，请刷新。';
    return;
  }
  content.hidden = false;
  status.className = `delivery-status${delivery.delivery_ready ? ' is-ready' : ''}`;
  status.textContent = delivery.delivery_ready
    ? '交付就绪：任务已完成，最新验证、独立 Review 与完成守卫证据一致。'
    : '尚未形成可交付结论；下方展示最近一次可信证据，不代表任务成功。';

  const diff = $('delivery-diff');
  diff.replaceChildren();
  if (delivery.patch) {
    const changed = delivery.verification?.changed_files || [];
    diff.append(node('p', '', `最近验证 Diff · ${changed.length} 个变更文件`));
    if (changed.length) diff.append(node('p', 'delivery-muted',
      `${changed.slice(0, 5).join('、')}${changed.length > 5 ? ` 等 ${changed.length} 个文件` : ''}`));
    diff.append(deliveryArtifactButton(task.task_id, delivery.patch.artifact_id, '预览 Diff 证据 ↗'));
    download.href = `/api/v1/tasks/${encodeURIComponent(task.task_id)}/delivery/patch/${encodeURIComponent(delivery.patch.artifact_id)}`;
    download.textContent = delivery.delivery_ready ? '下载最终 Patch ↓' : '下载最近验证 Patch（非最终）↓';
    download.hidden = false;
  } else diff.append(node('p', 'delivery-muted', '尚无验证关联的有效 Diff 或 Patch。'));

  const checks = $('delivery-checks');
  checks.replaceChildren();
  if (delivery.verification) {
    checks.append(node('p', '', delivery.verification.passed ? 'Verifier 总结：通过' : 'Verifier 总结：未通过'));
    for (const check of delivery.verification.checks) {
      checks.append(deliveryFact(checkNames[check.kind] || check.name,
        checkStates[check.status] || check.status, check.status === 'passed'));
      if (check.detail) checks.append(node('p', 'delivery-muted', check.detail));
    }
    checks.append(deliveryArtifactButton(task.task_id, delivery.verification.artifact.artifact_id, '查看验证报告 ↗'));
  } else checks.append(node('p', 'delivery-muted', '尚无确定性验证报告。'));

  const review = $('delivery-review');
  review.replaceChildren();
  if (delivery.review) {
    review.append(node('p', '', `最近评审：${delivery.review.verdict === 'approved' ? '批准' : '退回'}${delivery.review.follows_latest_verification ? '' : '（早于最新验证）'}`));
    review.append(node('p', 'delivery-muted', delivery.review.summary));
    for (const issue of delivery.review.issues) {
      review.append(deliveryFact(`${issue.priority} · ${issue.summary}`, issue.resolved ? '已解决' : '未解决', issue.resolved));
    }
    review.append(deliveryArtifactButton(task.task_id, delivery.review.artifact.artifact_id, '查看 Review 证据 ↗'));
  } else review.append(node('p', 'delivery-muted', '尚无独立 Review 结论。'));

  const guard = $('delivery-guard');
  guard.replaceChildren();
  if (delivery.completion) {
    guard.append(node('p', '', `CompletionGuard：${delivery.completion.passed ? '通过' : '未通过'}`));
    for (const condition of delivery.completion.conditions) {
      guard.append(deliveryFact(conditionNames[condition.kind] || condition.kind,
        condition.passed ? '通过' : '未通过', condition.passed));
    }
    guard.append(deliveryArtifactButton(task.task_id, delivery.completion.artifact.artifact_id, '查看守卫判定 ↗'));
  } else guard.append(node('p', 'delivery-muted', '尚无绑定最新验证与评审的完成守卫判定。'));
}

async function loadDelivery(taskId, requestId = state.requestId) {
  const deliveryRequestId = ++state.deliveryRequestId;
  try {
    const delivery = await api(`/tasks/${encodeURIComponent(taskId)}/delivery`);
    if (taskId !== state.selectedId || requestId !== state.requestId
        || deliveryRequestId !== state.deliveryRequestId) return;
    if (delivery.task_id !== taskId) throw new Error('交付证据不属于当前任务');
    state.delivery = delivery;
    state.deliveryError = null;
    renderDelivery();
  } catch (error) {
    if (taskId !== state.selectedId || requestId !== state.requestId
        || deliveryRequestId !== state.deliveryRequestId) return;
    state.delivery = null;
    state.deliveryError = error.message;
    renderDelivery();
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
  } catch (error) { if (taskId === state.selectedId) notice(`证据读取失败：${error.message}`); }
}

async function selectTask(taskId) {
  closeStream();
  closeControlPoll();
  if (state.selectedId !== taskId) {
    if (state.selectedId) state.discussionDrafts.set(state.selectedId, $('discussion-content').value);
    $('discussion-content').value = state.discussionDrafts.get(taskId) || '';
    $('human-content').value = '';
    $('issue-details').open = false;
    humanError('');
    humanStatus('');
    discussionError('');
    discussionStatus('');
    controlError('');
  }
  state.selectedId = taskId;
  state.selectedTask = null;
  state.messages = [];
  state.roomMembers = [];
  state.replyTarget = null;
  state.discussionReplyTarget = null;
  state.awaitingDiscussion = null;
  state.control = null;
  state.controlLoadError = null;
  state.controlMessageId = null;
  state.inlineFeedback = null;
  state.delivery = null;
  state.deliveryError = null;
  renderCancelAction();
  renderDiscussionComposer();
  renderHumanComposer();
  renderControl();
  renderDelivery();
  state.requestId += 1;
  const requestId = state.requestId;
  state.messageCursor = 0;
  renderTasks();
  $('empty-detail').hidden = true;
  $('task-detail').hidden = false;
  $('inspector-workflow').hidden = false;
  $('delivery-panel').hidden = false;
  $('live-status').textContent = '正在读取任务…';
  $('detail-title').textContent = '正在加载…';
  $('detail-issue').textContent = '';
  $('detail-status').replaceChildren();
  $('room-members').replaceChildren();
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
    state.roomMembers = room.room.members;
    renderDiscussionComposer();
    renderHumanComposer();
    $('room-members').replaceChildren(...room.room.members.filter((member) => member.kind === 'agent').map((member) => {
      const chip = node('span', 'member-chip');
      chip.append(window.CodeCrewAvatars.create(member.role, member.name), node('b', '', member.name), node('span', '', member.role));
      return chip;
    }));
    renderPlans(plans.items, taskId);
    notice('');
    await loadMessages(taskId, false, requestId);
    await loadControl(taskId, requestId);
    await loadDelivery(taskId, requestId);
    if (finishedStates.has(task.state)) $('live-status').textContent = '任务已结束 · 显示最终记录';
    else followTask(taskId, requestId);
  } catch (error) {
    if (requestId !== state.requestId) return;
    $('task-detail').hidden = true;
    $('inspector-workflow').hidden = true;
    $('delivery-panel').hidden = true;
    $('empty-detail').hidden = false;
    $('empty-detail').querySelector('h2').textContent = '任务详情暂不可用';
    $('empty-detail').querySelector('p').textContent = '请检查任务是否存在，或稍后重试。';
    notice(`任务详情读取失败：${error.message}`);
  }
}

document.querySelectorAll('.filter').forEach((button) => button.addEventListener('click', () => {
  document.querySelectorAll('.filter').forEach((item) => {
    item.classList.toggle('active', item === button);
    item.setAttribute('aria-pressed', String(item === button));
  });
  state.filter = button.dataset.filter;
  renderTasks();
}));
$('create-toggle').addEventListener('click', () => setCreateOpen($('create-form').hidden));
$('create-close').addEventListener('click', () => setCreateOpen(false));
$('create-form').addEventListener('submit', (event) => { event.preventDefault(); void createTask(); });
$('cancel-task').addEventListener('click', () => { void cancelTask(); });
$('discussion-form').addEventListener('submit', (event) => { event.preventDefault(); void postDiscussionMessage(); });
$('discussion-content').addEventListener('input', () => {
  if (state.selectedId) state.discussionDrafts.set(state.selectedId, $('discussion-content').value);
  discussionError('');
});
$('discussion-reply-clear').addEventListener('click', () => {
  state.discussionReplyTarget = null;
  discussionError('');
  renderDiscussionComposer();
});
document.querySelectorAll('[data-discussion-mention]').forEach((button) => button.addEventListener('click', () => insertDiscussionMention(button.dataset.discussionMention)));
$('human-form').addEventListener('submit', (event) => { event.preventDefault(); void postHumanMessage(); });
$('human-reply-clear').addEventListener('click', () => { state.replyTarget = null; humanError(''); renderHumanComposer(); });
$('control-refresh').addEventListener('click', () => { if (state.selectedId) void loadControl(state.selectedId); });
$('delivery-refresh').addEventListener('click', () => { if (state.selectedId) void loadDelivery(state.selectedId); });
$('control-message').addEventListener('change', () => { state.controlMessageId = $('control-message').value; renderControl(); });
$('control-role').addEventListener('change', renderControl);
$('control-reason').addEventListener('input', renderControl);
$('continue-workflow').addEventListener('click', () => { void continueWorkflow(); });
$('cancel-continuation').addEventListener('click', () => { void cancelContinuation(); });
const tabs = [...document.querySelectorAll('.tab')];
function activateTab(button) {
  for (const item of tabs) {
    const selected = item === button;
    item.classList.toggle('active', selected);
    item.setAttribute('aria-selected', String(selected));
    item.tabIndex = selected ? 0 : -1;
  }
  $('room-pane').hidden = button.dataset.tab !== 'room';
  $('plans-pane').hidden = button.dataset.tab !== 'plans';
}
tabs.forEach((button, index) => {
  button.addEventListener('click', () => activateTab(button));
  button.addEventListener('keydown', (event) => {
    const next = event.key === 'ArrowRight' ? (index + 1) % tabs.length
      : event.key === 'ArrowLeft' ? (index - 1 + tabs.length) % tabs.length
        : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : null;
    if (next === null) return;
    event.preventDefault();
    activateTab(tabs[next]);
    tabs[next].focus();
  });
});
$('refresh-button').addEventListener('click', async () => { if (await loadTasks() && state.selectedId) await selectTask(state.selectedId); });
$('load-more').addEventListener('click', () => loadTasks(true));
$('load-messages').addEventListener('click', () => loadMessages(state.selectedId, true));
$('clock').textContent = new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
window.addEventListener('beforeunload', () => { closeStream(); closeControlPoll(); });
loadTasks();
