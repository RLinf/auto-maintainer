// The agent, reachable from every page. The panel knows the page it was opened from (a
// PR, a repository, or a list) and sends only identifiers; the service reads that page's
// data itself (web/chat.py:_page_block). Each page context keeps its own conversation
// for the browser session. Whatever the agent proposes runs only after approval.

import { useQueryClient, useMutation, useQuery } from "@tanstack/react-query";
import {
  createContext,
  Fragment,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Link, matchPath, useLocation, useNavigate } from "react-router-dom";
import { api, type ChatMessage, type ChatRequest, type ChatSteps, type ChatThread } from "./api";
import { ErrorBox, GrowingTextarea, StopButton } from "./components";
import { useProposals } from "./data";
import { useT } from "./i18n";
import { Markdown } from "./markdown";
import { Icon, type IconName } from "./icons";
import { JobById, Steps } from "./jobs";
import { ProposalCard } from "./proposal";
import { ChangeById } from "./settingsChange";
import { useFollowBottom, useHotkey } from "./ui";

export interface AgentContext {
  page: string;
  repo: string | null;
  number: number | null;
  label: string;
}

const PAGES: { path: string; page: string; label: string; icon: IconName }[] = [
  { path: "/", page: "home", label: "Home", icon: "home" },
  { path: "/prs", page: "queue", label: "Pull requests", icon: "pr" },
  { path: "/repos", page: "repositories", label: "Repositories", icon: "repo" },
  { path: "/inbox", page: "proposals", label: "Approvals", icon: "inbox" },
  { path: "/questions", page: "questions", label: "Agent questions", icon: "question" },
  { path: "/memory", page: "memory", label: "Memory", icon: "book" },
  { path: "/ci", page: "ci", label: "CI health", icon: "activity" },
  { path: "/agents", page: "agents", label: "Agents", icon: "cpu" },
  { path: "/settings", page: "settings", label: "Settings", icon: "settings" },
  { path: "/service", page: "service", label: "Service", icon: "server" },
  { path: "/audit", page: "audit", label: "Audit log", icon: "shield" },
  { path: "/chat/*", page: "chat", label: "Conversations", icon: "message" },
];

export function routeContext(pathname: string): AgentContext {
  const pr = matchPath("/pr/:owner/:repo/:number", pathname);
  if (pr) {
    const repo = `${pr.params.owner}/${pr.params.repo}`;
    const number = Number(pr.params.number);
    return { page: "pr", repo, number, label: `${repo}#${number}` };
  }
  const repo = matchPath("/repos/:owner/:repo", pathname);
  if (repo) {
    const name = `${repo.params.owner}/${repo.params.repo}`;
    return { page: "repository", repo: name, number: null, label: name };
  }
  const page = PAGES.find((p) => matchPath({ path: p.path, end: true }, pathname)) ?? PAGES[0];
  return { page: page.page, repo: null, number: null, label: page.label };
}

function contextIcon(c: AgentContext): IconName {
  if (c.page === "pr") return "pr";
  if (c.page === "repository") return "repo";
  return PAGES.find((p) => p.page === c.page)?.icon ?? "home";
}

const SUGGESTIONS: Record<string, string[]> = {
  home: ["What needs my attention today?", "Summarize what changed since yesterday", "Which PRs are blocked, and on what?"],
  queue: ["Which PRs should I review first, and why?", "Which PRs look ready to merge?", "Which PRs have gone stale?"],
  pr: ["Summarize this PR", "What are the risks in this change?", "Why does it have this priority?", "What should the review focus on?"],
  repository: ["How is this repository doing?", "Which settings would you change?", "What has the service learned here recently?"],
  repositories: ["Which repository needs attention?", "Compare activity across repositories"],
  proposals: ["Which proposals are safe to approve?", "Explain the pending proposals"],
  questions: ["Explain what the agents are asking", "Which of these requests are risky?"],
  memory: ["What changed in memory recently?", "Is anything in memory out of date?"],
  ci: ["Which jobs are flaky?", "Should any failed jobs be rerun?"],
  agents: ["Which runtime works best for reviews?", "Why did recent runs fail?"],
  service: ["What do the pending commits change?", "Suggest an improvement to the service"],
  settings: ["Run Claude Code on another model provider", "Which agent does reviews, and why?", "Check repositories less often"],
  audit: ["Summarize recent activity", "Who changed configuration recently?"],
  chat: ["What can you do?"],
};

