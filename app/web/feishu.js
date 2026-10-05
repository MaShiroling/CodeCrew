'use strict';

async function refreshFeishuStatus() {
  const element = document.getElementById('feishu-status');
  if (!element) return;
  try {
    const response = await fetch('/api/v1/feishu/status');
    if (!response.ok) throw new Error('unavailable');
    const status = await response.json();
    element.hidden = !status.enabled;
    if (!status.enabled) return;
    const labels = {starting: '连接中', connected: '已连接', reconnecting: '重连中',
      failed: '连接失败', stopped: '已停止', disabled: '未启用'};
    element.textContent = `飞书 · ${labels[status.connection_state] || '状态未知'} · 待投递 ${status.pending_outbox_count} · 重试 ${status.retry_count} · 失败 ${status.failed_count}`;
  } catch (_error) {
    if (!element.hidden) element.textContent = '飞书 · 状态暂不可用';
  }
}

void refreshFeishuStatus();
window.setInterval(refreshFeishuStatus, 10000);
