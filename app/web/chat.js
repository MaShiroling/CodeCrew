const chatState = {
  rooms: [], room: null, members: new Map(), messages: new Map(), lastSequence: 0,
  replyTo: null, contextAnchorId: null, requestId: 0, pendingCreateKey: null, pendingSend: null,
  creating: false, sending: false, eventSource: null, refreshId: 0,
  codingCapability: {available: false, allowed_paths: []}, codingSource: null,
  codingPreview: null, codingCommand: null, codingBusy: false, codingVersion: 0,
};

const byId = (id) => document.getElementById(id);
const CHAT_API = '/api/v1/chats';
const CHAT_ROLES = {human: '我', planner: '白金', implementer: '月见', reviewer: '鲸鲸'};
const TURN_LABELS = {
  queued: '排队中', running: '正在回复', succeeded: '已回复', failed: '失败',
  cancelled: '已取消', interrupted: '已中断', budget_exhausted: '回合预算已用尽',
};

async function chatRequest(path, options = {}) {
  let response;
  try {
    response = await fetch(path, options);
  } catch (_) {
    throw new Error('网络未确认请求结果；请保持内容不变后重试。');
  }
  let data;
  try { data = await response.json(); } catch (_) { data = null; }
  if (!response.ok) {
    const error = new Error(data?.error?.message || `请求失败（${response.status}）`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function showError(id, error) {
  const element = byId(id);
  element.textContent = error?.message || String(error);
  element.hidden = false;
}

function clearError(id) {
  byId(id).textContent = '';
  byId(id).hidden = true;
}

function formatTime(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'}).format(date);
}

function memberFor(id) {
  return chatState.members.get(id) || {role: 'unknown', name: '未知成员'};
}

function roomButton(room) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = `room-item${chatState.room?.room_id === room.room_id ? ' active' : ''}`;
  button.setAttribute('aria-current', chatState.room?.room_id === room.room_id ? 'true' : 'false');
  const title = document.createElement('strong');
  title.textContent = room.title;
  const meta = document.createElement('small');
  meta.textContent = `#${room.room_id.slice(0, 8)} · ${formatTime(room.created_at)}`;
  button.append(title, meta);
  button.addEventListener('click', () => selectRoom(room));
  return button;
}

function renderRooms() {
  const list = byId('room-list');
  if (!chatState.rooms.length) {
    const empty = document.createElement('p');
    empty.className = 'empty-hint';
    empty.textContent = '还没有房间。输入话题，创建第一个聊天室。';
    list.replaceChildren(empty);
    return;
  }
  list.replaceChildren(...chatState.rooms.map(roomButton));
}

async function refreshRooms({selectInitial = false} = {}) {
  try {
    const page = await chatRequest(`${CHAT_API}?limit=100&offset=0`);
    chatState.rooms = page.items;
    renderRooms();
    if (selectInitial && page.items.length) {
      const requested = new URLSearchParams(window.location.search).get('room');
      await selectRoom(page.items.find((room) => room.room_id === requested) || page.items[0]);
    }
  } catch (error) {
    showError('create-room-error', error);
  }
}

async function createRoom(event) {
  event?.preventDefault();
  if (chatState.creating) return;
  const title = byId('room-title').value.trim();
  if (!title) { showError('create-room-error', new Error('请先填写房间话题。')); return; }
  clearError('create-room-error');
  chatState.creating = true;
  byId('create-room-button').disabled = true;
  chatState.pendingCreateKey ||= crypto.randomUUID();
  try {
    const room = await chatRequest(CHAT_API, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({title, idempotency_key: chatState.pendingCreateKey}),
    });
    chatState.pendingCreateKey = null;
    byId('room-title').value = '';
    await refreshRooms();
    await selectRoom(room);
  } catch (error) {
    showError('create-room-error', error);
  } finally {
    chatState.creating = false;
    byId('create-room-button').disabled = false;
  }
}

function renderMembers() {
  const badges = chatState.room.members.filter((member) => member.role !== 'human').map((member) => {
    const badge = document.createElement('span');
    badge.className = 'member';
    const name = document.createElement('strong');
    name.textContent = member.name;
    badge.append(window.CodeCrewAvatars.create(member.role, member.name), name, ` · ${member.role}`);
    return badge;
  });
  byId('member-list').replaceChildren(...badges);
}

function clearReply() {
  chatState.replyTo = null;
  byId('reply-preview').hidden = true;
  byId('reply-label').textContent = '';
}

function clearContextAnchor() {
  chatState.contextAnchorId = null;
  byId('context-preview').hidden = true;
  byId('context-label').textContent = '';
}

function setContextAnchor(message) {
  if (memberFor(message.sender_id).role !== 'human') return;
  clearReply();
  chatState.contextAnchorId = message.message_id;
  byId('context-label').textContent = `以这条 Human 消息为背景：${message.content.slice(0, 80)}`;
  byId('context-preview').hidden = false;
  byId('message-content').focus();
}

function setReply(message) {
  const author = memberFor(message.sender_id);
  if (author.role === 'human') return;
  clearContextAnchor();
  chatState.replyTo = message.message_id;
  byId('reply-label').textContent = `回复 ${author.name}：${message.content.slice(0, 80)}`;
  byId('reply-preview').hidden = false;
  byId('message-content').focus();
}

function resetCodingPreview() {
  chatState.codingVersion++;
  chatState.codingPreview = null;
  chatState.codingCommand = null;
  byId('coding-preview').hidden = true;
  byId('coding-confirm').checked = false;
  byId('coding-authorize').disabled = true;
  byId('coding-result').hidden = true;
  clearError('coding-error');
}

function closeCodingPanel() {
  chatState.codingSource = null;
  resetCodingPreview();
  byId('coding-panel').hidden = true;
}

function chooseCodingSource(message) {
  if (!chatState.codingCapability.available || memberFor(message.sender_id).role !== 'human') return;
  chatState.codingSource = message;
  resetCodingPreview();
  byId('coding-source').textContent = `来源：本房间 Human 消息 #${message.message_id.slice(0, 8)}。这条聊天消息不是授权。`;
  byId('coding-issue').value = chatState.codingCapability.demo_issue || message.content;
  byId('coding-scope').value = chatState.codingCapability.allowed_paths.join(', ');
  byId('coding-repository').value = chatState.codingCapability.demo_repository_path || '';
  byId('coding-repository').disabled = false;
  byId('coding-issue').disabled = false;
  byId('coding-preflight').disabled = false;
  byId('coding-panel').hidden = false;
  byId('coding-repository').focus();
}

async function loadCodingCapability() {
  try {
    const result = await chatRequest(`${CHAT_API}/coding-capability`);
    chatState.codingCapability = result.available ? result : {available: false, allowed_paths: []};
  } catch (_) {
    chatState.codingCapability = {available: false, allowed_paths: []};
  }
}

async function preflightCoding(event) {
  event?.preventDefault();
  if (!chatState.room || !chatState.codingSource || chatState.codingBusy) return;
  const roomId = chatState.room.room_id;
  const sourceId = chatState.codingSource.message_id;
  resetCodingPreview();
  const version = chatState.codingVersion;
  const draft = {
    source_message_id: sourceId,
    repository_path: byId('coding-repository').value.trim(),
    issue: byId('coding-issue').value.trim(),
    allowed_paths: chatState.codingCapability.allowed_paths,
  };
  if (!draft.repository_path || !draft.issue) {
    showError('coding-error', new Error('请填写仓库绝对路径和本次变更目标。'));
    return;
  }
  chatState.codingBusy = true;
  byId('coding-preflight').disabled = true;
  try {
    const preview = await chatRequest(`${CHAT_API}/${encodeURIComponent(roomId)}/coding-task-preflight`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(draft),
    });
    if (roomId !== chatState.room?.room_id || sourceId !== chatState.codingSource?.message_id
        || version !== chatState.codingVersion) return;
    if (preview.execution_authorized !== false || preview.task_created !== false) {
      throw new Error('预检返回了异常的授权状态，已停止操作。');
    }
    chatState.codingPreview = preview;
    byId('coding-summary').textContent = `仓库：${preview.repository_path}\n目标：${preview.issue}\n允许写入：${preview.allowed_paths.join(', ')}\nGit 基线：${preview.base_commit}\n预检通过，但尚未创建任务。`;
    byId('coding-preview').hidden = false;
  } catch (error) {
    if (roomId === chatState.room?.room_id) showError('coding-error', error);
  } finally {
    chatState.codingBusy = false;
    if (roomId === chatState.room?.room_id) byId('coding-preflight').disabled = false;
  }
}