// ---- per-context threads, kept for the browser session ------------------------------------

const STORE = "am.agent.threads";

function ctxKey(c: AgentContext) {
  return `${c.page}:${c.repo ?? ""}:${c.number ?? ""}`;
}

function readThreads(): Record<string, string> {
  try {
    return JSON.parse(sessionStorage.getItem(STORE) ?? "{}") as Record<string, string>;
  } catch {
    return {};
  }
}

function writeThreads(map: Record<string, string>) {
  try {
    sessionStorage.setItem(STORE, JSON.stringify(map));
  } catch {
    // storage can be unavailable; conversations then last until the page reloads
  }
}

// ---- provider ----------------------------------------------------------------------------

interface OpenOptions {
  context?: AgentContext;
  prompt?: string;
  send?: boolean;
}

interface AgentApi {
  isOpen: boolean;
  context: AgentContext;
  open: (options?: OpenOptions) => void;
  close: () => void;
  toggle: () => void;
}

const AgentCtx = createContext<AgentApi>({
  isOpen: false,
  context: routeContext("/"),
  open: () => {},
  close: () => {},
  toggle: () => {},
});

export function useAgent() {
  return useContext(AgentCtx);
}

interface Draft {
  text: string;
  send: boolean;
  nonce: number;
}

export function AgentProvider({ children }: { children: ReactNode }) {
  const location = useLocation();
  const fromRoute = useMemo(() => routeContext(location.pathname), [location.pathname]);
  const [override, setOverride] = useState<AgentContext | null>(null);
  const [isOpen, setOpen] = useState(false);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [threads, setThreads] = useState<Record<string, string>>(readThreads);
  useEffect(() => setOverride(null), [location.pathname]);
  const context = override ?? fromRoute;

  const open = useCallback((options: OpenOptions = {}) => {
    if (options.context) setOverride(options.context);
    if (options.prompt) setDraft({ text: options.prompt, send: !!options.send, nonce: Date.now() });
    setOpen(true);
  }, []);
  const api_ = useMemo<AgentApi>(
    () => ({ isOpen, context, open, close: () => setOpen(false), toggle: () => setOpen((o) => !o) }),
    [isOpen, context, open],
  );
  useHotkey("mod+j", () => setOpen((o) => !o));
  useEffect(() => {
    document.body.classList.toggle("agent-open", isOpen);
  }, [isOpen]);

  const key = ctxKey(context);
  const setThread = (id: string | null) =>
    setThreads((prev) => {
      const next = { ...prev };
      if (id) next[key] = id;
      else delete next[key];
      writeThreads(next);
      return next;
    });

  return (
    <AgentCtx.Provider value={api_}>
      {children}
      <AgentPanel
        isOpen={isOpen}
        context={context}
        pageContext={fromRoute}
        threadId={threads[key] ?? null}
        onThread={setThread}
        draft={draft}
        onClose={() => setOpen(false)}
        onResetContext={() => setOverride(null)}
      />
    </AgentCtx.Provider>
  );
}

