// A repository's People tab (design 2.6): its roles, what each is responsible for,
// how people move between roles and who decides, who holds each role, and the history
// of changes. It starts as a skeleton. Reviewers talk with the agent that keeps it:
// where the process is written, rules, answers to its questions, members, or a check
// of who should change role. What the agent drafts waits here as a memory change that
// a maintainer of the repository applies. Roles grant nothing in the service: GitHub
// permissions still decide.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useEffect, useRef, useState } from "react";
import { api, type ChatMessage, type JobStep } from "./api";
import { Empty, ErrorBox, Section } from "./components";
import { useQuestions } from "./data";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { Steps } from "./jobs";
import { Markdown } from "./markdown";
import { QuestionCard } from "./pages/Questions";
import { ChangeById } from "./settingsChange";
import { Skeleton } from "./ui";

interface Role {
  id: string;
  name: string;
  level: number;
  description?: string;
  responsibilities?: string[];
  privileges?: string[];
  github_permission?: string | null;
  scoped?: boolean;
}
interface Criterion {
  text: string;
  measure?: string | null;
  threshold?: number | null;
  window_months?: number | null;
}
interface Transition {
  id: string;
  from_role?: string | null;
  to_role?: string | null;
  criteria?: Criterion[];
  process?: string[];
  decided_by?: string[];
  approvals?: number;
}
interface Member {
  login: string;
  role: string;
  areas?: string[];
  since?: string | null;
  note?: string;
}
interface RoleChange {
  login: string;
  from_role?: string | null;
  to_role?: string | null;
  on: string;
  by: string;
  reason?: string;
  evidence?: string[];
}
interface Source {
  kind: "file" | "url" | "conversation";
  where: string;
  note?: string;
}
export interface Governance {
  repo: string;
  summary?: string;
  established?: boolean;
  sources?: Source[];
  roles?: Role[];
  transitions?: Transition[];
  members?: Member[];
  history?: RoleChange[];
  open_questions?: string[];
}
interface PeopleResponse {
  repo: string;
  governance: Governance;
  stored: boolean;
  error: string | null;
  checks: { kind: string; login: string | null; text: string }[];
  permissions: Record<string, string>;
  pending: { id: string; governance: Governance | null; role_changes: { login: string; from: string | null; to: string | null }[] }[];
  can_configure: boolean;
  has_profile: boolean;
}

