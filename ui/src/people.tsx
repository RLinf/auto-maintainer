// A repository's People tab (design 2.6): its roles, what each is responsible for,
// how people move between roles and who decides, who holds each role, and the history
// of changes. It starts as a skeleton. Reviewers talk with the agent that keeps it:
// where the process is written, rules, answers to its questions, members, or a check
// of who should change role. What the agent drafts waits here as a memory change that
// a maintainer of the repository applies. The holders of a role that maintains are the
// repository's maintainers in the service, and every member may use its dashboard.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, type ChatMessage, type JobStep } from "./api";
import { ErrorBox, Section, GrowingTextarea } from "./components";
import { useQuestions } from "./data";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { Steps } from "./jobs";
import { Markdown } from "./markdown";
import { QuestionCard } from "./pages/Questions";
import { ChangeById } from "./settingsChange";
import { DataTable, ListToolbar, useList, type Column } from "./list";
import { Skeleton, Tabs, useTab } from "./ui";
import { Avatar } from "./avatar";
import { forgeOf, links } from "./forges";

interface Role {
  id: string;
  name: string;
  level: number;
  description?: string;
  responsibilities?: string[];
  privileges?: string[];
  github_permission?: string | null;
  scoped?: boolean;
  /** Its holders are the repository's maintainers in the service. */
  maintains?: boolean;
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
  score?: number | null;
  next_step?: string;
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
  score_label?: string;
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
  const [person, setPerson] = useState<string | null>(null);
  const [seed, setSeed] = useState<{ text: string; n: number } | null>(null);
  const views = [
    { id: "members", label: t("People"), count: (data.data?.governance.members ?? []).length },
    { id: "roles", label: t("Roles & process") },
    { id: "history", label: t("History"), count: (data.data?.governance.history ?? []).length },
    { id: "talk", label: t("Talk with the agent") },
  ];
  const [view, setView] = useTab(views, "view");
  const [, setParams] = useSearchParams();
  // A step of the ladder shows the people who hold that role.
  const pickRole = (roleName: string) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete("view");
        next.set("p.f.role", roleName);
        next.delete("p.page");
        return next;
      },
      { replace: true },
    );
  if (data.isLoading) return <Skeleton rows={6} />;
  if (data.error || !data.data) return <ErrorBox error={data.error} />;
  const d = data.data;
  // The newest draft that waits for approval is what people read and answer first.
  const draft = [...d.pending].reverse().find((p) => p.governance);
  const viewingDraft = !!draft && !showCurrent;
  const gov = viewingDraft && draft?.governance ? draft.governance : d.governance;
  const roles = [...(gov.roles ?? [])].sort((a, b) => b.level - a.level);
  const name = (id: string | null | undefined) => (id ? (roles.find((r) => r.id === id)?.name ?? id) : null);
  const ask = (text: string) => {
    setPerson(null);
    setSeed({ text, n: (seed?.n ?? 0) + 1 });
    setView("talk");
  };
  const open = person ? (gov.members ?? []).find((m) => m.login === person) : undefined;

  return (
    <div className="stack people" style={{ gap: 14 }}>
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
          <span>{t("This is the starting skeleton. Tell the agent where your process is written, or describe it, and it fills this in for a maintainer to approve.")}</span>
          <button className="sm right" onClick={() => setView("talk")}>
            {t("Talk with the agent")}
          </button>
        </div>
      )}
      {d.error && (
        <div className="error">
          {t("The stored record does not read, so the skeleton is shown")}: {d.error}
        </div>
      )}
      {d.pending.length > 0 && <PendingChanges pending={d.pending} name={name} canConfigure={d.can_configure} />}
      <Ladder roles={roles} members={gov.members ?? []} onPick={pickRole} />
      <Tabs tabs={views} active={view} onChange={(v) => setView(v)} />
      {view === "members" && <MembersView repo={repo} gov={gov} roles={roles} checks={viewingDraft ? [] : d.checks} permissions={d.permissions} onOpen={setPerson} />}
      {view === "roles" && <RolesView repo={repo} gov={gov} roles={roles} name={name} />}
      {view === "history" && <HistoryView gov={gov} name={name} onOpen={setPerson} />}
      {view === "talk" && (
        <PeopleTalk repo={repo} questions={(draft?.governance ?? d.governance).open_questions ?? []} established={!!(draft?.governance ?? d.governance).established} hasProfile={d.has_profile} seed={seed} />
      )}
      {open && (
        <PersonPanel
          repo={repo}
          member={open}
          gov={gov}
          name={name}
          permission={d.permissions[open.login.toLowerCase()]}
          checks={viewingDraft ? [] : d.checks.filter((c) => c.login?.toLowerCase() === open.login.toLowerCase())}
          onClose={() => setPerson(null)}
          onAsk={() => ask(`@${open.login}: `)}
        />
      )}
    </div>
  );
}