async function authorizeCoding() {
  const preview = chatState.codingPreview;
  if (!preview || !byId('coding-confirm').checked || chatState.codingBusy) return;
  const roomId = chatState.room?.room_id;
  const sourceId = chatState.codingSource?.message_id;
  if (!roomId || preview.room_id !== roomId || preview.source_message_id !== sourceId) return;
  clearError('coding-error');
  chatState.codingCommand ||= {
    source_message_id: sourceId, repository_path: preview.repository_path,
    issue: preview.issue, allowed_paths: preview.allowed_paths,
    expected_base_commit: preview.base_commit,
    idempotency_key: crypto.randomUUID(), confirmation: 'authorize_one_coding_task',
  };
  chatState.codingBusy = true;
  byId('coding-authorize').disabled = true;
  byId('coding-preflight').disabled = true;
  byId('coding-repository').disabled = true;
  byId('coding-issue').disabled = true;
  try {
    const result = await chatRequest(`${CHAT_API}/${encodeURIComponent(roomId)}/coding-tasks`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(chatState.codingCommand),
    });
    if (roomId !== chatState.room?.room_id || sourceId !== chatState.codingSource?.message_id) return;
    if (result.execution_authorized !== true || result.task_created !== true) {
      throw new Error('服务端未确认任务创建，不能显示成功。');
    }
    byId('coding-result-text').textContent = `已创建受控任务 #${result.task_id.slice(0, 8)}；这不代表代码已完成。请到任务工作台查看测试、Review 和交付证据。`;
    byId('coding-task-link').href = `/ui/?task=${encodeURIComponent(result.task_id)}`;
    byId('coding-result').hidden = false;
    byId('coding-preview').hidden = true;
  } catch (error) {
    if (roomId === chatState.room?.room_id) {
      showError('coding-error', new Error(`${error.message}。结果不确定时请保持本面板不变，使用同一授权重试；不要重新创建授权。`));
    }
  } finally {
    chatState.codingBusy = false;
    if (roomId === chatState.room?.room_id && byId('coding-result').hidden) {
      byId('coding-authorize').disabled = false;
    }
  }
}

