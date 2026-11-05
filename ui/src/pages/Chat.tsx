// Chat: one maintainer's threads with the service. Proposed actions appear inline and
// run only after approval.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, type ChatMessage, type ChatThread, type Me } from "../api";
import { Empty, ErrorBox, Loading, useAct } from "../components";
import { useT } from "../i18n";
import { ProposalCard, useProposals } from "./Inbox";

export default function ChatPage({ me }: { me: Me }) {
  const t = useT();
  const { threadId } = useParams();
  const navigate = useNavigate();
  const threads = useQuery({ queryKey: ["chat", "threads"], queryFn: () => api.get<ChatThread[]>("/api/chat/threads") });
  const [repo, setRepo] = useState("");
  const [first, setFirst] = useState("");
  const create = useAct(
    async () => {
      const thread = await api.post<ChatThread>("/api/chat/threads", { text: first, repo: repo || null });
      await api.post<ChatMessage>(`/api/chat/threads/${thread.id}/messages`, { text: first });
      return thread;
    },
    [["chat"], ["proposals"]],
  );

  return (
    <>
      <h1>{t("Chat")}</h1>
      <div className="chat">
        <aside className="card stack">
          <form
            className="stack"
            onSubmit={async (e) => {
              e.preventDefault();
              const thread = await create.mutateAsync(undefined);
              setFirst("");
              navigate(`/chat/${thread.id}`);
            }}
          >
            <strong className="small">{t("New thread")}</strong>
            <select value={repo} onChange={(e) => setRepo(e.target.value)} aria-label={t("Repository")}>
              <option value="">any repository</option>
              {me.repos.map((r) => (
                <option key={r}>{r}</option>
              ))}
            </select>
            <textarea value={first} onChange={(e) => setFirst(e.target.value)} placeholder="What should I review today?" rows={3} />
            <button className="primary" disabled={!first.trim() || create.isPending}>
              {create.isPending ? "…" : t("Send")}
            </button>
            <ErrorBox error={create.error} />
          </form>
          <div className="thread-list">
            {threads.isLoading ? <Loading /> : (threads.data ?? []).map((th) => (
              <Link key={th.id} to={`/chat/${th.id}`} className={th.id === threadId ? "active" : ""}>
                <div className="small">{th.title || "Untitled"}</div>
                <div className="muted small">{th.repo ?? "all repositories"}</div>
              </Link>
            ))}
          </div>
        </aside>
        <section className="card">{threadId ? <Thread threadId={threadId} /> : <Empty>Pick or start a thread.</Empty>}</section>
      </div>
    </>
  );
}

function Thread({ threadId }: { threadId: string }) {
  const t = useT();
  const messages = useQuery({ queryKey: ["chat", "messages", threadId], queryFn: () => api.get<ChatMessage[]>(`/api/chat/threads/${threadId}/messages`) });
  const proposals = useProposals();
  const [text, setText] = useState("");
  const send = useAct((body: string) => api.post<ChatMessage>(`/api/chat/threads/${threadId}/messages`, { text: body }), [["chat"], ["proposals"]]);
  const byAction = new Map((proposals.data ?? []).map((p) => [p.action_id, p]));

  return (
    <div className="stack">
      <ErrorBox error={messages.error} />
      {messages.isLoading ? <Loading /> : (
        <div className="messages">
          {(messages.data ?? []).map((m) => (
            <div key={m.id} className={`msg ${m.role}`}>
              {m.text}
              {m.proposals.length > 0 && (
                <div className="stack" style={{ marginTop: 8 }}>
                  {m.proposals.map((id) => {
                    const p = byAction.get(id);
                    return p ? <ProposalCard key={id} proposal={p} compact /> : (
                      <span key={id} className="small muted">
                        Proposal for action {id} is no longer pending. <Link to="/inbox">Inbox</Link>
                      </span>
                    );
                  })}
                </div>
              )}
            </div>
          ))}
          {send.isPending && <div className="msg assistant muted">…</div>}
        </div>
      )}
      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault();
          if (!text.trim()) return;
          send.mutate(text);
          setText("");
        }}
      >
        <input value={text} onChange={(e) => setText(e.target.value)} style={{ flex: 1 }} placeholder="Message" />
        <button className="primary" disabled={send.isPending || !text.trim()}>
          {t("Send")}
        </button>
      </form>
      <ErrorBox error={send.error} />
    </div>
  );
}