function AgentPanel({
  isOpen,
  context,
  pageContext,
  threadId,
  onThread,
  draft,
  onClose,
  onResetContext,
}: {
  isOpen: boolean;
  context: AgentContext;
  pageContext: AgentContext;
  threadId: string | null;
  onThread: (id: string | null) => void;
  draft: Draft | null;
  onClose: () => void;
  onResetContext: () => void;
}) {
  const t = useT();
  const navigate = useNavigate();
  const ref = useRef<HTMLElement>(null);
  const overridden = ctxKey(context) !== ctxKey(pageContext);
  // Mounted from the first opening on, so closing the panel does not drop a reply in flight.
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    if (isOpen) setMounted(true);
    // a closed panel is off screen; keep its controls out of the tab order
    ref.current?.toggleAttribute("inert", !isOpen);
  }, [isOpen]);
  return (
    <aside
      ref={ref}
      className={`agent-panel ${isOpen ? "open" : ""}`}
      aria-label={t("Agent")}
      aria-hidden={!isOpen}
      onKeyDown={(e) => {
        if (e.key === "Escape") onClose();
      }}
    >
      <div className="agent-head">
        <span className="avatar-ai">
          <Icon name="sparkles" size={16} />
        </span>
        <div style={{ flex: 1, minWidth: 0 }} className="stack">
          <strong style={{ lineHeight: 1.2 }}>{t("Agent")}</strong>
          <span className="context-chip" title={t("The agent sees this page")}>
            <Icon name={contextIcon(context)} size={12} />
            <span className="truncate">{context.label}</span>
            {overridden && (
              <button className="ghost icon sm" style={{ width: 18, height: 18 }} aria-label={t("Back to this page")} onClick={onResetContext}>
                <Icon name="x" size={11} />
              </button>
            )}
          </span>
        </div>
        {threadId && (
          <button
            className="ghost icon sm"
            title={t("Open in Conversations")}
            aria-label={t("Open in Conversations")}
            onClick={() => {
              navigate(`/chat/${threadId}`);
              onClose();
            }}
          >
            <Icon name="external" size={15} />
          </button>
        )}
        <button className="ghost icon sm" title={t("New conversation")} aria-label={t("New conversation")} onClick={() => onThread(null)}>
          <Icon name="plus" size={16} />
        </button>
        <button className="ghost icon sm" title={`${t("Close")} (Esc)`} aria-label={t("Close")} onClick={onClose}>
          <Icon name="x" size={16} />
        </button>
      </div>
      {mounted && (
        <Conversation key={ctxKey(context)} threadId={threadId} context={context} onThread={onThread} draft={draft} autoFocus={isOpen} />
      )}
    </aside>
  );
}

// ---- conversation (the panel and the Conversations page) ----------------------------------

/** The newest draft already applied, so a conversation that mounts again does not resend it. */
let consumedDraft = 0;