async function selectRoom(room) {
  if (!room) return;
  disconnectRoom();
  const requestId = ++chatState.requestId;
  chatState.room = room;
  chatState.members = new Map(room.members.map((member) => [member.member_id, member]));
  chatState.messages = new Map();
  chatState.lastSequence = 0;
  chatState.pendingSend = null;
  byId('message-content').value = '';
  clearReply();
  clearContextAnchor();
  clearError('conversation-error');
  clearError('send-error');
  byId('send-status').textContent = '提及 Agent 或回复其消息即可开始对话';
  closeCodingPanel();
  byId('empty-room').hidden = true;
  byId('active-room').hidden = false;
  byId('conversation-title').textContent = room.title;
  byId('room-status').textContent = room.status === 'active' ? '可开始聊天' : '已关闭';
  byId('message-content').disabled = room.status !== 'active';
  byId('send-button').disabled = room.status !== 'active';
  renderMembers();
  renderRooms();
  byId('message-list').replaceChildren();
  byId('turn-list').replaceChildren();
  const next = new URL(window.location.href);
  next.searchParams.set('room', room.room_id);
  window.history.replaceState(null, '', next);
  await refreshSelected(requestId);
  if (requestId === chatState.requestId) connectRoom(room.room_id, requestId);
}

function disconnectRoom() {
  if (chatState.eventSource) chatState.eventSource.close();
  chatState.eventSource = null;
}

