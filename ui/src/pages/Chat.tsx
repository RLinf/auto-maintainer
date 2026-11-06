// Conversations: every thread the user had with the agent, from any page, in full. It
// renders the same conversation as the agent panel.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, type ChatThread } from "../api";
import { Conversation } from "../agent";
import { ErrorBox, relative } from "../components";
import { useT } from "../i18n";
import { Icon } from "../icons";
import { Skeleton } from "../ui";

export default function ChatPage() {
  const t = useT();
  const { threadId } = useParams();
  const navigate = useNavigate();
  const threads = useQuery({ queryKey: ["chat", "threads"], queryFn: () => api.get<ChatThread[]>("/api/chat/threads") });
  const [filter, setFilter] = useState("");
  const rows = [...(threads.data ?? [])]
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
    .filter((th) => `${th.title} ${th.repo ?? ""}`.toLowerCase().includes(filter.toLowerCase()));
  const current = threads.data?.find((th) => th.id === threadId);
  const context = current?.repo
    ? { page: "repository", repo: current.repo, number: null, label: current.repo }
    : { page: "chat", repo: null, number: null, label: t("Conversations") };

  return (
    <div className="chat">
      <aside className="card stack" style={{ padding: 12, gap: 8, alignSelf: "start" }}>
        <button className="primary" onClick={() => navigate("/chat")}>
          <Icon name="plus" />
          {t("New conversation")}
        </button>
        <label className="search" style={{ minWidth: 0, width: "100%", flex: "none" }}>
          <Icon name="search" />
          <input placeholder={t("Search conversations")} value={filter} onChange={(e) => setFilter(e.target.value)} />
        </label>
        <ErrorBox error={threads.error} />
        <div className="thread-list" style={{ maxHeight: "64vh", overflowY: "auto" }}>
          {threads.isLoading ? (
            <Skeleton rows={4} />
          ) : (
            rows.map((th) => (
              <Link key={th.id} to={`/chat/${th.id}`} className={th.id === threadId ? "active" : ""}>
                <div className="truncate" style={{ fontWeight: 520 }}>
                  {th.title || t("Untitled")}
                </div>
                <div className="muted small">
                  {th.repo ?? t("all repositories")} · {relative(th.created_at)}
                </div>
              </Link>
            ))
          )}
          {!threads.isLoading && !rows.length && <span className="small muted">{t("No conversations yet.")}</span>}
        </div>
      </aside>
      <section className="card flush convo">
        <Conversation
          key={threadId ?? "new"}
          threadId={threadId ?? null}
          context={context}
          onThread={(id) => navigate(`/chat/${id}`, { replace: true })}
        />
      </section>
    </div>
  );
}