export function Conversation({
  threadId,
  context,
  onThread,
  draft,
  autoFocus = true,
  liveRun = null,
}: {
  threadId: string | null;
  context: AgentContext;
  onThread: (id: string) => void;
  draft?: Draft | null;
  autoFocus?: boolean;
  /** The turn the service says runs now in this thread, from its list of threads:
   * one sent before this page loaded can still be stopped. */
  liveRun?: string | null;
}) {
  const t = useT();
  const client = useQueryClient();
  const messages = useQuery({
    queryKey: ["chat", "messages", threadId],
    queryFn: () => api.get<ChatMessage[]>(`/api/chat/threads/${threadId}/messages`),
    enabled: !!threadId,
  });
  const proposals = useProposals();
  const byAction = new Map((proposals.data ?? []).map((p) => [p.action_id, p]));
  const [text, setText] = useState("");
  const body = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const ctx = { page: context.page, repo: context.repo, number: context.number };

  const send = useMutation({
    mutationFn: async (message: string) => {
      let id = threadId;
      if (!id) {
        const thread = await api.post<ChatThread>("/api/chat/threads", { text: message, context: ctx });
        id = thread.id;
        onThread(id);
      }
      return api.post<ChatMessage>(`/api/chat/threads/${id}/messages`, { text: message, context: ctx });
    },
    onSettled: () => {
      for (const key of ["chat", "proposals", "settings", "memory"]) void client.invalidateQueries({ queryKey: [key] });
    },
  });
  // A message sent while the agent answers interrupts it: the service stops that
  // turn and answers this one.
  const submit = (message: string) => {
    const m = message.trim();
    if (!m) return;
    setText("");
    send.mutate(m);
  };

  useEffect(() => {
    if (!draft || draft.nonce <= consumedDraft) return;
    consumedDraft = draft.nonce;
    if (draft.send) submit(draft.text);
    else {
      setText(draft.text);
      input.current?.focus();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft?.nonce]);

  // What the agent is doing for the message being answered; the event stream refreshes
  // it for operators, and a short poll covers everyone else.
  const live = useQuery({
    queryKey: ["chat", "steps", threadId, "live"],
    queryFn: () => api.get<ChatSteps>(`/api/chat/threads/${threadId}/steps`),
    enabled: !!threadId && send.isPending,
    refetchInterval: send.isPending ? 1500 : false,
  });
  const list = messages.data ?? [];
  const pendingText = send.isPending ? send.variables : null;
  const last = list[list.length - 1];
  const showPending = pendingText && !(last && last.role === "user" && last.text === pendingText);
  // Follows the conversation and the agent's live steps; sending a message jumps back down.
  const stick = useFollowBottom(body);
  useEffect(() => {
    if (send.isPending) stick();
  }, [send.isPending, stick]);
  useEffect(() => {
    if (autoFocus) input.current?.focus();
  }, [autoFocus]);

  const suggestions = SUGGESTIONS[context.page] ?? SUGGESTIONS.home;
  return (
    <>
      <div className="agent-body" ref={body}>
        {!list.length && !pendingText && (
          <div className="agent-welcome">
            <strong>{t("Ask about")} {context.label}</strong>
            <span className="small">
              {t("The agent reads what you can see here. Anything it suggests changing waits for your approval.")}
            </span>
          </div>
        )}
        {list.map((m) => (
          <Fragment key={m.id}>
            <div className={`bubble ${m.role === "user" ? "user" : "assistant"}`}>
              {m.role === "user" ? m.text : <Rich text={m.text} />}
            </div>
            {m.run_id && threadId && <RunSteps threadId={threadId} run={m.run_id} />}
            {(m.jobs ?? []).map((id) => (
              <JobById key={id} id={id} />
            ))}
            {threadId &&
              (m.requests ?? []).map((r) => (
                <RequestCard key={r.id} threadId={threadId} request={r} />
              ))}
            {(m.changes ?? []).map((id) => (
              <ChangeById key={id} id={id} />
            ))}
            {m.proposals.map((id) => {
              const p = byAction.get(id);
              return p ? (
                <ProposalCard key={id} proposal={p} compact />
              ) : (
                <span key={id} className="small muted">
                  {t("A proposal from this answer is no longer pending.")} <Link to="/inbox">{t("Approvals")}</Link>
                </span>
              );
            })}
          </Fragment>
        ))}
        {showPending && <div className="bubble user">{pendingText}</div>}
        {send.isPending &&
          (live.data?.steps.length ? (
            <div className="agent-live" aria-live="polite">
              <Steps steps={live.data.steps} active />
            </div>
          ) : (
            <div className="bubble assistant thinking" aria-live="polite">
              <span className="typing" aria-label={t("The agent is working")}>
                <span />
                <span />
                <span />
              </span>
            </div>
          ))}
        <ErrorBox error={send.error || messages.error} />
      </div>
      <div className="agent-foot">
        {!list.length && !send.isPending && (
          <div className="suggestions">
            {suggestions.map((s) => (
              <button key={s} className="suggestion" onClick={() => submit(t(s))}>
                {t(s)}
              </button>
            ))}
          </div>
        )}
        <form
          className="composer"
          onSubmit={(e) => {
            e.preventDefault();
            submit(text);
          }}
        >
          <GrowingTextarea
            ref={input}
            value={text}
            placeholder={t("Ask the agent…")}
            aria-label={t("Message")}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                submit(text);
              }
            }}
          />
          {(send.isPending || !!liveRun) && threadId && <StopButton url={`/api/chat/threads/${threadId}/stop`} />}
          <button className="primary icon" disabled={!text.trim()} aria-label={t("Send")} title={send.isPending ? t("Stop the agent and send this instead") : undefined}>
            <Icon name="send" size={15} />
          </button>
        </form>
        <span className="small muted" style={{ paddingLeft: 4 }}>
          <span className="kbd">↵</span> {t("send")} · <span className="kbd">⇧</span>
          <span className="kbd">↵</span> {t("new line")}
        </span>
      </div>
    </>
  );
}

