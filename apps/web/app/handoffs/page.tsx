"use client";

import { useState } from "react";
import { CheckCircle2, Inbox, XCircle } from "lucide-react";
import { PageHeading, formatTime } from "../../components/shell";
import { ConfirmDialog, EmptyState, LoadingSkeleton, Metric, Modal, Panel, StatusPill } from "../../components/ui";
import { Handoff, acceptHandoff, errorMessage, rejectHandoff } from "../../lib/api";
import { useWorkspace } from "../../lib/workspace";

export default function HandoffsPage() {
  const { reviewCenter, notify, refresh, loading } = useWorkspace();
  const handoffs = reviewCenter.handoffs;
  const [busy, setBusy] = useState("");
  const [confirmAccept, setConfirmAccept] = useState<{ handoff: Handoff; receiver: string } | null>(null);
  const [rejectTarget, setRejectTarget] = useState<{ handoff: Handoff; receiver: string } | null>(null);
  const [reason, setReason] = useState("");

  const handleAccept = async (handoff: Handoff) => {
    setBusy(handoff.id);
    try {
      await acceptHandoff(handoff.id);
      await refresh();
      notify("已接受交接：可以进入下游");
    } catch (error) {
      // 例如"这份交接不是发给你的"——服务端按收据行校验身份，原因如实展示。
      notify(errorMessage(error, "交接接受失败"));
    } finally {
      setBusy("");
      setConfirmAccept(null);
    }
  };

  const handleReject = async (handoff: Handoff) => {
    if (!reason.trim()) {
      notify("请填写拒绝原因（会进入复核记录）");
      return;
    }
    setBusy(handoff.id);
    try {
      await rejectHandoff(handoff.id, reason.trim());
      await refresh();
      notify("已拒绝交接：发送方需要修订");
      setReason("");
    } catch (error) {
      notify(errorMessage(error, "交接拒绝失败"));
    } finally {
      setBusy("");
      setRejectTarget(null);
    }
  };

  return (
    <div className="page-content" id="handoffs">
      <PageHeading hint="接力与分发交接：逐接收方收据，拒绝即返工（接受/拒绝由接收方提交）" />
      <section className="metrics-grid">
        <Metric label="交接总数" value={handoffs.length} detail="含接力与分发" />
        <Metric label="已接受" value={handoffs.filter((item) => item.receipt_status === "ACCEPTED").length} detail="可进入下游" tone="positive" />
        <Metric label="待确认" value={handoffs.filter((item) => item.receipt_status === "PENDING").length} detail="等待接收方" tone="warning" />
        <Metric label="已拒绝" value={handoffs.filter((item) => item.receipt_status === "REJECTED").length} detail="需要返工" tone="warning" />
      </section>
      <Panel title="交接收据" subtitle="Fanout 必须等待所有接收方确认" testId="handoff-list">
        <div className="list">
          {handoffs.map((handoff) => (
            <div className="list-item list-item-static" key={handoff.id} data-testid={`handoff-${handoff.id}`}>
              <span className="icon-tile"><Inbox size={15} /></span>
              <div className="item-copy">
                <strong>{handoff.handoff_type} · {handoff.sender_agent_id}</strong>
                <small>{handoff.objective} · {formatTime(handoff.created_at)}</small>
                {handoff.decision_reason && <small>拒绝原因：{handoff.decision_reason}</small>}
                <div className="chips">
                  {handoff.receipts.map((receipt) => (
                    <span
                      className={`chip ${receipt.status === "ACCEPTED" ? "status-green" : receipt.status === "REJECTED" ? "status-red" : "status-amber"}`}
                      key={receipt.id}
                    >
                      {receipt.receiver_id} · {receipt.status}
                    </span>
                  ))}
                </div>
              </div>
              <div className="list-actions">
                <StatusPill status={String(handoff.receipt_status)} />
                {/* 逐条待确认收据给入口：身份由服务端按收据行校验，点错会明确报"不是发给你的" */}
                {handoff.receipts.filter((receipt) => receipt.status === "PENDING").map((receipt) => (
                  <div className="form-row" key={`actions-${receipt.id}`} style={{ justifyContent: "flex-end" }}>
                    <button
                      className="text-button"
                      disabled={busy === handoff.id}
                      title={`以 ${receipt.receiver_id} 身份接受`}
                      data-testid={`handoff-accept-${handoff.id}-${receipt.receiver_id}`}
                      onClick={() => setConfirmAccept({ handoff, receiver: receipt.receiver_id })}
                    >
                      <CheckCircle2 size={13} /> 接受
                    </button>
                    <button
                      className="text-button"
                      disabled={busy === handoff.id}
                      title={`以 ${receipt.receiver_id} 身份拒绝`}
                      data-testid={`handoff-reject-${handoff.id}-${receipt.receiver_id}`}
                      onClick={() => { setRejectTarget({ handoff, receiver: receipt.receiver_id }); setReason(""); }}
                    >
                      <XCircle size={13} /> 拒绝
                    </button>
                  </div>
                ))}
              </div>
            </div>
          ))}
          {loading ? <LoadingSkeleton rows={3} label="正在加载交接" /> : !handoffs.length && <EmptyState>暂无交接记录</EmptyState>}
        </div>
      </Panel>

      {confirmAccept && (
        <ConfirmDialog
          title="接受这份交接？"
          description={
            <>
              <strong>{confirmAccept.handoff.handoff_type} · 来自 {confirmAccept.handoff.sender_agent_id}</strong>
              <div style={{ marginTop: 6 }}>{confirmAccept.handoff.objective}</div>
              <div className="hint" style={{ marginTop: 6 }}>
                以 <code>{confirmAccept.receiver}</code> 身份接受。接受后该收据变为 ACCEPTED，交接可进入下游；Fanout 需要所有接收方都确认。
              </div>
            </>
          }
          confirmLabel="接受交接"
          busy={busy === confirmAccept.handoff.id}
          testId="handoff-accept-confirm"
          onCancel={() => setConfirmAccept(null)}
          onConfirm={() => void handleAccept(confirmAccept.handoff)}
        />
      )}

      {rejectTarget && (
        <Modal
          title="拒绝这份交接"
          subtitle={`以 ${rejectTarget.receiver} 身份拒绝 · 发送方需要修订后重新提交`}
          testId="handoff-reject-modal"
          onClose={() => setRejectTarget(null)}
          actions={
            <>
              <button className="button button-secondary" onClick={() => setRejectTarget(null)}>取消</button>
              <button
                className="button button-danger"
                disabled={busy === rejectTarget.handoff.id || !reason.trim()}
                data-testid="handoff-reject-submit"
                onClick={() => void handleReject(rejectTarget.handoff)}
              >
                拒绝并退回
              </button>
            </>
          }
        >
          <label className="field"><span>拒绝原因（必填，会进入复核记录）</span>
            <textarea
              rows={3}
              value={reason}
              data-testid="handoff-reject-reason"
              placeholder="例如：缺少问题三的消融结果，无法作为下游输入"
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
        </Modal>
      )}
    </div>
  );
}