type Named = (id: string | null | undefined) => string | null;

/** The drafts that wait for a maintainer, with whose role each would change. */
function PendingChanges({ pending, name, canConfigure }: { pending: PeopleResponse["pending"]; name: Named; canConfigure: boolean }) {
  const t = useT();
  return (
    <Section title={t("Changes waiting for a maintainer")} hint={canConfigure ? t("you can apply them") : t("a maintainer applies them")}>
      <div className="stack" style={{ gap: 12 }}>
        {pending.map((p) => (
          <div key={p.id} className="stack" style={{ gap: 8 }}>
            {p.role_changes.length > 0 && (
              <details>
                <summary className="small">
                  {p.role_changes.length} {t("changes of role")}:{" "}
                  {p.role_changes
                    .slice(0, 4)
                    .map((c) => `@${c.login}`)
                    .join(", ")}
                  {p.role_changes.length > 4 ? " …" : ""}
                </summary>
                <ul className="role-changes" style={{ marginTop: 8 }}>
                  {p.role_changes.map((c) => (
                    <li key={`${c.login}-${c.from}-${c.to}`}>
                      <strong>@{c.login}</strong> <Move from={name(c.from)} to={name(c.to)} />
                    </li>
                  ))}
                </ul>
              </details>
            )}
            <ChangeById id={p.id} />
          </div>
        ))}
      </div>
    </Section>
  );
}

/** Each role with how many hold it, highest first; a click filters the people. */
function Ladder({ roles, members, onPick }: { roles: Role[]; members: Member[]; onPick: (role: string) => void }) {
  const counts = new Map<string, number>();
  for (const m of members) counts.set(m.role, (counts.get(m.role) ?? 0) + 1);
  return (
    <div className="ladder" role="list">
      {roles.map((r) => (
        <button key={r.id} role="listitem" className={`ladder-step lvl-${Math.min(r.level, 6)}`} onClick={() => onPick(r.name)} title={r.description}>
          <span className="ellipsis">{shortName(r.name)}</span>
          <span className="n">{counts.get(r.id) ?? 0}</span>
        </button>
      ))}
    </div>
  );
}

/** A role's name without a translation in brackets, for tight places. */
function shortName(name: string): string {
  return name.replace(/\s*[（(][^）)]*[）)]\s*$/, "") || name;
}

function RoleBadge({ role }: { role: Role | undefined }) {
  if (!role) return null;
  return (
    <span className={`role-badge lvl-${Math.min(role.level, 6)}`} title={role.name}>
      {shortName(role.name)}
    </span>
  );
}

interface PersonRow extends Member {
  level: number;
  roleName: string;
  last?: RoleChange;
}