/** Plain text with ``` fences, `code`, and **bold**; no HTML from the reply is rendered. */
export function Rich({ text }: { text: string }) {
  return <Markdown text={text} className="chat-md" />;
}

/** A button that opens the agent, optionally about something other than the page. */
export function AskButton({
  context,
  prompt,
  label = "Ask agent",
  size,
  iconOnly,
}: {
  context?: AgentContext;
  prompt?: string;
  label?: string;
  size?: "sm";
  iconOnly?: boolean;
}) {
  const t = useT();
  const agent = useAgent();
  return (
    <button
      className={`${iconOnly ? "ghost icon" : "agent"} ${size ?? ""}`}
      title={t(label)}
      aria-label={t(label)}
      onClick={() => agent.open({ context, prompt })}
    >
      <Icon name="sparkles" size={size ? 14 : 16} />
      {!iconOnly && t(label)}
    </button>
  );
}

/** What the agent did for one answer, read when the person opens it. */
function RunSteps({ threadId, run }: { threadId: string; run: string }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const steps = useQuery({
    queryKey: ["chat", "steps", threadId, run],
    queryFn: () => api.get<ChatSteps>(`/api/chat/threads/${threadId}/steps?run=${encodeURIComponent(run)}`),
    enabled: open,
  });
  return (
    <details className="run-steps" onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}>
      <summary className="small muted">{t("What the agent did")}</summary>
      {steps.data ? (
        steps.data.steps.length ? (
          <Steps steps={steps.data.steps} active={false} />
        ) : (
          <span className="small muted">{t("Its steps are no longer kept.")}</span>
        )
      ) : (
        <span className="small muted">…</span>
      )}
    </details>
  );
}

/** Something an answer asks to do, waiting for a person: a change to the service's own
 * code (a maintainer of its repository approves) or onboarding a repository (someone
 * who may configure it approves). Nothing starts before Approve. */
export function RequestCard({ threadId, request, askedBy }: { threadId: string; request: ChatRequest; askedBy?: string }) {
  const t = useT();
  const client = useQueryClient();
  const decide = useMutation({
    mutationFn: (decision: "approve" | "decline") => api.post<ChatMessage>(`/api/chat/threads/${threadId}/requests/${request.id}/${decision}`),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["chat", "messages", threadId] });
      void client.invalidateQueries({ queryKey: ["chat", "requests"] });
    },
  });
  const improve = request.kind === "improve";
  return (
    <div className="proposal request-card">
      <div className="row" style={{ gap: 8 }}>
        <Icon name={improve ? "settings" : "repo"} size={15} />
        <strong>{improve ? t("Change the service's code") : t("Add a repository")}</strong>
        {askedBy && (
          <span className="small muted">
            {t("asked by")} {askedBy}
          </span>
        )}
        <span className={`pill ${request.state === "approved" ? "status-ok" : request.state === "declined" ? "" : "status-warn"}`}>
          {t(request.state === "pending" ? "Waiting for approval" : request.state === "approved" ? "Approved" : "Declined")}
        </span>
      </div>
      {improve ? <blockquote className="change-note">{request.text}</blockquote> : <code>{request.text}</code>}
      <span className="small muted">
        {improve
          ? t("An agent changes the code in a separate worktree; the service runs the tests and opens a PR. Merging and deploying it each need a maintainer's approval again.")
          : t("The service reads the repository and drafts its profile; its maintainers approve the profile before the service does anything there.")}
      </span>
      {request.state === "pending" ? (
        <div className="row">
          <button className="primary sm" disabled={decide.isPending} onClick={() => decide.mutate("approve")}>
            <Icon name="check" size={14} />
            {improve ? t("Approve and start") : t("Approve and add")}
          </button>
          <button className="ghost sm" disabled={decide.isPending} onClick={() => decide.mutate("decline")}>
            {t("Decline")}
          </button>
        </div>
      ) : (
        request.decided_by && (
          <span className="small muted">
            {t(request.state === "approved" ? "Approved by" : "Declined by")} {request.decided_by}
          </span>
        )
      )}
      {request.job && <JobById id={request.job} />}
      <ErrorBox error={decide.error} />
    </div>
  );
}