function connectRoom(roomId, requestId) {
  if (typeof EventSource === 'undefined') {
    byId('room-subtitle').textContent = '只读讨论 · 每 10 秒检查新消息';
    return;
  }
  const source = new EventSource(`${CHAT_API}/${encodeURIComponent(roomId)}/events`);
  chatState.eventSource = source;
  source.addEventListener('open', () => {
    if (requestId === chatState.requestId) byId('room-subtitle').textContent = '实时连接中 · 只读讨论';
  });
  source.addEventListener('chat_changed', () => {
    if (requestId === chatState.requestId) refreshSelected(requestId);
  });
  source.addEventListener('error', () => {
    if (requestId === chatState.requestId) {
      byId('room-subtitle').textContent = '实时连接中断，低频轮询仍在运行';
    }
  });
}

function renderMessage(stored) {
  const message = stored.message;
  const author = memberFor(message.sender_id);
  const human = author.role === 'human';
  const row = document.createElement('article');
  row.className = `message${human ? ' human' : ''}`;
  row.dataset.messageId = message.message_id;
  const avatar = human ? document.createElement('span')
    : window.CodeCrewAvatars.create(author.role, author.name);
  if (human) {
    avatar.className = 'avatar human-avatar';
    avatar.textContent = '我';
    avatar.setAttribute('aria-hidden', 'true');
  }
  const body = document.createElement('div');
  body.className = 'message-body';
  const meta = document.createElement('div');
  meta.className = 'message-meta';
  const name = document.createElement('strong');
  name.textContent = human ? '我' : author.name;
  const role = document.createElement('span');
  role.textContent = human ? 'human' : author.role;
  const time = document.createElement('time');
  time.textContent = formatTime(message.created_at);
  meta.append(name, role, time);
  body.append(meta);
  if (message.reply_to) {
    const context = document.createElement('div');
    context.className = 'reply-context';
    const parent = chatState.messages.get(message.reply_to)?.message;
    context.textContent = parent ? `↳ ${memberFor(parent.sender_id).name}：${parent.content.slice(0, 60)}` : '↳ 关联上一条消息';
    body.append(context);
  }
  if (message.context_anchor_id) {
    const background = document.createElement('div');
    background.className = 'reply-context';
    const anchor = chatState.messages.get(message.context_anchor_id)?.message;
    background.textContent = anchor ? `背景 · 我：${anchor.content.slice(0, 60)}` : '背景 · 已引用 Human 消息';
    body.append(background);
  }
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = message.content;
  body.append(bubble);
  if (human) {
    if (chatState.codingCapability.available) {
      const coding = document.createElement('button');
      coding.type = 'button';
      coding.className = 'reply-action coding-action';
      coding.textContent = '以此发起受控编码任务 ↗';
      coding.addEventListener('click', () => chooseCodingSource(message));
      body.append(coding);
    }
    const anchor = document.createElement('button');
    anchor.type = 'button';
    anchor.className = 'reply-action';
    anchor.textContent = '以此为背景继续 ↗';
    anchor.addEventListener('click', () => setContextAnchor(message));
    body.append(anchor);
  } else {
    const reply = document.createElement('button');
    reply.type = 'button';
    reply.className = 'reply-action';
    reply.textContent = '回复这条消息 ↗';
    reply.addEventListener('click', () => setReply(message));
    body.append(reply);
  }
  row.append(avatar, body);
  return row;
}

function isCurrentRefresh(requestId, refreshId) {
  return requestId === chatState.requestId && refreshId === chatState.refreshId;
}