function MembersView({
  repo,
  gov,
  roles,
  checks,
  permissions,
  onOpen,
}: {
  repo: string;
  gov: Governance;
  roles: Role[];
  checks: PeopleResponse["checks"];
  permissions: Record<string, string>;
  onOpen: (login: string) => void;
}) {
  const t = useT();
  const byId = new Map(roles.map((r) => [r.id, r]));
  const lastChange = new Map<string, RoleChange>();
  for (const c of gov.history ?? []) lastChange.set(c.login.toLowerCase(), c);
  const rows: PersonRow[] = (gov.members ?? []).map((m) => ({
    ...m,
    level: byId.get(m.role)?.level ?? 0,
    roleName: byId.get(m.role)?.name ?? m.role,
    last: lastChange.get(m.login.toLowerCase()),
  }));
  const scored = rows.some((r) => r.score != null);
  const flagged = new Set(checks.map((c) => c.login?.toLowerCase()).filter(Boolean));
  const list = useList<PersonRow>({
    rows,
    key: (r) => `${r.login}:${r.role}`,
    search: (r) => [r.login, r.roleName, ...(r.areas ?? []), r.next_step ?? "", r.note ?? ""].join(" "),
    facets: [
      { id: "role", label: t("Role"), values: (r) => r.roleName },
      { id: "area", label: t("Area"), values: (r) => (r.areas ?? []).map(topArea) },
    ],
    sorts: [
      { id: "role", label: t("Role"), value: (r) => r.level * 1e6 + (r.score ?? 0) },
      ...(scored ? [{ id: "score", label: gov.score_label || t("Score"), value: (r: PersonRow) => r.score ?? null }] : []),
      { id: "name", label: t("Name"), value: (r) => r.login.toLowerCase() },
      { id: "since", label: t("Since"), value: (r) => r.since ?? null },
      { id: "changed", label: t("Last change"), value: (r) => r.last?.on ?? null },
    ],
    defaultSort: "-role",
    pageSize: 25,
    param: "p",
  });
  const columns: Column<PersonRow>[] = [
    {
      id: "person",
      header: t("Person"),
      primary: true,
      sort: "name",
      cell: (r) => (
        <span className="row" style={{ gap: 8, minWidth: 0, flexWrap: "nowrap" }}>
          <Avatar login={r.login} />
          <strong className="ellipsis">@{r.login}</strong>
          {flagged.has(r.login.toLowerCase()) && (
            <span title={t("Differs from GitHub")}>
              <Icon name="shield" size={13} />
            </span>
          )}
        </span>
      ),
    },
    { id: "role", header: t("Role"), label: t("Role"), sort: "role", cell: (r) => <RoleBadge role={byId.get(r.role)} /> },
    {
      id: "areas",
      header: t("Areas"),
      label: t("Areas"),
      cell: (r) =>
        (r.areas ?? []).length ? (
          <span className="small mono ellipsis" title={(r.areas ?? []).join("\n")}>
            {(r.areas ?? []).slice(0, 2).join(", ")}
            {(r.areas ?? []).length > 2 && <span className="muted"> +{(r.areas ?? []).length - 2}</span>}
          </span>
        ) : (
          <span className="muted small">—</span>
        ),
    },
    ...(scored
      ? [
          {
            id: "score",
            header: gov.score_label || t("Score"),
            label: gov.score_label || t("Score"),
            sort: "score",
            className: "num",
            cell: (r: PersonRow) => (r.score != null ? <span className="score-num">{formatScore(r.score)}</span> : <span className="muted">—</span>),
          },
        ]
      : []),
    {
      id: "next",
      header: t("Next step"),
      label: t("Next step"),
      cell: (r) => (r.next_step ? <span className="small clamp2">{r.next_step}</span> : <span className="muted small">—</span>),
    },
    { id: "since", header: t("Since"), label: t("Since"), sort: "since", cell: (r) => <span className="small muted nowrap">{r.since ?? "—"}</span> },
    {
      id: "changed",
      header: t("Last change"),
      label: t("Last change"),
      sort: "changed",
      cell: (r) => (r.last ? <span className="small muted nowrap">{r.last.on}</span> : <span className="muted small">—</span>),
    },
  ];
  return (
    <div className="stack" style={{ gap: 10 }}>
      {checks.length > 0 && (
        <details className="strip-details">
          <summary className="small">
            <Icon name="shield" size={13} /> {checks.length} {t("differences from GitHub")}
          </summary>
          <ul className="small" style={{ margin: "6px 0 0", paddingLeft: 18 }}>
            {checks.map((c) => (
              <li key={`${c.kind}-${c.login}`}>{c.text}</li>
            ))}
          </ul>
        </details>
      )}
      <ListToolbar list={list} placeholder={t("Search people, areas, notes")} />
      <DataTable
        list={list}
        columns={columns}
        onOpen={(r) => onOpen(r.login)}
        empty={(gov.members ?? []).length ? t("Nobody matches.") : t("Nobody is listed yet. Tell the agent who holds each role.")}
      />
      <span className="small muted">
        {t("Holders of a role that maintains are this repository's maintainers here; until the record names some, its GitHub admins are. Every member may use its dashboard.")} {Object.keys(permissions).length > 0 && `${t("Checked against GitHub for")} ${Object.keys(permissions).length} ${t("people")}.`}{" "}
        <a href={links.contributors(repo)} target="_blank" rel="noreferrer">
          {t("Contributors on GitHub")}
        </a>
      </span>
    </div>
  );
}

