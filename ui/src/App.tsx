// Layout, navigation, sign-in gate, and routes.

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, NavLink, Route, Routes } from "react-router-dom";
import { api, ApiError, type Me, type ProposalWithAction } from "./api";
import { ErrorBox, Loading } from "./components";
import { QUESTION_EVENT, useLiveEvents } from "./events";
import { useLang, useT } from "./i18n";
import AgentsPage from "./pages/Agents";
import AuditPage from "./pages/Audit";
import ChatPage from "./pages/Chat";
import CiPage from "./pages/Ci";
import InboxPage from "./pages/Inbox";
import MemoryPage from "./pages/Memory";
import PrPage from "./pages/Pr";
import QuestionsPage from "./pages/Questions";
import QueuePage from "./pages/Queue";
import ServicePage from "./pages/Service";
import { RepoDetailPage, ReposPage } from "./pages/Repos";
import SignIn from "./pages/SignIn";

export function useMe() {
  return useQuery({ queryKey: ["me"], queryFn: () => api.get<Me>("/api/me") });
}

export default function App() {
  const me = useMe();
  const t = useT();
  const { lang, setLang } = useLang();
  const signedIn = me.isSuccess;
  useLiveEvents(signedIn);
  const proposals = useQuery({
    queryKey: ["proposals", "pending"],
    queryFn: () => api.get<ProposalWithAction[]>("/api/proposals"),
    enabled: signedIn,
  });
  const [asked, setAsked] = useState<string | null>(null);
  useEffect(() => {
    const show = (e: Event) => setAsked((e as CustomEvent<string>).detail);
    window.addEventListener(QUESTION_EVENT, show);
    return () => window.removeEventListener(QUESTION_EVENT, show);
  }, []);

  if (me.isLoading) return <Loading />;
  if (me.error instanceof ApiError && me.error.status === 401) return <SignIn />;
  if (me.error) return <div className="content"><ErrorBox error={me.error} /></div>;
  const user = me.data!;

  const signOut = async () => {
    await fetch("/auth/logout", { method: "POST", credentials: "same-origin" });
    window.location.href = "/";
  };

  const nav: [string, string, number | undefined][] = [
    ["/", t("Queue"), undefined],
    ["/inbox", t("Inbox"), proposals.data?.length],
    ["/questions", t("Questions"), user.pending_questions],
    ["/memory", t("Memory"), undefined],
    ["/repos", t("Repositories"), undefined],
    ["/ci", t("CI"), undefined],
    ["/agents", t("Agents"), undefined],
    ["/service", t("Service"), undefined],
    ["/audit", t("Audit"), undefined],
    ["/chat", t("Chat"), undefined],
  ];

  return (
    <div className="layout">
      <nav className="nav" aria-label="Main">
        <div className="brand">auto-maintainer</div>
        {nav.map(([to, label, count]) => (
          <NavLink key={to} to={to} end={to === "/"}>
            <span>{label}</span>
            {count ? <span className="count">{count}</span> : null}
          </NavLink>
        ))}
      </nav>
      <div className="main">
        {user.paused && <div className="banner paused">{t("The service is paused")}</div>}
        {(asked || user.pending_questions > 0) && (
          <div className="banner question" role="status">
            {asked ?? t("An agent is waiting for an answer")}{" "}
            <Link to="/questions" onClick={() => setAsked(null)}>
              {t("Open questions")}
            </Link>
          </div>
        )}
        <header className="topbar">
          {user.dev_mode && <span className="badge">{t("Dev mode")}</span>}
          <span className="spacer" />
          <select aria-label="Language" value={lang} onChange={(e) => setLang(e.target.value as "en" | "zh")}>
            <option value="en">EN</option>
            <option value="zh">中文</option>
          </select>
          <span className="row">
            {user.avatar_url && <img className="avatar" src={user.avatar_url} alt="" />}
            <strong>{user.login}</strong>
            {user.operator && <span className="muted small">operator</span>}
          </span>
          <button onClick={signOut}>{t("Sign out")}</button>
        </header>
        <main className="content">
          <Routes>
            <Route path="/" element={<QueuePage me={user} />} />
            <Route path="/pr/:owner/:repo/:number" element={<PrPage />} />
            <Route path="/inbox" element={<InboxPage me={user.login} />} />
            <Route path="/questions" element={<QuestionsPage />} />
            <Route path="/memory" element={<MemoryPage />} />
            <Route path="/repos" element={<ReposPage />} />
            <Route path="/repos/:owner/:repo" element={<RepoDetailPage />} />
            <Route path="/ci" element={<CiPage me={user} />} />
            <Route path="/agents" element={<AgentsPage />} />
            <Route path="/service" element={<ServicePage />} />
            <Route path="/audit" element={<AuditPage me={user} />} />
            <Route path="/chat" element={<ChatPage me={user} />} />
            <Route path="/chat/:threadId" element={<ChatPage me={user} />} />
            <Route path="*" element={<div className="muted">Page not found.</div>} />
          </Routes>
        </main>
      </div>
    </div>
  );
}
