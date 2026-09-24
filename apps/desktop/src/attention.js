// 关注清单的通知判定（DESKTOP-NOTIFY）
//
// 纯函数，便于单测：主进程只负责"拉清单、交给这里判定、把结果变成系统通知"。
// 三条规则（都在"降噪"和"别漏"之间取平衡）：
//   1. 第一次见到的条目 → 通知（派给我 / 待我复核）；
//   2. 状态变成 NEEDS_REVISION（退回给我改）→ 再通知一次：那是新的一次"要你动手"；
//   3. 其余状态变化（READY→CLAIMED→RUNNING）不通知：机器在干活，不该吵人；
//      处理完消失的条目不通知（"没事了"不值得打扰）。
'use strict';

/** 条目 → 去重键。状态参与键，才能让"退回给我改"再报一次而不重复报普通流转。 */
function attentionKey(item) {
  const id = String(item?.task_id || '');
  const status = String(item?.status || '');
  // 只有 NEEDS_REVISION 把状态并进键里；其他状态一律按 id 去重（避免流转刷屏）
  return status === 'NEEDS_REVISION' ? `${id}#NEEDS_REVISION` : id;
}

function listOf(payload, name) {
  const value = payload && Array.isArray(payload[name]) ? payload[name] : [];
  return value;
}

/** 把一条关注项渲染成通知（标题/正文/点击打开的站内路径）。 */
function renderNotification(item) {
  const title = String(item?.title || '未命名任务');
  const project = String(item?.project_name || '');
  const suffix = project ? `（${project}）` : '';
  if (item?.kind === 'review') {
    return {
      title: '待你复核',
      body: `${title}${suffix}`,
      url: `/tasks?task=${encodeURIComponent(String(item.task_id))}`,
      kind: 'review',
    };
  }
  const extra = item?.deadline_passed ? ' · 已过期' : '';
  return {
    title: item?.status === 'NEEDS_REVISION' ? '任务被退回，等你处理' : '有任务派给你',
    body: `${title}${suffix}${extra}`,
    url: `/tasks?task=${encodeURIComponent(String(item.task_id))}`,
    kind: 'assigned',
  };
}

/**
 * 对比"上次见过的键"与"这次拉到的清单"，返回要发的通知。
 *
 * @param {{seen?: string[]}} state 上次的已见键（调用方持久化）
 * @param {object} payload `/api/my-attention` 的响应
 * @returns {{notifications: object[], seen: string[], assigned_total: number, review_total: number}}
 */
function diffAttention(state, payload) {
  const previous = new Set(Array.isArray(state?.seen) ? state.seen : []);
  const notifications = [];
  const seen = [];
  const items = [...listOf(payload, 'assigned'), ...listOf(payload, 'review_pending')];
  for (const item of items) {
    const key = attentionKey(item);
    if (!key) continue;
    seen.push(key);
    if (!previous.has(key)) notifications.push(renderNotification(item));
  }
  return {
    notifications,
    // 保留最近 300 条：清单不会无限增长，也不会因为一次全清就丢掉"见过"的记忆
    seen: seen.slice(-300),
    assigned_total: Number(payload?.assigned_total ?? listOf(payload, 'assigned').length),
    review_total: Number(payload?.review_total ?? listOf(payload, 'review_pending').length),
  };
}

module.exports = { attentionKey, renderNotification, diffAttention };