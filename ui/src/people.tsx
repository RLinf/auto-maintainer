// A repository's People tab (design 2.6): its roles, what each is responsible for,
// how people move between roles and who decides, who holds each role, and the history
// of changes. It starts as a skeleton; an agent fills it in from the repository's own
// process and what reviewers tell it, and checks who should change role. What the
// agent drafts waits here as a memory change that a maintainer of the repository
// applies. Roles grant nothing in the service: GitHub permissions still decide.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "./api";
import { Empty, ErrorBox, Section, useAct } from "./components";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { Markdown } from "./markdown";
import { openJob } from "./openJob";
import { ChangeById } from "./settingsChange";
import { Skeleton, useToast } from "./ui";

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
  pending: { id: string; role_changes: { login: string; from: string | null; to: string | null }[] }[];
  can_configure: boolean;
  has_profile: boolean;
}

const KEYS = [["people"], ["memory"], ["work"]];

export function PeopleTab({ repo }: { repo: string }) {
  const t = useT();
  const data = useQuery({
    queryKey: ["people", repo],
    queryFn: () => api.get<PeopleResponse>(`/api/repos/${repo}/governance`),
  });
  if (data.isLoading) return <Skeleton rows={6} />;
  if (data.error || !data.data) return <ErrorBox error={data.error} />;
  const d = data.data;
  const gov = d.governance;
  const roles = [...(gov.roles ?? [])].sort((a, b) => b.level - a.level);
  const name = (id: string | null | undefined) => (id ? (roles.find((r) => r.id === id)?.name ?? id) : null);
  const flagged = new Set(d.checks.map((c) => c.login?.toLowerCase()).filter(Boolean));

  return (
    <div className="stack people" style={{ gap: 16 }}>
      {!gov.established && (
        <div className="strip question">
          <Icon name="sparkles" />
          <span>{t("This is the starting skeleton. Tell the agent where your process is written, or describe it, and it fills this in for a maintainer to approve.")}</span>
        </div>
      )}
      {d.error && (
        <div className="error">
          {t("The stored record does not read, so the skeleton is shown")}: {d.error}
        </div>
      )}
      <AskAgent repo={repo} established={!!gov.established} hasProfile={d.has_profile} />
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
      {d.checks.length > 0 && (
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
      {(gov.open_questions ?? []).length > 0 && (
        <Section title={t("Open questions")} hint={t("for the maintainers")}>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {(gov.open_questions ?? []).map((q) => (
              <li key={q}>{q}</li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  );
}

function AskAgent({ repo, established, hasProfile }: { repo: string; established: boolean; hasProfile: boolean }) {
  const t = useT();
  const toast = useToast();
  const [text, setText] = useState("");
  const ask = useAct((mode: "edit" | "members") => api.post<{ event: string }>(`/api/repos/${repo}/governance/agent`, { instruction: text.trim(), mode }), KEYS);
  const start = (mode: "edit" | "members") =>
    ask.mutate(mode, {
      onSuccess: (out) => {
        setText("");
        toast({
          title: mode === "members" ? t("Checking who should change role") : t("The agent is updating the record"),
          body: t("Its draft appears here for a maintainer to apply."),
          icon: "sparkles",
          action: { label: t("Watch"), onClick: () => openJob(out.event) },
        });
      },
    });
  return (
    <Section title={t("Ask the agent")} hint={t("in your own words")}>
      <div className="stack" style={{ gap: 8 }}>
        <textarea
          value={text}
          rows={3}
          onChange={(e) => setText(e.target.value)}
          placeholder={t("Where is your process written, or what should change? For example: our roles are in GOVERNANCE.md; reviewers become maintainers after six months and two maintainers' approval; add @alice as a reviewer of docs/.")}
          aria-label={t("What the agent should do")}
        />
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          <button className="primary" disabled={ask.isPending || !hasProfile || (established && !text.trim())} onClick={() => start("edit")}>
            <Icon name="sparkles" size={14} />
            {established ? t("Update with the agent") : t("Fill in with the agent")}
          </button>
          <button disabled={ask.isPending || !hasProfile || !established} title={!established ? t("Fill in the process first; the check follows its rules.") : undefined} onClick={() => start("members")}>
            <Icon name="refresh" size={14} />
            {t("Check who should change role")}
          </button>
          <span className="small muted">{t("Nothing changes until a maintainer applies the draft.")}</span>
        </div>
        <ErrorBox error={ask.error} />
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