function topArea(path: string): string {
  const parts = path.replace(/^\/+/, "").split("/").filter(Boolean);
  return parts.length > 1 ? `${parts[0]}/${parts[1]}` : parts[0] || path;
}

function formatScore(n: number): string {
  return Number.isInteger(n) ? String(n) : n.toFixed(1);
}

/** One person: role, areas, score, next step, note, GitHub, and their history. */
function PersonPanel({
  repo,
  member,
  gov,
  name,
  permission,
  checks,
  onClose,
  onAsk,
}: {
  repo: string;
  member: Member;
  gov: Governance;
  name: Named;
  permission: string | undefined;
  checks: PeopleResponse["checks"];
  onClose: () => void;
  onAsk: () => void;
}) {
  const t = useT();
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const role = (gov.roles ?? []).find((r) => r.id === member.role);
  const history = (gov.history ?? []).filter((c) => c.login.toLowerCase() === member.login.toLowerCase()).reverse();
  return (
    <aside className="job-panel person-panel" aria-label={`@${member.login}`}>
      <div className="agent-head">
        <Avatar login={member.login} size={32} />
        <div className="stack" style={{ gap: 2, minWidth: 0, flex: 1 }}>
          <strong>
            <a href={links.user(member.login)} target="_blank" rel="noreferrer">
              @{member.login}
            </a>
          </strong>
          <span className="row small" style={{ gap: 6 }}>
            <RoleBadge role={role} />
            {member.since && (
              <span className="muted">
                {t("since")} {member.since}
              </span>
            )}
          </span>
        </div>
        <button className="ghost icon" onClick={onClose} aria-label={t("Close")} title={t("Close")}>
          <Icon name="x" />
        </button>
      </div>
      <div className="agent-body stack" style={{ gap: 14 }}>
        {member.score != null && (
          <div className="row" style={{ gap: 8, alignItems: "baseline" }}>
            <span className="score-big">{formatScore(member.score)}</span>
            <span className="small muted">{gov.score_label || t("Score")}</span>
          </div>
        )}
        {member.next_step && (
          <div className="stack" style={{ gap: 2 }}>
            <span className="kind-chip">{t("Next step")}</span>
            <span>{member.next_step}</span>
          </div>
        )}
        {(member.areas ?? []).length > 0 && (
          <div className="stack" style={{ gap: 4 }}>
            <span className="kind-chip">
              {t("Areas")} · {(member.areas ?? []).length}
            </span>
            <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
              {(member.areas ?? []).map((a) => (
                <a key={a} className="area-chip mono" href={links.tree(repo, a)} target="_blank" rel="noreferrer">
                  {a}
                </a>
              ))}
            </div>
          </div>
        )}
        {member.note && (
          <div className="stack" style={{ gap: 2 }}>
            <span className="kind-chip">{t("Note")}</span>
            <Markdown text={member.note.replace(/\s*｜\s*/g, "\n\n")} className="chat-md" />
          </div>
        )}
        <div className="stack" style={{ gap: 2 }}>
          <span className="kind-chip">{forgeOf(repo).terms.forge_name}</span>
          <span className="small">
            {permission ? `${t("permission")}: ${permission}` : t("Not checked")}
            {role?.github_permission && (
              <span className="muted">
                {" "}
                · {t("the role expects")} {role.github_permission}
              </span>
            )}
          </span>
          {checks.map((c) => (
            <span key={c.kind} className="small" style={{ color: "var(--warn)" }}>
              {c.text}
            </span>
          ))}
        </div>
        <div className="stack" style={{ gap: 6 }}>
          <span className="kind-chip">
            {t("History")} · {history.length}
          </span>
          {history.length === 0 ? (
            <span className="small muted">{t("No changes of role recorded yet.")}</span>
          ) : (
            <ul className="history">
              {history.map((c, i) => (
                <li key={`${c.on}-${i}`}>
                  <span className="muted mono small">{c.on}</span> <Move from={name(c.from_role)} to={name(c.to_role)} />
                  <div className="small muted">
                    {t("by")} {c.by}
                    {c.reason && ` · ${c.reason}`}
                  </div>
                  {(c.evidence ?? []).length > 0 && (
                    <div className="small row" style={{ gap: 6, flexWrap: "wrap" }}>
                      {(c.evidence ?? []).map((e) =>
                        /^https?:\/\//.test(e) ? (
                          <a key={e} href={e} target="_blank" rel="noreferrer">
                            {e.replace(/^https:\/\/github\.com\/[^/]+\/[^/]+\/pull\//, "#")}
                          </a>
                        ) : (
                          <span key={e} className="muted">
                            {e}
                          </span>
                        ),
                      )}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
      <div className="agent-foot">
        <div className="row">
          <span className="small muted">{t("Changes go through the agent and a maintainer's approval.")}</span>
          <button className="primary right" onClick={onAsk}>
            <Icon name="message" size={14} />
            {t("Ask the agent about")} @{member.login}
          </button>
        </div>
      </div>
    </aside>
  );
}

/** The roles, highest first; each opens to what it involves and how people get in and out. */
function RolesView({ repo, gov, roles, name }: { repo: string; gov: Governance; roles: Role[]; name: Named }) {
  const t = useT();
  const counts = new Map<string, number>();
  for (const m of gov.members ?? []) counts.set(m.role, (counts.get(m.role) ?? 0) + 1);
  const ways = (r: Role, dir: "in" | "out") => (gov.transitions ?? []).filter((tr) => (dir === "in" ? tr.to_role === r.id : tr.from_role === r.id));
  const joining = (gov.transitions ?? []).filter((tr) => !tr.to_role || !roles.some((r) => r.id === tr.to_role));
  return (
    <div className="stack" style={{ gap: 14 }}>
      {gov.summary && (
        <Section title={t("How this repository is run")}>
          <Clamped text={gov.summary} />
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
      <div className="stack role-list" style={{ gap: 8 }}>
        {roles.map((r) => (
          <details key={r.id} className="role-row">
            <summary>
              <RoleBadge role={r} />
              <span className="ellipsis role-desc small muted">{r.description}</span>
              <span className="row small" style={{ gap: 6, marginLeft: "auto", flexShrink: 0 }}>
                {r.maintains && (
                  <span className="pill status-ok" title={t("Its holders configure the repository here, assign reviewers, and decide every proposal.")}>
                    {t("maintains")}
                  </span>
                )}
                {r.github_permission && <span className="pill tag">{forgeOf(repo).terms.forge_name}: {r.github_permission}</span>}
                {r.scoped && <span className="pill tag">{t("by area")}</span>}
                <span className="pill tag">
                  {counts.get(r.id) ?? 0} {t("people")}
                </span>
              </span>
            </summary>
            <div className="role-body">
              {r.description && <p style={{ margin: 0 }}>{r.description}</p>}
              <div className="role-cols">
                {(r.responsibilities ?? []).length > 0 && (
                  <div>
                    <span className="kind-chip">{t("Responsible for")}</span>
                    <ul>
                      {(r.responsibilities ?? []).map((x) => (
                        <li key={x}>{x}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {(r.privileges ?? []).length > 0 && (
                  <div>
                    <span className="kind-chip">{t("May")}</span>
                    <ul>
                      {(r.privileges ?? []).map((x) => (
                        <li key={x}>{x}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
              {(["in", "out"] as const).map((dir) =>
                ways(r, dir).length ? (
                  <div key={dir} className="stack" style={{ gap: 8 }}>
                    <span className="kind-chip">{dir === "in" ? t("How to get here") : t("How to leave or move on")}</span>
                    {ways(r, dir).map((tr) => (
                      <TransitionCard key={tr.id} tr={tr} name={name} />
                    ))}
                  </div>
                ) : null,
              )}
            </div>
          </details>
        ))}
      </div>
      {joining.length > 0 && (
        <Section title={t("Leaving")}>
          <div className="stack" style={{ gap: 8 }}>
            {joining.map((tr) => (
              <TransitionCard key={tr.id} tr={tr} name={name} />
            ))}
          </div>
        </Section>
      )}
      {(gov.open_questions ?? []).length > 0 && (
        <Section title={t("Open questions")} hint={t("answer them in Talk with the agent")}>
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

function TransitionCard({ tr, name }: { tr: Transition; name: Named }) {
  const t = useT();
  return (
    <div className="transition">
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
  );
}

interface HistoryRow extends RoleChange {
  i: number;
}

function HistoryView({ gov, name, onOpen }: { gov: Governance; name: Named; onOpen: (login: string) => void }) {
  const t = useT();
  const rows: HistoryRow[] = (gov.history ?? []).map((c, i) => ({ ...c, i }));
  const list = useList<HistoryRow>({
    rows,
    key: (r) => String(r.i),
    search: (r) => [r.login, r.by, r.reason ?? "", name(r.from_role) ?? "", name(r.to_role) ?? ""].join(" "),
    facets: [{ id: "to", label: t("To"), values: (r) => name(r.to_role) ?? t("Leaves") }],
    sorts: [
      { id: "on", label: t("Date"), value: (r) => `${r.on}:${String(r.i).padStart(6, "0")}` },
      { id: "name", label: t("Name"), value: (r) => r.login.toLowerCase() },
    ],
    defaultSort: "-on",
    pageSize: 25,
    param: "h",
  });
  const columns: Column<HistoryRow>[] = [
    { id: "on", header: t("Date"), label: t("Date"), sort: "on", cell: (r) => <span className="mono small">{r.on}</span> },
    {
      id: "who",
      header: t("Person"),
      primary: true,
      sort: "name",
      cell: (r) => (
        <span className="row" style={{ gap: 8, minWidth: 0, flexWrap: "nowrap" }}>
          <Avatar login={r.login} size={20} />
          <strong className="ellipsis">@{r.login}</strong>
        </span>
      ),
    },
    { id: "move", header: t("Change"), label: t("Change"), cell: (r) => <span className="small"><Move from={name(r.from_role)} to={name(r.to_role)} /></span> },
    { id: "by", header: t("By"), label: t("By"), cell: (r) => <span className="small">{r.by}</span> },
    { id: "why", header: t("Reason"), label: t("Reason"), cell: (r) => <span className="small muted clamp2" title={r.reason}>{r.reason || "—"}</span> },
  ];
  return (
    <div className="stack" style={{ gap: 10 }}>
      <ListToolbar list={list} placeholder={t("Search the history")} />
      <DataTable list={list} columns={columns} onOpen={(r) => onOpen(r.login)} empty={t("No changes of role recorded yet.")} />
    </div>
  );
}

const MESSAGES_SHOWN = 2;
const QUESTIONS_SHOWN = 3;
const CLAMP_CHARS = 700;

/** A long answer, cut to a few lines until the reader opens it. */
function Clamped({ text }: { text: string }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const long = text.length > CLAMP_CHARS || text.split("\n").length > 12;
  return (
    <>
      <div className={long && !open ? "clamped" : undefined}>
        <Markdown text={text} className="chat-md" />
      </div>
      {long && (
        <button className="ghost sm" style={{ marginTop: 4 }} onClick={() => setOpen(!open)}>
          {open ? t("Show less") : t("Show more")}
        </button>
      )}
    </>
  );
}

type TalkState = { thread: string | null; live_run: string | null; listening: boolean; messages: ChatMessage[] };
type TurnSteps = { run: string | null; live: boolean; steps: JobStep[] };

/** The conversation with the agent that keeps the record, shared by the repository's reviewers. */
function PeopleTalk({
  repo,
  questions,
  established,
  hasProfile,
  seed,
}: {
  repo: string;
  questions: string[];
  established: boolean;
  hasProfile: boolean;
  seed?: { text: string; n: number } | null;
}) {
  const t = useT();
  const client = useQueryClient();
  const box = useRef<HTMLTextAreaElement>(null);
  const [text, setText] = useState("");
  const [pendingText, setPendingText] = useState("");
  const [allMessages, setAllMessages] = useState(false);
  const [allQuestions, setAllQuestions] = useState(false);
  const log = useRef<HTMLDivElement>(null);
  const base = `/api/repos/${repo}/governance/talk`;
  // Asking about someone from the people list starts the message here.
  useEffect(() => {
    if (!seed) return;
    setText(seed.text);
    window.setTimeout(() => {
      box.current?.focus();
      box.current?.setSelectionRange(seed.text.length, seed.text.length);
    }, 50);
  }, [seed]);
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
  // The latest exchange shows; earlier messages open on request, in a box of their own.
  const latest = messages.length - Math.max(0, messages.map((m) => m.role).lastIndexOf("user"));
  const earlier = Math.max(0, messages.length - Math.max(MESSAGES_SHOWN, latest));
  useEffect(() => {
    if (allMessages && log.current) log.current.scrollTop = log.current.scrollHeight;
  }, [allMessages, messages.length]);
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
        {earlier > 0 && (
          <button className="ghost sm" style={{ alignSelf: "flex-start" }} onClick={() => setAllMessages(!allMessages)}>
            <Icon name={allMessages ? "chevronDown" : "chevronRight"} size={13} />
            {allMessages ? t("Show only the latest") : `${t("Show earlier messages")} (${earlier})`}
          </button>
        )}
        <div ref={log} className={`stack talk-log ${allMessages ? "all" : ""}`} style={{ gap: 10 }}>
        {(allMessages ? messages : messages.slice(earlier)).map((m) => (
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
                <Clamped text={m.text} />
              </div>
            )}
            {m.role !== "user" && (m.changes ?? []).length > 0 && (
              <span className="small" style={{ color: "var(--ok)" }}>
                <Icon name="check" size={13} /> {t("Drafted a revision; it waits below for a maintainer")}
              </span>
            )}
          </Fragment>
        ))}
        </div>
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
            {(allQuestions ? questions : questions.slice(0, QUESTIONS_SHOWN)).map((q) => (
              <div key={q} className="row" style={{ gap: 8, alignItems: "flex-start" }}>
                <span className="small" style={{ flex: 1, minWidth: 0 }}>
                  {q}
                </span>
                <button className="sm" onClick={() => answer(q)}>
                  {t("Answer")}
                </button>
              </div>
            ))}
            {questions.length > QUESTIONS_SHOWN && (
              <button className="ghost sm" style={{ alignSelf: "flex-start" }} onClick={() => setAllQuestions(!allQuestions)}>
                {allQuestions ? t("Show fewer questions") : `${t("Show all questions")} (${questions.length})`}
              </button>
            )}
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
            ref={box}
            value={text}
            minRows={2}
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
      <a className="mono" href={links.file(repo, source.where)} target="_blank" rel="noreferrer">
        {source.where}
      </a>
    );
  }
  return <span>{source.where}</span>;
}
