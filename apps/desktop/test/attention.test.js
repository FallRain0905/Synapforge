// 关注清单通知判定的单测（DESKTOP-NOTIFY）
//
//   node --test apps/desktop/test/attention.test.js
//
// 只测纯逻辑（不碰 Electron、不碰网络）：去重、退回重报、状态流转不刷屏、点击路径。
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { attentionKey, renderNotification, diffAttention } = require('../src/attention.js');

const assigned = (id, title, extra = {}) => ({
  task_id: id,
  title,
  project_name: '演示 · 多 Agent 文档撰写',
  status: extra.status || 'READY',
  kind: 'assigned',
  deadline_passed: Boolean(extra.deadline_passed),
});

const review = (id, title) => ({
  task_id: id,
  title,
  project_name: '演示项目',
  status: 'WAITING_REVIEW',
  kind: 'review',
});

test('首见即通知，重复拉取不重复通知', () => {
  const payload = { assigned: [assigned('t1', '写初稿')], review_pending: [], assigned_total: 1, review_total: 0 };
  const first = diffAttention({ seen: [] }, payload);
  assert.equal(first.notifications.length, 1);
  assert.equal(first.notifications[0].title, '有任务派给你');
  assert.match(first.notifications[0].body, /写初稿/);
  assert.equal(first.notifications[0].url, '/tasks?task=t1');

  const second = diffAttention({ seen: first.seen }, payload);
  assert.equal(second.notifications.length, 0, '同一批清单第二次拉取不该再通知');
});

test('退回给我改（NEEDS_REVISION）会再通知一次', () => {
  const ready = diffAttention({ seen: [] }, { assigned: [assigned('t2', '改一下')] });
  const returned = diffAttention(
    { seen: ready.seen },
    { assigned: [assigned('t2', '改一下', { status: 'NEEDS_REVISION' })] },
  );
  assert.equal(returned.notifications.length, 1);
  assert.equal(returned.notifications[0].title, '任务被退回，等你处理');
  // 再拉一次同样的状态不再重复
  const again = diffAttention({ seen: returned.seen }, { assigned: [assigned('t2', '改一下', { status: 'NEEDS_REVISION' })] });
  assert.equal(again.notifications.length, 0);
});

test('普通流转（READY→CLAIMED→RUNNING→WAITING_REVIEW）不刷屏', () => {
  let seen = diffAttention({ seen: [] }, { assigned: [assigned('t3', '机器在跑', { status: 'READY' })] }).seen;
  for (const status of ['CLAIMED', 'RUNNING', 'WAITING_REVIEW', 'BLOCKED']) {
    const step = diffAttention({ seen }, { assigned: [assigned('t3', '机器在跑', { status })] });
    assert.equal(step.notifications.length, 0, `${status} 不该发通知`);
    seen = step.seen;
  }
});

test('待复核的通知与点击路径', () => {
  const result = diffAttention({ seen: [] }, { review_pending: [review('t9', '复核初稿')], review_total: 1 });
  assert.equal(result.notifications.length, 1);
  assert.equal(result.notifications[0].title, '待你复核');
  assert.match(result.notifications[0].body, /复核初稿（演示项目）/);
  assert.equal(result.notifications[0].url, '/tasks?task=t9');
  assert.equal(result.review_total, 1);
});

test('条目消失（处理完了）不通知', () => {
  const before = diffAttention({ seen: [] }, { assigned: [assigned('t4', '做完了')] });
  const after = diffAttention({ seen: before.seen }, { assigned: [] });
  assert.equal(after.notifications.length, 0);
});

test('中文标题与过期标记进正文；任务 id 会 URL 编码', () => {
  const item = assigned('任务/带斜杠', '交一篇「指南」', { deadline_passed: true });
  const rendered = renderNotification(item);
  assert.equal(rendered.body, '交一篇「指南」（演示 · 多 Agent 文档撰写） · 已过期');
  assert.equal(rendered.url, '/tasks?task=%E4%BB%BB%E5%8A%A1%2F%E5%B8%A6%E6%96%9C%E6%9D%A0');
  assert.equal(attentionKey(item), '任务/带斜杠');
});

test('已见键只保留最近 300 条', () => {
  const many = Array.from({ length: 350 }, (_, index) => assigned(`t${index}`, `任务 ${index}`));
  const result = diffAttention({ seen: [] }, { assigned: many });
  assert.equal(result.seen.length, 300);
  assert.equal(result.notifications.length, 350, '本次仍然逐条通知（首见）');
});

test('坏输入不炸：缺字段、非数组、空 payload', () => {
  assert.deepEqual(diffAttention({ seen: [] }, {}).notifications, []);
  assert.deepEqual(diffAttention({ seen: null }, { assigned: 'nope' }).notifications, []);
  assert.deepEqual(diffAttention(undefined, undefined).notifications, []);
  assert.equal(attentionKey({}), '');
});