async function loadMessages(requestId, refreshId) {
  const roomId = chatState.room.room_id;
  let cursor = chatState.lastSequence;
  let added = 0;
  while (true) {
    const page = await chatRequest(`${CHAT_API}/${encodeURIComponent(roomId)}/messages?after_sequence=${cursor}&limit=100`);
    if (!isCurrentRefresh(requestId, refreshId)) return;
    const list = byId('message-list');
    for (const stored of page.items) {
      if (stored.sequence <= chatState.lastSequence) continue;
      chatState.messages.set(stored.message.message_id, stored);
      if (list.querySelector('.empty-hint')) list.replaceChildren();
      list.append(renderMessage(stored));
      chatState.lastSequence = stored.sequence;
      added++;
    }
    if (page.items.length < 100) break;
    cursor = page.items.at(-1).sequence;
  }
  if (chatState.lastSequence === 0) {
    const hint = document.createElement('p');
    hint.className = 'empty-hint';
    hint.textContent = '还没有消息。试试 @白金 请她先聊聊方案。';
    byId('message-list').replaceChildren(hint);
  } else if (added) {
    const list = byId('message-list');
    list.scrollTop = list.scrollHeight;
  }
}

function renderTurn(turn) {
  const item = document.createElement('div');
  item.className = 'turn-item';
  const member = memberFor(turn.recipient_id);
  const head = document.createElement('div');
  head.className = 'turn-item-head';
  const name = document.createElement('strong');
  name.textContent = member.name;
  const state = document.createElement('span');
  state.className = `turn-state ${turn.status}`;
  state.textContent = TURN_LABELS[turn.status] || turn.status;
  head.append(name, state);
  const detail = document.createElement('p');
  detail.textContent = `关联消息 #${turn.message_id.slice(0, 8)} · ${formatTime(turn.updated_at)}`;
  item.append(head, detail);
  if (turn.error) {
    const error = document.createElement('p');
    error.textContent = turn.error;
    item.append(error);
  }
  if (turn.status === 'running' || turn.status === 'queued') {
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.textContent = '请求取消';
    cancel.addEventListener('click', async () => {
      cancel.disabled = true;
      try {
        await chatRequest(`${CHAT_API}/${encodeURIComponent(turn.room_id)}/turns/${encodeURIComponent(turn.turn_id)}/cancel`, {method: 'POST'});
        if (chatState.room?.room_id === turn.room_id) await refreshSelected(chatState.requestId);
      } catch (error) { showError('conversation-error', error); cancel.disabled = false; }
    });
    item.append(cancel);
  }
  return item;
}

async function loadTurns(requestId, refreshId) {
  const roomId = chatState.room.room_id;
  const page = await chatRequest(`${CHAT_API}/${encodeURIComponent(roomId)}/turns`);
  if (!isCurrentRefresh(requestId, refreshId)) return;
  const list = byId('turn-list');
  const active = page.items.filter((turn) => turn.status === 'queued' || turn.status === 'running');
  const latest = page.items.at(-1);
  if (active.length) {
    byId('room-status').textContent = `${memberFor(active[0].recipient_id).name}正在回复`;
    if (byId('send-error').hidden) byId('send-status').textContent = 'Agent 正在处理，只读讨论不会改代码。';
  } else if (latest && ['failed', 'interrupted', 'budget_exhausted', 'cancelled'].includes(latest.status)) {
    byId('room-status').textContent = TURN_LABELS[latest.status];
    if (byId('send-error').hidden) byId('send-status').textContent = '此回合未成功；可查看团队动态并重新提问。';
  } else {
    byId('room-status').textContent = chatState.room.status !== 'active' ? '已关闭'
      : latest?.status === 'succeeded' ? '已回复' : '可开始聊天';
    if (latest?.status === 'succeeded' && byId('send-error').hidden) {
      byId('send-status').textContent = 'Agent 已回复，可以继续讨论。';
    }
  }
  if (!page.items.length) {
    const hint = document.createElement('p');
    hint.className = 'empty-hint';
    hint.textContent = '暂无 Agent 回合。';
    list.replaceChildren(hint);
    return;
  }
  list.replaceChildren(...page.items.slice(-12).reverse().map(renderTurn));
}

async function refreshSelected(requestId = chatState.requestId) {
  if (!chatState.room) return;
  const refreshId = ++chatState.refreshId;
  try {
    await loadMessages(requestId, refreshId);
    if (isCurrentRefresh(requestId, refreshId)) await loadTurns(requestId, refreshId);
    if (isCurrentRefresh(requestId, refreshId)) clearError('conversation-error');
  } catch (error) {
    if (isCurrentRefresh(requestId, refreshId)) showError('conversation-error', error);
  }
}