export function PeopleTab({ repo }: { repo: string }) {
  const t = useT();
  const data = useQuery({
    queryKey: ["people", repo],
    queryFn: () => api.get<PeopleResponse>(`/api/repos/${repo}/governance`),
  });
  const [showCurrent, setShowCurrent] = useState(false);
  if (data.isLoading) return <Skeleton rows={6} />;
  if (data.error || !data.data) return <ErrorBox error={data.error} />;
  const d = data.data;
  // The newest draft that waits for approval is what people read and answer first.
  const draft = [...d.pending].reverse().find((p) => p.governance);
  const viewingDraft = !!draft && !showCurrent;
  const gov = viewingDraft && draft?.governance ? draft.governance : d.governance;
  const roles = [...(gov.roles ?? [])].sort((a, b) => b.level - a.level);
  const name = (id: string | null | undefined) => (id ? (roles.find((r) => r.id === id)?.name ?? id) : null);
  const flagged = new Set(d.checks.map((c) => c.login?.toLowerCase()).filter(Boolean));

  return (
    <div className="stack people" style={{ gap: 16 }}>
      {draft && (
        <div className="strip question">
          <Icon name="shield" />
          <span>{viewingDraft ? t("Showing the agent's draft, which waits for a maintainer to apply it. Talk with the agent to change it.") : t("Showing the record as it is applied now.")}</span>
          <button className="sm right" onClick={() => setShowCurrent(!showCurrent)}>
            {viewingDraft ? t("Show the current record") : t("Show the draft")}
          </button>
        </div>
      )}
      {!draft && !gov.established && (
        <div className="strip question">
          <Icon name="sparkles" />
          <span>{t("This is the starting skeleton. Tell the agent below where your process is written, or describe it, and it fills this in for a maintainer to approve.")}</span>
        </div>
      )}
      {d.error && (
        <div className="error">
          {t("The stored record does not read, so the skeleton is shown")}: {d.error}
        </div>
      )}
      <PeopleTalk repo={repo} questions={(draft?.governance ?? d.governance).open_questions ?? []} established={!!(draft?.governance ?? d.governance).established} hasProfile={d.has_profile} />
      {d.pending.length > 0 && (
        <Section title={t("Changes waiting for a maintainer")} hint={d.can_configure ? t("you can apply them") : t("a maintainer applies them")}>
          <div className="stack" style={{ gap: 12 }}>
            {d.pending.map((p) => (
              <div key={p.id} className="stack" style={{ gap: 8 }}>
                {p.role_changes.length > 0 && (
                  <ul className="role-changes">
                    {p.role_changes.map((c) => (
                      <li key={`${c.login}-${c.from}-${c.to}`}>
                        <strong>@{c.login}</strong> <Move from={name(c.from)} to={name(c.to)} />
                      </li>
                    ))}
                  </ul>
                )}
                <ChangeById id={p.id} />
              </div>
            ))}
          </div>
        </Section>
      )}
      {gov.summary && (
        <Section title={t("How this repository is run")}>
          <Markdown text={gov.summary} />
          {(gov.sources ?? []).length > 0 && (
            <div className="small muted" style={{ marginTop: 8 }}>
              {t("From")}:{" "}
              {(gov.sources ?? []).map((s, i) => (
                <span key={`${s.kind}-${s.where}`}>
                  {i > 0 && " · "}
                  <SourceLink repo={repo} source={s} />
                </span>
              ))}
            </div>
          )}
        </Section>
      )}
      {!viewingDraft && d.checks.length > 0 && (
        <Section title={t("Where this differs from GitHub")} hint={t("checked now")}>
          <ul className="stack" style={{ gap: 4, margin: 0, paddingLeft: 18 }}>
            {d.checks.map((c) => (
              <li key={`${c.kind}-${c.login}`} className="small">
                {c.text}
              </li>
            ))}
          </ul>
        </Section>
      )}
      <Section title={t("Roles")} hint={t("highest first")}>
        {roles.length === 0 ? (
          <Empty icon="shield">{t("No roles yet.")}</Empty>
        ) : (
          <div className="role-grid">
            {roles.map((r) => {
              const members = (gov.members ?? []).filter((m) => m.role === r.id);
              return (
                <div key={r.id} className="role-card">
                  <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                    <strong>{r.name}</strong>
                    <span className="pill tag">{t("level")} {r.level}</span>
                    {r.github_permission && (
                      <span className="pill tag" title={t("The GitHub permission a holder is expected to have")}>
                        GitHub: {r.github_permission}
                      </span>
                    )}
                    {r.scoped && <span className="pill tag">{t("by area")}</span>}
                  </div>
                  {r.description && <span className="small muted">{r.description}</span>}
                  {(r.responsibilities ?? []).length > 0 && (
                    <div className="stack" style={{ gap: 2 }}>
                      <span className="kind-chip">{t("Responsible for")}</span>
                      <ul>
                        {(r.responsibilities ?? []).map((x) => (
                          <li key={x}>{x}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {(r.privileges ?? []).length > 0 && (
                    <div className="stack" style={{ gap: 2 }}>
                      <span className="kind-chip">{t("May")}</span>
                      <ul>
                        {(r.privileges ?? []).map((x) => (
                          <li key={x}>{x}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                  <div className="stack" style={{ gap: 4 }}>
                    <span className="kind-chip">
                      {t("Members")} · {members.length}
                    </span>
                    {members.length === 0 ? (
                      <span className="small muted">{t("Nobody yet.")}</span>
                    ) : (
                      <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                        {members.map((m) => (
                          <span
                            key={m.login}
                            className={`member${flagged.has(m.login.toLowerCase()) ? " flagged" : ""}`}
                            title={[d.permissions[m.login.toLowerCase()] && `GitHub: ${d.permissions[m.login.toLowerCase()]}`, m.since && `${t("since")} ${m.since}`, m.note].filter(Boolean).join(" · ")}
                          >
                            @{m.login}
                            {(m.areas ?? []).length > 0 && <span className="muted"> · {(m.areas ?? []).join(", ")}</span>}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </Section>
      <Section title={t("How roles change")}>
        {(gov.transitions ?? []).length === 0 ? (
          <Empty icon="activity">{t("No rules yet.")}</Empty>
        ) : (
          <div className="stack" style={{ gap: 12 }}>
            {(gov.transitions ?? []).map((tr) => (
              <div key={tr.id} className="transition">
                <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                  <Move from={name(tr.from_role)} to={name(tr.to_role)} strong />
                  {(tr.decided_by ?? []).length > 0 && (
                    <span className="small muted">
                      · {t("decided by")} {(tr.decided_by ?? []).map(name).join(t(" or "))}
                      {(tr.approvals ?? 1) > 1 && ` (${tr.approvals} ${t("must agree")})`}
                    </span>
                  )}
                </div>
                {(tr.criteria ?? []).length > 0 && (
                  <ul>
                    {(tr.criteria ?? []).map((c) => (
                      <li key={c.text}>
                        {c.text}
                        {c.measure && c.threshold != null && (
                          <span className="pill tag" style={{ marginLeft: 6 }} title={t("What the agent checks on GitHub")}>
                            {measureText(c, t)}
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
                {(tr.process ?? []).length > 0 && (
                  <ol>
                    {(tr.process ?? []).map((s) => (
                      <li key={s}>{s}</li>
                    ))}
                  </ol>
                )}
              </div>
            ))}
          </div>
        )}
      </Section>
      <Section title={t("History")} hint={t("newest first")}>
        {(gov.history ?? []).length === 0 ? (
          <Empty icon="clock">{t("No changes of role recorded yet.")}</Empty>
        ) : (
          <ul className="history">
            {[...(gov.history ?? [])].reverse().map((c, i) => (
              <li key={`${c.login}-${c.on}-${i}`}>
                <span className="muted mono small">{c.on}</span> <strong>@{c.login}</strong> <Move from={name(c.from_role)} to={name(c.to_role)} />
                <span className="small muted">
                  {" "}
                  · {t("by")} {c.by}
                  {c.reason && ` · ${c.reason}`}
                </span>
                {(c.evidence ?? []).length > 0 && <div className="small muted">{(c.evidence ?? []).join("; ")}</div>}
              </li>
            ))}
          </ul>
        )}
      </Section>
    </div>
  );
}

type TalkState = { thread: string | null; live_run: string | null; listening: boolean; messages: ChatMessage[] };
type TurnSteps = { run: string | null; live: boolean; steps: JobStep[] };

/** The conversation with the agent that keeps the record, shared by the repository's reviewers. */
function PeopleTalk({ repo, questions, established, hasProfile }: { repo: string; questions: string[]; established: boolean; hasProfile: boolean }) {
  const t = useT();
  const client = useQueryClient();
  const box = useRef<HTMLTextAreaElement>(null);
  const [text, setText] = useState("");
  const [pendingText, setPendingText] = useState("");
  const base = `/api/repos/${repo}/governance/talk`;
  const talk = useQuery({ queryKey: ["people", repo, "talk"], queryFn: () => api.get<TalkState>(base) });
  const send = useMutation({
    mutationFn: (said: string) => api.post<ChatMessage>(base, { text: said }),
    onSettled: () => {
      setPendingText("");
      for (const queryKey of [["people"], ["memory"]]) void client.invalidateQueries({ queryKey });
    },
  });
  const running = send.isPending || !!talk.data?.live_run;
  // While a turn runs, others' messages and whether the agent takes one move too.
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => void client.invalidateQueries({ queryKey: ["people", repo, "talk"], exact: true }), 2000);
    return () => window.clearInterval(timer);
  }, [running, client, repo]);
  const live = useQuery({
    queryKey: ["people", repo, "talk", "live"],
    queryFn: () => api.get<TurnSteps>(`${base}/steps`),
    enabled: running,
    refetchInterval: running ? 1500 : false,
  });
  const listening = !!talk.data?.listening;
  const busy = running && !listening;
  const pendingQuestions = useQuestions("pending");
  const messages = talk.data?.messages ?? [];
  const runs = new Set([...messages.map((m) => m.run_id), live.data?.run, talk.data?.live_run].filter((x): x is string => !!x));
  const waiting = (pendingQuestions.data ?? []).filter((q) => q.job_id && runs.has(q.job_id));
  const last = messages[messages.length - 1];
  const showPending = !!pendingText && !(last?.role === "user" && last.text === pendingText);

  const submit = (said: string) => {
    const clean = said.trim();
    if (!clean || busy || !hasProfile) return;
    if (!listening) setPendingText(clean);
    setText("");
    send.mutate(clean);
  };
  const answer = (q: string) => {
    setText(`${q}\n→ `);
    box.current?.focus();
  };

  return (
    <Section title={t("Talk with the agent")} hint={t("shared by this repository's reviewers")}>
      <div className="stack review-talk" style={{ gap: 10 }}>
        {!messages.length && !pendingText && (
          <span className="small muted">
            {established
              ? t("Tell it what to change: a rule, a member, a change of role. Ask it to check who should change role. It drafts each change for a maintainer to apply.")
              : t("Start by telling it where your process is written (a file in the repository or a web page), or describe the roles and how people move between them.")}
          </span>
        )}
        {messages.map((m) => (
          <Fragment key={m.id}>
            {m.role === "user" ? (
              <div className="bubble user">
                <span className="small" style={{ display: "block", opacity: 0.75 }}>
                  {m.author}
                  {m.while_running && ` · ${t("sent while it worked")}`}
                </span>
                {m.text}
              </div>
            ) : (
              <div className="bubble assistant">
                <Markdown text={m.text} className="chat-md" />
              </div>
            )}
            {m.role !== "user" && (m.changes ?? []).length > 0 && (
              <span className="small" style={{ color: "var(--ok)" }}>
                <Icon name="check" size={13} /> {t("Drafted a revision; it waits below for a maintainer")}
              </span>
            )}
          </Fragment>
        ))}
        {showPending && <div className="bubble user">{pendingText}</div>}
        {running &&
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
        {waiting.length > 0 && (
          <section className="stack" style={{ gap: 8 }}>
            <span className="kind-chip warn">{t("The agent is waiting for an answer")}</span>
            {waiting.map((q) => (
              <QuestionCard key={q.id} question={q} compact />
            ))}
          </section>
        )}
        {questions.length > 0 && (
          <div className="stack open-questions" style={{ gap: 6 }}>
            <span className="kind-chip">{t("The agent asks")}</span>
            {questions.map((q) => (
              <div key={q} className="row" style={{ gap: 8, alignItems: "flex-start" }}>
                <span className="small" style={{ flex: 1, minWidth: 0 }}>
                  {q}
                </span>
                <button className="sm" onClick={() => answer(q)}>
                  {t("Answer")}
                </button>
              </div>
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
          <textarea
            ref={box}
            value={text}
            rows={Math.min(8, Math.max(2, text.split("\n").length))}
            placeholder={
              listening
                ? t("Tell the agent something while it works…")
                : t("For example: our roles are in GOVERNANCE.md; reviewers become maintainers after six months and two maintainers' approval; add @alice as a reviewer of docs/.")
            }
            aria-label={t("Message")}
            disabled={!hasProfile}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                submit(text);
              }
            }}
          />
          <button className="primary icon" disabled={!text.trim() || busy || !hasProfile} aria-label={t("Send")}>
            <Icon name="send" size={15} />
          </button>
        </form>
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          <button className="sm" disabled={busy || !established || !hasProfile} title={!established ? t("Fill in the process first; the check follows its rules.") : undefined} onClick={() => submit(t("Check who should change role under our process, from GitHub activity."))}>
            <Icon name="refresh" size={13} />
            {t("Check who should change role")}
          </button>
          <span className="small muted">{t("Nothing changes until a maintainer applies the draft.")}</span>
        </div>
        <ErrorBox error={send.error || talk.error} />
      </div>
    </Section>
  );
}

function Move({ from, to, strong }: { from: string | null; to: string | null; strong?: boolean }) {
  const t = useT();
  const a = from ?? t("Contributor");
  const b = to ?? t("Leaves");
  const inner = (
    <>
      {a} <span className="muted">→</span> {b}
    </>
  );
  return strong ? <strong>{inner}</strong> : <span>{inner}</span>;
}

function measureText(c: Criterion, t: (s: string) => string): string {
  const n = c.threshold ?? 0;
  const per = c.window_months ? ` / ${c.window_months} ${t("months")}` : "";
  switch (c.measure) {
    case "merged_prs":
      return `≥ ${n} ${t("merged PRs")}${per}`;
    case "reviews":
      return `≥ ${n} ${t("reviews")}${per}`;
    case "months_in_role":
      return `≥ ${n} ${t("months in the role")}`;
    case "inactive_months":
      return `${t("inactive")} ≥ ${n} ${t("months")}`;
    case "nominations":
      return `≥ ${n} ${t("nominations")}`;
    default:
      return `${c.measure} ≥ ${n}`;
  }
}

function SourceLink({ repo, source }: { repo: string; source: Source }) {
  if (source.kind === "url" && /^https?:\/\//.test(source.where)) {
    return (
      <a href={source.where} target="_blank" rel="noreferrer">
        {source.where}
      </a>
    );
  }
  if (source.kind === "file") {
    return (
      <a className="mono" href={`https://github.com/${repo}/blob/HEAD/${source.where.replace(/^\/+/, "")}`} target="_blank" rel="noreferrer">
        {source.where}
      </a>
    );
  }
  return <span>{source.where}</span>;
}
