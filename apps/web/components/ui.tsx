"use client";

import { AlertTriangle, ArrowUpRight, CheckCircle2, RefreshCcw, X } from "lucide-react";
import { useEffect, useId, useRef, type ReactNode } from "react";

/** 复用度最高的小组件：卡片、指标、徽标、进度、弹窗、空状态。 */

export function Panel({
  title,
  subtitle,
  actions,
  children,
  testId,
  footer,
}: {
  title?: string;
  subtitle?: string;
  actions?: ReactNode;
  children: ReactNode;
  testId?: string;
  footer?: ReactNode;
}) {
  return (
    <section className="panel" data-testid={testId}>
      {(title || actions) && (
        <div className="panel-heading">
          <div>
            {title && <h2>{title}</h2>}
            {subtitle && <p>{subtitle}</p>}
          </div>
          {actions && <div className="panel-heading-actions">{actions}</div>}
        </div>
      )}
      {children}
      {footer && <div>{footer}</div>}
    </section>
  );
}

export function Metric({ label, value, detail, tone = "default" }: { label: string; value: string | number; detail: string; tone?: "default" | "warning" | "positive" }) {
  const toneClass = tone === "warning" ? "metric-warning" : tone === "positive" ? "metric-positive" : "";
  return (
    <div className={`metric ${toneClass}`.trim()}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

const statusTone: Record<string, string> = {
  APPROVED: "status-green",
  PASSED: "status-green",
  SUCCEEDED: "status-green",
  ACCEPTED: "status-green",
  RUNNING: "status-blue",
  CLAIMED: "status-blue",
  READY: "status-blue",
  PROCESSING: "status-blue",
  WAITING_REVIEW: "status-amber",
  PENDING_REVIEW: "status-amber",
  PENDING: "status-amber",
  NEEDS_REVISION: "status-amber",
  BLOCKED: "status-red",
  FAILED: "status-red",
  REJECTED: "status-red",
  CANCELLED: "status-neutral",
  DRAFT: "status-neutral",
};

export function StatusPill({ status, label }: { status: string; label?: string }) {
  return (
    <span className={`status-pill ${statusTone[status] ?? "status-neutral"}`}>
      <span className="status-bullet" />
      {label ?? status}
    </span>
  );
}

export function Progress({ label, value, detail }: { label: string; value: number; detail?: string }) {
  const percent = Math.max(0, Math.min(100, Math.round(value)));
  return (
    <div>
      <div className="progress-copy">
        <span>{label}</span>
        <strong>{percent}%</strong>
      </div>
      <div className="progress-track"><span style={{ width: `${percent}%` }} /></div>
      {detail && <div className="hint" style={{ marginTop: 6 }}>{detail}</div>}
    </div>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="empty-state">{children}</div>;
}

/** 数据未到位时的占位，避免把"还没加载"显示成"暂无数据"。 */
export function LoadingSkeleton({ rows = 3, label = "正在加载…" }: { rows?: number; label?: string }) {
  return (
    <div className="skeleton" data-testid="loading-skeleton" aria-busy="true" aria-label={label}>
      {Array.from({ length: rows }).map((_, index) => (
        <div className="skeleton-row" key={index}>
          <span className="skeleton-tile" />
          <span className="skeleton-lines">
            <span className="skeleton-bar" style={{ width: `${72 - index * 9}%` }} />
            <span className="skeleton-bar skeleton-bar-thin" style={{ width: `${44 - index * 5}%` }} />
          </span>
        </div>
      ))}
    </div>
  );
}

export function Modal({
  title,
  subtitle,
  onClose,
  children,
  actions,
  wide = false,
  testId,
}: {
  title: string;
  subtitle?: string;
  onClose: () => void;
  children: ReactNode;
  actions?: ReactNode;
  wide?: boolean;
  testId?: string;
}) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const onCloseRef = useRef(onClose);
  const titleId = useId();

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(
        dialogRef.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      );
      if (!focusable.length) {
        event.preventDefault();
        dialogRef.current.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus();
    };
  }, []);

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <div
        ref={dialogRef}
        className={`modal ${wide ? "modal-wide" : ""}`.trim()}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        data-testid={testId}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="modal-heading">
          <div>
            <h2 id={titleId}>{title}</h2>
            {subtitle && <p>{subtitle}</p>}
          </div>
          <button ref={closeRef} className="app-icon" aria-label="关闭" onClick={onClose}><X size={17} /></button>
        </div>
        {children}
        {actions && <div className="modal-actions">{actions}</div>}
      </div>
    </div>
  );
}

/** 危险动作的二次确认：在 Modal 之上包一层语义与默认焦点。 */
export function ConfirmDialog({
  title,
  description,
  confirmLabel = "确认",
  cancelLabel = "取消",
  tone = "default",
  busy = false,
  onConfirm,
  onCancel,
  testId,
}: {
  title: string;
  description: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "default" | "danger";
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  testId?: string;
}) {
  return (
    <Modal
      title={title}
      onClose={onCancel}
      testId={testId ?? "confirm-dialog"}
      actions={
        <>
          <button className="button button-secondary" onClick={onCancel} disabled={busy}>{cancelLabel}</button>
          <button
            className={`button ${tone === "danger" ? "button-danger" : "button-primary"}`}
            onClick={onConfirm}
            disabled={busy}
            data-testid="confirm-accept"
          >
            {busy ? <RefreshCcw size={15} className="spin" /> : <AlertTriangle size={15} />} {confirmLabel}
          </button>
        </>
      }
    >
      <div className="confirm-body">{description}</div>
    </Modal>
  );
}

export function FindingList({ findings }: { findings: { severity: string; code: string; subject?: string; message: string }[] }) {
  if (!findings.length) {
    return (
      <div className="empty-state" style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 8 }}>
        <CheckCircle2 size={16} /> 未发现问题
      </div>
    );
  }
  return (
    <div className="list">
      {findings.map((finding) => (
        <div className="list-item list-item-static" key={`${finding.code}-${finding.subject ?? ""}-${finding.message}`}>
          <span className={`risk-severity risk-severity-${finding.severity}`}><AlertTriangle size={14} /></span>
          <div className="item-copy">
            <strong>{finding.code}<span className={`risk-label risk-label-${finding.severity}`} style={{ marginLeft: 8 }}>{finding.severity}</span></strong>
            <small>{finding.message}</small>
            {finding.subject && <small>{finding.subject}</small>}
          </div>
        </div>
      ))}
    </div>
  );
}

export function LinkOut({ children }: { children: ReactNode }) {
  return <span className="text-button">{children} <ArrowUpRight size={14} /></span>;
}

export { statusTone };