function insertMention(mention) {
  const field = byId('message-content');
  const at = field.selectionStart ?? field.value.length;
  field.value = `${field.value.slice(0, at)}${mention} ${field.value.slice(at)}`;
  field.focus();
  field.setSelectionRange(at + mention.length + 1, at + mention.length + 1);
}

async function sendMessage(event) {
  event?.preventDefault();
  if (!chatState.room || chatState.sending) return;
  const roomId = chatState.room.room_id;
  const content = byId('message-content').value.trim();
  const replyTo = chatState.replyTo;
  const contextAnchorId = chatState.contextAnchorId;
  if (!content) { showError('send-error', new Error('请先输入消息。')); return; }
  if (!/@[\w\u4e00-\u9fff]+/u.test(content) && !replyTo) {
    showError('send-error', new Error('请提及一位 Agent，或回复一条 Agent 消息。'));
    return;
  }
  clearError('send-error');
  const pending = chatState.pendingSend;
  const key = pending && pending.roomId === roomId && pending.content === content
    && pending.replyTo === replyTo && pending.contextAnchorId === contextAnchorId
    ? pending.key : crypto.randomUUID();
  chatState.pendingSend = {roomId, content, replyTo, contextAnchorId, key};
  chatState.sending = true;
  byId('send-button').disabled = true;
  byId('send-status').textContent = '正在发送…';
  try {
    const receipt = await chatRequest(`${CHAT_API}/${encodeURIComponent(roomId)}/messages`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({content, reply_to: replyTo, context_anchor_id: contextAnchorId,
        idempotency_key: key}),
    });
    if (chatState.room?.room_id === roomId) {
      byId('message-content').value = '';
      clearReply();
      clearContextAnchor();
      byId('send-status').textContent = receipt.discussion_queued ? '消息已送达，等待 Agent 回复。' : '消息已保存。';
      await refreshSelected(chatState.requestId);
    }
    chatState.pendingSend = null;
  } catch (error) {
    if (chatState.room?.room_id === roomId) {
      showError('send-error', error);
      byId('send-status').textContent = '内容未清除；如需重试请保持原文。';
    }
  } finally {
    chatState.sending = false;
    if (chatState.room?.room_id === roomId) byId('send-button').disabled = chatState.room.status !== 'active';
  }
}

function initializeChat() {
  byId('coding-close').addEventListener('click', closeCodingPanel);
  byId('coding-form').addEventListener('submit', preflightCoding);
  byId('coding-confirm').addEventListener('change', () => {
    byId('coding-authorize').disabled = !byId('coding-confirm').checked || chatState.codingBusy;
  });
  byId('coding-authorize').addEventListener('click', authorizeCoding);
  for (const id of ['coding-repository', 'coding-issue']) {
    byId(id).addEventListener('input', resetCodingPreview);
  }
  byId('create-room-form').addEventListener('submit', createRoom);
  byId('room-title').addEventListener('input', () => { chatState.pendingCreateKey = null; clearError('create-room-error'); });
  byId('refresh-rooms').addEventListener('click', () => refreshRooms());
  byId('refresh-chat').addEventListener('click', () => refreshSelected());
  byId('message-form').addEventListener('submit', sendMessage);
  byId('message-content').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) sendMessage(event);
  });
  byId('message-content').addEventListener('input', () => clearError('send-error'));
  byId('clear-reply').addEventListener('click', clearReply);
  byId('clear-context').addEventListener('click', clearContextAnchor);
  document.querySelectorAll('[data-mention]').forEach((button) => {
    button.addEventListener('click', () => insertMention(button.dataset.mention));
  });
  void loadCodingCapability().then(() => refreshRooms({selectInitial: true}));
  window.setInterval(() => refreshSelected(), 10000);
  window.addEventListener('beforeunload', disconnectRoom);
}

initializeChat();
