"use client";

/**
 * 文档协作编辑（阶段 7：多人同时编辑分析报告）。
 *
 * 用 Yjs 的 CRDT 文本作为事实来源，通过平台已有的项目 WebSocket 中继增量更新：
 * 服务端只做「同项目、按文档分房间、不回声」的转发，收敛完全由 Yjs 保证，
 * 因此两个客户端并发编辑同一段落也不会互相覆盖。
 */

import * as Y from "yjs";

type FrameHandler = (frame: { type: string; document_id: string; update?: string; peer?: string; state?: string }) => void;

export type CollaborativeDocument = {
  readonly doc: Y.Doc;
  readonly text: Y.Text;
  readonly peerCount: number;
  applyRemote(frame: { update?: string }): void;
  insert(index: number, content: string): void;
  delete(index: number, length: number): void;
  getText(): string;
  peerId: string;
  destroy(): void;
};

export type CollaborationOptions = {
  projectId: string;
  documentId: string;
  wsUrl: string;
  initialText?: string;
  onFrame?: FrameHandler;
  onStatus?: (status: { connected: boolean; peerCount: number }) => void;
};

function toBase64(bytes: Uint8Array): string {
  let binary = "";
  bytes.forEach((value) => {
    binary += String.fromCharCode(value);
  });
  return btoa(binary);
}

function fromBase64(value: string): Uint8Array {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

export class WebSocketCollaboration implements CollaborativeDocument {
  readonly doc: Y.Doc;
  readonly text: Y.Text;
  readonly peerId: string;

  private socket: WebSocket | null = null;
  private peers = new Set<string>();
  private options: CollaborationOptions;
  private seeded = false;

  constructor(options: CollaborationOptions) {
    this.options = options;
    this.doc = new Y.Doc();
    this.text = this.doc.getText("document");
    this.peerId = `peer-${Math.random().toString(36).slice(2, 10)}`;

    // 本地编辑 → 通过中继广播增量更新。
    this.doc.on("update", (update: Uint8Array, origin: unknown) => {
      if (origin === "remote") return;
      this.send({
        type: "document.update",
        document_id: options.documentId,
        update: toBase64(update),
      });
    });

    if (options.initialText) {
      this.seed(options.initialText);
    }
    this.connect();
  }

  private seed(content: string) {
    if (this.seeded || content.length === 0) return;
    this.doc.transact(() => {
      this.text.insert(0, content);
    }, "seed");
    this.seeded = true;
  }

  private connect() {
    if (typeof window === "undefined") return;
    const socket = new WebSocket(this.options.wsUrl);
    this.socket = socket;
    socket.onopen = () => {
      this.options.onStatus?.({ connected: true, peerCount: this.peerCount });
      this.send({ type: "document.presence", document_id: this.options.documentId, peer: this.peerId, state: "active" });
      // 新加入者用状态向量换取对端缺失的更新（Yjs 同步第二步）。
      this.send({ type: "document.presence", document_id: this.options.documentId, peer: this.peerId, state: "sync-request" });
    };
    socket.onclose = () => this.options.onStatus?.({ connected: false, peerCount: this.peerCount });
    socket.onmessage = (message) => {
      try {
        const frame = JSON.parse(String(message.data));
        if (!frame || frame.document_id !== this.options.documentId) return;
        if (frame.type === "document.presence") {
          if (frame.peer && frame.peer !== this.peerId && frame.state === "active") {
            this.peers.add(frame.peer);
            this.options.onStatus?.({ connected: true, peerCount: this.peerCount });
          }
          return;
        }
        if (frame.type === "document.update" && frame.update) {
          this.applyRemote(frame);
          this.options.onFrame?.(frame);
        }
      } catch {
        // 非法帧忽略：协作失败不应影响本地编辑。
      }
    };
  }

  private send(frame: Record<string, unknown>) {
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(frame));
    }
  }

  applyRemote(frame: { update?: string }) {
    if (!frame.update) return;
    Y.applyUpdate(this.doc, fromBase64(frame.update), "remote");
  }

  insert(index: number, content: string) {
    this.text.insert(index, content);
  }

  delete(index: number, length: number) {
    this.text.delete(index, length);
  }

  getText(): string {
    return this.text.toString();
  }

  get peerCount(): number {
    return this.peers.size + 1;
  }

  /** 应用到本地 Y.Doc 的更新编码（测试与离线合并使用）。 */
  encodeStateAsUpdate(): Uint8Array {
    return Y.encodeStateAsUpdate(this.doc);
  }

  destroy() {
    this.socket?.close();
    this.socket = null;
    this.doc.destroy();
  }
}

export function createCollaborativeDocument(options: CollaborationOptions): CollaborativeDocument {
  return new WebSocketCollaboration(options);
}