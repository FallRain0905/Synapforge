"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { BookOpen, MessageSquare, Plus, RefreshCcw, Send, Trash2 } from "lucide-react";
import Link from "next/link";
import { PageHeading } from "../../components/shell";
import { ConfirmDialog, EmptyState, Panel } from "../../components/ui";
import {
  Conversation,
  KnowledgeBase,
  appendMessage,
  askAiChat,
  createConversation,
  deleteConversation,
  listConversations,
  listKbs,
  queryKb,
} from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { useWorkspace } from "../../lib/workspace";

type Message = { id: string; role: "user" | "assistant"; content: string; sources: Record<string, unknown>[]; via?: "channel" | "own_key" };

const MODE_LABEL: Record<string, string> = { chat: "对话", rag: "检索" };

/**
 * AI 问答（W-7 重构）：像 ChatGPT 网页端那样——主区域是对话窗口（铺满、消息内滚、输入区钉底），
 * 右侧是会话管理（新建/切换/删除、绑定知识库）。此前会话列表在宽栏、聊天在窄栏，是"倒的"。
 *
 * 未选择会话时直接输入即可：自动新建一个「对话」会话再发送（检索会话需要绑定知识库，须显式新建）。
 */
export default function AskPage() {
  const { notify } = useWorkspace();
  const { ready, authenticated } = useAuth();
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [ragKbId, setRagKbId] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; title: string } | null>(null);
  const chatEnd = useRef<HTMLDivElement | null>(null);
  const inputBox = useRef<HTMLTextAreaElement | null>(null);

  const loadConversations = useCallback(
    async (preferredId?: string) => {
      try {
        const list = await listConversations();
        setConversations(list);
        if (!preferredId) {
          const current = list.find((item) => item.id === selectedId);
          if (current?.messages) {
            setMessages(
              current.messages.map((message) => ({
                id: message.id,
                role: message.role as "user" | "assistant",
                content: message.content,
                sources: message.sources,
              })),
            );
          }
        }
        // 带 preferredId 时只刷新侧栏列表、不动消息区：
        // 自动建会话的流程里，乐观插入的用户气泡正在展示，重拉会把它清掉
      } catch {
        notify("会话列表读取失败");
      }
    },
    [notify, selectedId],
  );

  const loadKbs = useCallback(async () => {
    try {
      setKbs(await listKbs());
    } catch {
      // /ask 页允许 KB 加载失败
    }
  }, []);

  // 会话就绪门控：子组件 effect 先于 AuthProvider 恢复令牌执行，
  // 不等 ready 的话第一个请求会带不上 Authorization（401），侧栏就一直是空的
  useEffect(() => {
    if (!ready || !authenticated) return;
    void loadConversations();
    void loadKbs();
  }, [ready, authenticated, loadConversations, loadKbs]);
  useEffect(() => {
    chatEnd.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const handleNewChat = async (mode: "chat" | "rag") => {
    setBusy(true);
    try {
      const conversation = await createConversation({
        title: mode === "rag" ? "检索问答" : "普通对话",
        mode,
        kb_id: mode === "rag" ? ragKbId || kbs[0]?.id || null : null,
      });
      await loadConversations(conversation.id);
      setSelectedId(conversation.id);
      setMessages([]);
      notify(mode === "rag" ? "检索会话已创建（绑定知识库）" : "对话已创建");
      inputBox.current?.focus();
    } catch {
      notify("会话创建失败");
    } finally {
      setBusy(false);
    }
  };

  const handleSelect = (conversationId: string) => {
    setSelectedId(conversationId);
    const conversation = conversations.find((item) => item.id === conversationId);
    if (conversation?.messages) {
      setMessages(
        conversation.messages.map((message) => ({
          id: message.id,
          role: message.role as "user" | "assistant",
          content: message.content,
          sources: message.sources,
        })),
      );
    } else {
      setMessages([]);
    }
  };

  const handleDelete = async (conversationId: string) => {
    try {
      await deleteConversation(conversationId);
      if (selectedId === conversationId) {
        setSelectedId("");
        setMessages([]);
      }
      await loadConversations();
      notify("会话已删除");
    } catch {
      notify("删除失败");
    }
  };

  const sendTo = async (
    conversation: Conversation,
    question: string,
  ): Promise<{ answer: string; sources: Record<string, unknown>[]; via?: "channel" | "own_key" }> => {
    const isRag = conversation.mode === "rag" && (conversation.kb_id || ragKbId);
    if (isRag) {
      const result = await queryKb(conversation.kb_id || ragKbId, question);
      return { answer: result.response || "（无结果）", sources: (result.text_units as Record<string, unknown>[]) || [], via: undefined };
    }
    const data = await askAiChat([{ role: "user", content: question }]);
    return { answer: data.content ?? data.reply ?? "（空回答）", sources: [], via: data.via };
  };

  const handleSend = async () => {
    const question = input.trim();
    if (!question || busy) return;
    setInput("");
    setMessages((current) => [...current, { id: `local-${Date.now()}`, role: "user", content: question, sources: [] }]);
    setBusy(true);
    try {
      // 没选会话：自动新建一个「对话」再发（检索会话要绑知识库，得显式建）
      let conversation = conversations.find((item) => item.id === selectedId);
      if (!conversation) {
        conversation = await createConversation({
          title: question.length > 18 ? `${question.slice(0, 18)}…` : question,
          mode: "chat",
          kb_id: null,
        });
        setSelectedId(conversation.id);
        await loadConversations(conversation.id);
      }
      const { answer, sources, via } = await sendTo(conversation, question);
      setMessages((current) => [...current, { id: `reply-${Date.now()}`, role: "assistant", content: answer, sources, via }]);
      await appendMessage(conversation.id, "user", question);
      await appendMessage(conversation.id, "assistant", answer, sources);
      await loadConversations(conversation.id);
    } catch (error) {
      const message = error instanceof Error ? error.message : "请求失败";
      setMessages((current) => [...current, { id: `error-${Date.now()}`, role: "assistant", content: message, sources: [] }]);
    } finally {
      setBusy(false);
    }
  };

  const selected = conversations.find((item) => item.id === selectedId);

  /** 会话按日期分组（今天 / 昨天 / 更早）——参考图里每条会话名下带日期，这里分组显示更省地方。 */
  const groupedConversations = useMemo(() => {
    const dayOf = (value: string) => new Date(value).toDateString();
    const today = new Date().toDateString();
    const yesterday = new Date(Date.now() - 86400000).toDateString();
    const groups: { label: string; items: Conversation[] }[] = [
      { label: "今天", items: [] },
      { label: "昨天", items: [] },
      { label: "更早", items: [] },
    ];
    for (const conversation of conversations) {
      const day = dayOf(conversation.updated_at || conversation.created_at);
      if (day === today) groups[0].items.push(conversation);
      else if (day === yesterday) groups[1].items.push(conversation);
      else groups[2].items.push(conversation);
    }
    return groups.filter((group) => group.items.length > 0);
  }, [conversations]);

  const shortDate = (value: string) => {
    const date = new Date(value);
    return `${date.getMonth() + 1}/${date.getDate()}`;
  };

  return (
    <div className="page-content" id="ask" data-page="ask">
      <PageHeading hint="内置 AI 问答：普通对话与知识库检索，多会话管理" />

      <section className="ask-layout">
        {/* 左列：会话（新对话在最上，按日期分组）——参考图的形态 */}
        <aside className="ask-sessions" aria-label="会话管理">
          <button className="ask-new-chat" data-testid="ask-new-chat" disabled={busy} onClick={() => void handleNewChat("chat")}>
            <Plus size={15} /> 新对话
          </button>
          <div className="ask-sessions-scroll" data-testid="conversation-list">
            {groupedConversations.map((group) => (
              <div className="ask-session-group" key={group.label}>
                <div className="ask-session-group-label">{group.label}</div>
                {group.items.map((conversation) => (
                  <div
                    key={conversation.id}
                    className={`ask-session-row${selectedId === conversation.id ? " is-active" : ""}`}
                    onClick={() => handleSelect(conversation.id)}
                    data-testid={`session-row-${conversation.id}`}
                  >
                    <div className="ask-session-main">
                      <strong>{conversation.title}</strong>
                      <small>
                        {shortDate(conversation.updated_at || conversation.created_at)} · {MODE_LABEL[conversation.mode] ?? conversation.mode}
                        {conversation.messages?.length ? ` · ${conversation.messages.length} 条` : ""}
                      </small>
                    </div>
                    <button
                      className="ask-session-delete"
                      aria-label={`删除 ${conversation.title}`}
                      onClick={(event) => {
                        event.stopPropagation();
                        setDeleteTarget({ id: conversation.id, title: conversation.title });
                      }}
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                ))}
              </div>
            ))}
            {!conversations.length && <EmptyState>还没有会话：点上面「新对话」或在右侧直接输入</EmptyState>}
          </div>
        </aside>

        {/* 右侧：对话窗口（顶部工具条 + 消息内滚 + 输入区钉底） */}
        <div className="grid ask-main">
          <Panel testId="chat-panel">
            <div className="ask-toolbar">
              <span className="ask-toolbar-title">{selected?.title ?? "新对话"}</span>
              <select
                value={ragKbId}
                onChange={(event) => setRagKbId(event.target.value)}
                disabled={!kbs.length}
                aria-label="知识库"
                data-testid="ask-kb-select"
              >
                <option value="">{kbs.length ? "选择知识库…" : "还没有知识库"}</option>
                {kbs.map((kb) => (
                  <option key={kb.id} value={kb.id}>
                    {kb.name}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="button button-secondary"
                data-testid="ask-new-rag"
                disabled={busy || !kbs.length}
                title={kbs.length ? "绑定所选知识库开一个检索会话" : "先在知识库页建一个知识库"}
                onClick={() => void handleNewChat("rag")}
              >
                <BookOpen size={14} /> 检索会话
              </button>
              <Link className="inline-link ask-toolbar-link" href="/kb">
                管理文档 →
              </Link>
            </div>
            <div className="ask-messages" data-testid="chat-messages">
              {messages.map((message) => (
                <div key={message.id} className={`ask-row${message.role === "user" ? " is-user" : ""}`}>
                  {message.role === "user" ? (
                    <div className="ask-bubble">{message.content}</div>
                  ) : (
                    <div className="ask-answer">
                      {message.content}
                      {message.via ? (
                        <div className="hint" style={{ marginTop: 6 }}>
                          {message.via === "channel" ? "平台渠道 · 消耗免费额度" : "你的自配模型"}
                        </div>
                      ) : null}
                      {message.sources.length > 0 ? (
                        <div className="chips" style={{ marginTop: 8 }}>
                          {message.sources.slice(0, 3).map((source, index) => (
                            <span className="chip" key={index}>
                              {String(source.title || source.full_doc_id || "来源")}
                            </span>
                          ))}
                        </div>
                      ) : null}
                    </div>
                  )}
                </div>
              ))}
              {!messages.length && !busy ? (
                <EmptyState>
                  <MessageSquare size={16} /> 输入问题开始对话——没有选中的会话时会自动新建一个
                  {kbs.length ? "；要用知识库检索请先在右侧建「检索会话」" : ""}
                </EmptyState>
              ) : null}
              {busy ? <div className="hint">思考中…</div> : null}
              <div ref={chatEnd} />
            </div>

            <div className="ask-compose">
              <textarea
                ref={inputBox}
                value={input}
                rows={1}
                placeholder={selected?.mode === "rag" ? "问知识库中的内容…" : "输入问题，Enter 发送（Shift+Enter 换行）"}
                data-testid="chat-input"
                onChange={(event) => setInput(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    void handleSend();
                  }
                }}
              />
              <button className="button button-primary" data-testid="chat-send" disabled={busy || !input.trim()} onClick={() => void handleSend()}>
                {busy ? <RefreshCcw size={15} className="spin" /> : <Send size={15} />}
                发送
              </button>
            </div>
            {selected?.mode === "rag" ? <p className="hint">检索问答走知识库索引；需先在知识库页触发一次索引。</p> : null}
          </Panel>
        </div>

      </section>

      {deleteTarget ? (
        <ConfirmDialog
          title="删除会话？"
          description={
            <>
              <strong>{deleteTarget.title}</strong>
              <div style={{ marginTop: 6 }}>会话及其消息记录会被删除，无法恢复。</div>
            </>
          }
          confirmLabel="删除会话"
          tone="danger"
          testId="ask-delete-confirm"
          onCancel={() => setDeleteTarget(null)}
          onConfirm={() => {
            const target = deleteTarget;
            setDeleteTarget(null);
            void handleDelete(target.id);
          }}
        />
      ) : null}
    </div>
  );
}