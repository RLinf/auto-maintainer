// People & permissions (design 2.7): each person's role in each repository the service
// serves, and the permissions it gives them. Everyone sees their own; the service's
// owner and its service admins see everyone and change config/access.yaml here: roles'
// defaults, a person's role and permissions, limits, and each repository's mapping of
// its People record roles. Only the owner changes who the service admins are. Every
// change goes through PUT /api/access, the gate of that file.

import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { Person } from "../avatar";
import { Empty, ErrorBox, PageHead, Section, useAct } from "../components";
import { useLang, useT } from "../i18n";
import { Icon } from "../icons";
import { DataTable, ListToolbar, useList, type Column } from "../list";
import { Skeleton, useToast } from "../ui";

type RoleId = "maintainer" | "reviewer" | "guest_reviewer" | "viewer" | "blocked";

interface Limits {
  tokens_per_day?: number | null;
  tokens_per_week?: number | null;
  jobs_at_once?: number | null;
  agents?: string[] | null;
  machines?: string[] | null;
}

interface PersonAccess {
  role?: RoleId | null;
  repos?: Record<string, RoleId>;
  grants?: Record<string, boolean>;
  limits?: Limits;
  note?: string;
}

interface AccessConfig {
  schema?: number;
  admins: string[];
  roles: Record<string, { grants?: Record<string, boolean>; limits?: Limits }>;
  people: Record<string, PersonAccess>;
  repos: Record<string, { people_roles: Record<string, RoleId> }>;
}

interface Catalog {
  groups: { name: string; grants: { id: string; about: string; gated: boolean }[] }[];
  roles: { id: RoleId; name: string; grants: string[] }[];
}

interface Effective {
  repo: string;
  role: RoleId | null;
  role_from: string;
  grants: Record<string, string>;
  withheld: Record<string, string>;
  limits: Limits;
}

interface Mine extends Catalog {
  login: string;
  owner: boolean;
  admin: boolean;
  manages: boolean;
  repos: Effective[];
}

interface PeopleRoleRow {
  id: string;
  name: string;
  members: number;
  maintains: boolean;
  default: RoleId;
  mapped: RoleId | null;
}

interface AccessView extends Catalog {
  path: string;
  text: string;
  problem: string | null;
  config: AccessConfig;
  admins: string[];
  owner: boolean;
  repos: { repo: string; people_roles: PeopleRoleRow[] }[];
  agents: string[];
  machines: string[];
}

interface PersonRow {
  login: string;
  signed_in: boolean;
  admin: boolean;
  set: PersonAccess | null;
  repos: Record<string, { role: RoleId | null; role_from: string }>;
}

const ROLE_CLASS: Record<RoleId, string> = {
  maintainer: "job-running",
  reviewer: "job-done",
  guest_reviewer: "access-guest",
  viewer: "job-waiting",
  blocked: "job-failed",
};

/** Where a role or permission came from, as the server says it, in the page's
 * language: the server writes these in English (``domain/access.py:resolve``). */
function useWhy(): (text: string) => string {
  const { lang } = useLang();
  const t = useT();
  return (text: string) => {
    if (lang !== "zh" || !text) return text;
    const rules: [RegExp, (m: RegExpMatchArray) => string][] = [
      [/^they maintain (.+)$/, (m) => `维护 ${m[1]}`],
      [/^their role (.+) in (.+)'s People record( \(the default mapping\))?$/, (m) => `${m[2]} 成员档案中的角色 ${m[1]}${m[3] ? "（默认映射）" : ""}`],
      [/^write access to (.+)$/, (m) => `${m[1]} 的写权限`],
      [/^set for them in (.+)$/, (m) => `已在 ${m[1]} 中为其设置`],
      [/^set for them$/, () => "已为其设置"],
      [/^neither write access to (.+) nor a place in its People record$/, (m) => `既没有 ${m[1]} 的写权限，也不在其成员档案中`],
      [/^the (.+) role, as changed for the service$/, (m) => `${t(m[1])}角色（已为整个服务修改）`],
      [/^the (.+) role$/, (m) => `${t(m[1])}角色`],
      [/^they own the service$/, () => "服务所有者"],
      [/^they are a service admin$/, () => "服务管理员"],
      [/^they operate the service$/, () => "服务操作员"],
      [/^only (.+)'s maintainers$/, (m) => `仅限 ${m[1]} 的维护者`],
      [/^only the owner and the service admins$/, () => "仅限所有者和服务管理员"],
    ];
    for (const [re, to] of rules) {
      const m = text.match(re);
      if (m) return to(m);
    }
    return text;
  };
}

function roleName(catalog: Catalog, id: RoleId | null | undefined): string {
  if (!id) return "";
  return catalog.roles.find((r) => r.id === id)?.name ?? id;
}

function RolePill({ catalog, role, title }: { catalog: Catalog; role: RoleId | null; title?: string }) {
  const t = useT();
  if (!role) return <span className="small muted" title={title}>{t("no role")}</span>;
  return (
    <span className={`pill ${ROLE_CLASS[role]}`} title={title}>
      {t(roleName(catalog, role))}
    </span>
  );
}

function toggled(set: Set<string>, value: string): Set<string> {
  const next = new Set(set);
  if (!next.delete(value)) next.add(value);
  return next;
}

/** ``config`` without empty sections, so saving sends only what it sets. */
function tidy(config: AccessConfig): AccessConfig {
  const people: Record<string, PersonAccess> = {};
  for (const [login, p] of Object.entries(config.people)) {
    const limits = Object.fromEntries(Object.entries(p.limits ?? {}).filter(([, v]) => v !== null && v !== undefined && v !== ""));
    const kept: PersonAccess = {
      ...(p.role ? { role: p.role } : {}),
      ...(p.repos && Object.keys(p.repos).length ? { repos: p.repos } : {}),
      ...(p.grants && Object.keys(p.grants).length ? { grants: p.grants } : {}),
      ...(Object.keys(limits).length ? { limits } : {}),
      ...(p.note ? { note: p.note } : {}),
    };
    if (Object.keys(kept).length) people[login] = kept;
  }
  const roles: AccessConfig["roles"] = {};
  for (const [id, r] of Object.entries(config.roles)) {
    const limits = Object.fromEntries(Object.entries(r.limits ?? {}).filter(([, v]) => v !== null && v !== undefined && v !== ""));
    if ((r.grants && Object.keys(r.grants).length) || Object.keys(limits).length) {
      roles[id] = { ...(r.grants && Object.keys(r.grants).length ? { grants: r.grants } : {}), ...(Object.keys(limits).length ? { limits } : {}) };
    }
  }
  const repos: AccessConfig["repos"] = {};
  for (const [repo, r] of Object.entries(config.repos)) {
    if (Object.keys(r.people_roles ?? {}).length) repos[repo] = r;
  }
  return { schema: config.schema ?? 1, admins: config.admins, roles, people, repos };
}

export default function AccessPage() {
  const t = useT();
  const mine = useQuery({ queryKey: ["access", "me"], queryFn: () => api.get<Mine>("/api/access/me") });
  return (
    <>
      <PageHead
        title={t("People & permissions")}
        sub={t("Each person's role in each repository the service serves, and what it lets them do.")}
      />
      <ErrorBox error={mine.error} />
      {mine.isLoading || !mine.data ? (
        <div className="card">
          <Skeleton rows={4} />
        </div>
      ) : (
        <div className="stack" style={{ gap: 18 }}>
          <YourPermissions mine={mine.data} />
          {mine.data.manages && <Manage />}
        </div>
      )}
    </>
  );
}

function YourPermissions({ mine }: { mine: Mine }) {
  const t = useT();
  const why = useWhy();
  const [open, setOpen] = useState<Set<string>>(new Set());
  if (!mine.repos.length) {
    return (
      <Section title={t("Your permissions")}>
        <Empty icon="key">{t("You have no role in any repository the service serves.")}</Empty>
      </Section>
    );
  }
  const total = mine.groups.reduce((n, g) => n + g.grants.length, 0);
  return (
    <Section title={t("Your permissions")} hint={mine.owner ? t("You own the service.") : mine.admin ? t("You are a service admin.") : undefined}>
      <div className="stack" style={{ gap: 14 }}>
        {mine.repos.map((e) => {
          // With several repositories, each one's list waits to be opened.
          const shown = mine.repos.length === 1 || open.has(e.repo);
          const held = Object.keys(e.grants).length;
          return (
            <div key={e.repo} className="stack" style={{ gap: 8 }}>
              <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                <strong>{e.repo}</strong>
                <RolePill catalog={mine} role={e.role} />
                <span className="small muted">{why(e.role_from)}</span>
                <span className="small muted">
                  {held}/{total} {t("permissions")}
                </span>
                {mine.repos.length > 1 && (
                  <button className="sm ghost right" aria-expanded={shown} onClick={() => setOpen(toggled(open, e.repo))}>
                    <Icon name={shown ? "chevronDown" : "chevronRight"} size={13} />
                    {shown ? t("Hide") : t("Show")}
                  </button>
                )}
              </div>
              {shown && <GrantList catalog={mine} grants={e.grants} withheld={e.withheld} />}
              <LimitLine limits={e.limits} />
            </div>
          );
        })}
      </div>
    </Section>
  );
}

function GrantList({ catalog, grants, withheld }: { catalog: Catalog; grants: Record<string, string>; withheld: Record<string, string> }) {
  const t = useT();
  const why = useWhy();
  return (
    <div className="access-groups">
      {catalog.groups.map((g) => (
        <div key={g.name} className="stack" style={{ gap: 2 }}>
          <span className="small muted">{t(g.name)}</span>
          {g.grants.map((grant) => {
            const held = grants[grant.id];
            return (
              <span key={grant.id} className="small" style={{ opacity: held ? 1 : 0.5 }} title={held ? `${t("from")} ${why(held)}` : why(withheld[grant.id] ?? "") || t(grant.about)}>
                <Icon name={held ? "check" : "x"} size={11} /> {t(grant.about)}
              </span>
            );
          })}
        </div>
      ))}
    </div>
  );
}

function LimitLine({ limits }: { limits: Limits }) {
  const t = useT();
  const parts = [
    limits.tokens_per_day != null && `${limits.tokens_per_day.toLocaleString()} ${t("tokens a day")}`,
    limits.tokens_per_week != null && `${limits.tokens_per_week.toLocaleString()} ${t("tokens a week")}`,
    limits.jobs_at_once != null && `${limits.jobs_at_once} ${t("jobs at once")}`,
    limits.agents && `${t("agents")}: ${limits.agents.join(", ") || t("none")}`,
    limits.machines && `${t("machines")}: ${limits.machines.join(", ") || t("none")}`,
  ].filter(Boolean);
  if (!parts.length) return null;
  return (
    <span className="small muted">
      {t("Limits")}: {parts.join(" · ")}
    </span>
  );
}

function Manage() {
  const t = useT();
  const toast = useToast();
  const view = useQuery({ queryKey: ["access", "file"], queryFn: () => api.get<AccessView>("/api/access") });
  const people = useQuery({ queryKey: ["access", "people"], queryFn: () => api.get<PersonRow[]>("/api/access/people") });
  const [draft, setDraft] = useState<AccessConfig | null>(null);
  useEffect(() => {
    if (view.data) setDraft(view.data.config);
  }, [view.data]);
  const save = useAct((config: AccessConfig) => api.put<{ applied: boolean; reason: string }>("/api/access", { config: tidy(config), title: "access: People & permissions" }), [["access"]]);
  const dirty = useMemo(() => !!(view.data && draft && JSON.stringify(tidy(draft)) !== JSON.stringify(tidy(view.data.config))), [view.data, draft]);
  if (view.error) return <ErrorBox error={view.error} />;
  if (!view.data || !draft) {
    return (
      <div className="card">
        <Skeleton rows={6} />
      </div>
    );
  }
  const v = view.data;
  const submit = () =>
    save.mutate(draft, {
      onSuccess: (out) => toast({ title: out.applied ? t("Saved") : t("Waiting for the owner"), body: out.applied ? undefined : out.reason, icon: out.applied ? "check" : "inbox" }),
      onError: (e) => toast({ title: t("Not saved"), body: String((e as Error).message), icon: "x" }),
    });
  return (
    <>
      {v.problem && (
        <div className="card small" style={{ borderColor: "var(--bad)" }}>
          <strong>{t("The stored file is not in force")}</strong>: <span className="mono">{v.problem}</span>
        </div>
      )}
      <PeopleSection v={v} rows={people.data ?? []} loading={people.isLoading} error={people.error} draft={draft} setDraft={setDraft} />
      <RolesSection v={v} draft={draft} setDraft={setDraft} />
      <MappingSection v={v} draft={draft} setDraft={setDraft} />
      <AdminsSection v={v} draft={draft} setDraft={setDraft} />
      <FileSection v={v} />
      {dirty && (
        <div className="card row access-save" style={{ gap: 8 }}>
          <span className="small">{t("You have changes that are not saved.")}</span>
          <button className="sm ghost right" onClick={() => setDraft(v.config)}>
            {t("Discard")}
          </button>
          <button className="sm primary" disabled={save.isPending} onClick={submit}>
            {t("Save")}
          </button>
        </div>
      )}
      <ErrorBox error={save.error} />
    </>
  );
}

interface DraftProps {
  v: AccessView;
  draft: AccessConfig;
  setDraft: (c: AccessConfig) => void;
}

function PeopleSection({ v, rows, loading, error, draft, setDraft }: DraftProps & { rows: PersonRow[]; loading: boolean; error: unknown }) {
  const t = useT();
  const why = useWhy();
  const [open, setOpen] = useState<string | null>(null);
  const editor = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // Opened from anywhere in a long list: bring it into view.
    if (open) editor.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [open]);
  const repos = v.repos.map((r) => r.repo);
  const list = useList<PersonRow>({
    rows,
    key: (p) => p.login,
    search: (p) => `${p.login} ${Object.values(p.repos).map((r) => r.role ?? "").join(" ")}`,
    facets: [
      { id: "role", label: t("Role"), values: (p) => Object.values(p.repos).map((r) => roleName(v, r.role) || t("no role")) },
      { id: "signed", label: t("Signed in"), values: (p) => (p.signed_in ? t("signed in") : t("not signed in")) },
    ],
    sorts: [{ id: "login", label: t("Name"), value: (p) => p.login.toLowerCase() }],
    defaultSort: "login",
    pageSize: 25,
    param: "people",
  });
  const columns: Column<PersonRow>[] = [
    {
      id: "person",
      header: t("Person"),
      primary: true,
      sort: "login",
      cell: (p) => (
        <span className="row" style={{ gap: 6 }}>
          <Person login={p.login} />
          {p.admin && <span className="pill">{t("service admin")}</span>}
          {draft.people[p.login] && <span className="small muted">{t("set here")}</span>}
        </span>
      ),
    },
    ...repos.map((repo) => ({
      id: `repo-${repo}`,
      header: repo,
      cell: (p: PersonRow) => <RolePill catalog={v} role={p.repos[repo]?.role ?? null} title={why(p.repos[repo]?.role_from ?? "")} />,
    })),
    { id: "signed", header: t("Signed in"), cell: (p) => (p.signed_in ? <Icon name="check" size={13} /> : <span className="muted">—</span>), width: 90 },
  ];
  return (
    <Section
      title={t("People")}
      hint={t("Everyone signed in, in a People record, or set here. Open a person to change their role or permissions.")}
      aside={<AddPerson draft={draft} setDraft={setDraft} onAdd={setOpen} />}
    >
      <div className="stack" style={{ gap: 12 }}>
        <ListToolbar list={list} placeholder={t("Search people")} />
        <ErrorBox error={error} />
        {open && (
          <div ref={editor} className="access-editor-wrap">
            <PersonEditor v={v} login={open} row={rows.find((r) => r.login === open)} draft={draft} setDraft={setDraft} onClose={() => setOpen(null)} />
          </div>
        )}
        {loading ? <Skeleton rows={6} /> : <DataTable list={list} columns={columns} onOpen={(p) => setOpen(open === p.login ? null : p.login)} empty={<Empty>{t("Nobody yet.")}</Empty>} />}
      </div>
    </Section>
  );
}

function AddPerson({ draft, setDraft, onAdd }: { draft: AccessConfig; setDraft: (c: AccessConfig) => void; onAdd: (login: string) => void }) {
  const t = useT();
  const [login, setLogin] = useState("");
  const add = () => {
    const name = login.trim().replace(/^@/, "");
    if (!name) return;
    if (!draft.people[name]) setDraft({ ...draft, people: { ...draft.people, [name]: {} } });
    onAdd(name);
    setLogin("");
  };
  return (
    <div className="row access-add" style={{ gap: 8 }}>
      <input className="mono" value={login} placeholder={t("GitHub login")} onChange={(e) => setLogin(e.target.value)} onKeyDown={(e) => e.key === "Enter" && add()} />
      <button className="sm ghost" disabled={!login.trim()} onClick={add}>
        <Icon name="plus" size={13} />
        {t("Set someone's access")}
      </button>
    </div>
  );
}

function RoleSelect({ v, value, onChange, inherit }: { v: AccessView; value: RoleId | null | undefined; onChange: (r: RoleId | null) => void; inherit: string }) {
  const t = useT();
  return (
    <select value={value ?? ""} onChange={(e) => onChange((e.target.value || null) as RoleId | null)}>
      <option value="">{inherit}</option>
      {v.roles.map((r) => (
        <option key={r.id} value={r.id}>
          {t(r.name)}
        </option>
      ))}
    </select>
  );
}

/** A tri-state switch for one permission: the default, on, or off. */
function GrantSwitch({ state, onChange, label }: { state: boolean | undefined; onChange: (s: boolean | undefined) => void; label: string }) {
  const t = useT();
  const next = state === undefined ? true : state ? false : undefined;
  return (
    <button className={`sm ghost access-switch ${state === undefined ? "" : state ? "on" : "off"}`} title={label} onClick={() => onChange(next)}>
      {state === undefined ? t("default") : state ? t("on") : t("off")}
    </button>
  );
}

function PersonEditor({ v, login, row, draft, setDraft, onClose }: DraftProps & { login: string; row?: PersonRow; onClose: () => void }) {
  const t = useT();
  const why = useWhy();
  const person = draft.people[login] ?? {};
  const set = (patch: Partial<PersonAccess>) => setDraft({ ...draft, people: { ...draft.people, [login]: { ...person, ...patch } } });
  const grants = person.grants ?? {};
  return (
    <div className="card stack access-editor" style={{ gap: 10 }}>
      <div className="row" style={{ gap: 8 }}>
        <Person login={login} size={20} />
        <button className="sm ghost right" onClick={onClose}>
          {t("Close")}
        </button>
      </div>
      <label className="row small" style={{ gap: 8, flexWrap: "wrap" }}>
        {t("Role everywhere")}
        <RoleSelect v={v} value={person.role} inherit={t("From GitHub and the People record")} onChange={(role) => set({ role })} />
      </label>
      {v.repos.map(({ repo }) => (
        <label key={repo} className="row small" style={{ gap: 8, flexWrap: "wrap" }}>
          {t("Role in")} <span className="mono">{repo}</span>
          <RoleSelect
            v={v}
            value={person.repos?.[repo]}
            inherit={t("As above")}
            onChange={(role) => {
              const repos = { ...(person.repos ?? {}) };
              if (role) repos[repo] = role;
              else delete repos[repo];
              set({ repos });
            }}
          />
          {row?.repos[repo] && (
            <span className="muted">
              {t("now")}: {t(roleName(v, row.repos[repo].role)) || t("no role")} ({why(row.repos[repo].role_from)})
            </span>
          )}
        </label>
      ))}
      <span className="small muted">{t("Permissions for this person, over their role's")}</span>
      <div className="access-groups">
        {v.groups.map((g) => (
          <div key={g.name} className="stack" style={{ gap: 4 }}>
            <span className="small muted">{t(g.name)}</span>
            {g.grants.map((grant) => (
              <span key={grant.id} className="access-grant small">
                <GrantSwitch
                  state={grants[grant.id]}
                  label={t(grant.about)}
                  onChange={(s) => {
                    const next = { ...grants };
                    if (s === undefined) delete next[grant.id];
                    else next[grant.id] = s;
                    set({ grants: next });
                  }}
                />
                <span>
                  {t(grant.about)}
                  {grant.gated && <span className="muted"> ({t("only the repository's maintainers")})</span>}
                </span>
              </span>
            ))}
          </div>
        ))}
      </div>
      <LimitsForm v={v} limits={person.limits ?? {}} onChange={(limits) => set({ limits })} inherit={t("the role's")} />
      <label className="stack small" style={{ gap: 2 }}>
        {t("Note")}
        <input value={person.note ?? ""} placeholder={t("Why, for the other admins")} onChange={(e) => set({ note: e.target.value })} />
      </label>
    </div>
  );
}

function LimitsForm({ v, limits, onChange, inherit }: { v: AccessView; limits: Limits; onChange: (l: Limits) => void; inherit: string }) {
  const t = useT();
  const num = (key: "tokens_per_day" | "tokens_per_week" | "jobs_at_once", label: string) => (
    <label className="stack small" style={{ gap: 2 }}>
      {label}
      <input
        type="number"
        min={0}
        style={{ width: 140 }}
        value={limits[key] ?? ""}
        placeholder={inherit}
        onChange={(e) => onChange({ ...limits, [key]: e.target.value === "" ? null : Math.max(0, Math.floor(Number(e.target.value))) })}
      />
    </label>
  );
  const pick = (key: "agents" | "machines", label: string, all: string[]) => (
    <label className="stack small" style={{ gap: 2 }}>
      {label}
      <span className="row" style={{ gap: 6, flexWrap: "wrap" }}>
        <label className="row" style={{ gap: 4 }}>
          <input type="checkbox" checked={limits[key] == null} onChange={(e) => onChange({ ...limits, [key]: e.target.checked ? null : [] })} />
          {t("every one")}
        </label>
        {limits[key] != null &&
          all.map((name) => (
            <label key={name} className="row mono" style={{ gap: 4 }}>
              <input
                type="checkbox"
                checked={limits[key]!.includes(name)}
                onChange={(e) => onChange({ ...limits, [key]: e.target.checked ? [...limits[key]!, name] : limits[key]!.filter((n) => n !== name) })}
              />
              {name}
            </label>
          ))}
      </span>
    </label>
  );
  return (
    <div className="stack" style={{ gap: 6 }}>
      <span className="small muted">{t("Limits; empty uses")} {inherit}</span>
      <div className="row" style={{ gap: 12, flexWrap: "wrap" }}>
        {num("tokens_per_day", t("Tokens a day"))}
        {num("tokens_per_week", t("Tokens a week"))}
        {num("jobs_at_once", t("Jobs at once"))}
      </div>
      {pick("agents", t("The service's agents they may use"), v.agents)}
      {pick("machines", t("The service's test machines they may use"), v.machines)}
    </div>
  );
}

function RolesSection({ v, draft, setDraft }: DraftProps) {
  const t = useT();
  const [limitsOf, setLimitsOf] = useState<RoleId | null>(null);
  const change = (role: RoleId, grant: string, s: boolean | undefined) => {
    const current = draft.roles[role] ?? {};
    const grants = { ...(current.grants ?? {}) };
    if (s === undefined) delete grants[grant];
    else grants[grant] = s;
    setDraft({ ...draft, roles: { ...draft.roles, [role]: { ...current, grants } } });
  };
  const roles = v.roles.filter((r) => r.id !== "blocked");
  return (
    <Section title={t("Roles")} hint={t("What each role lets people do. A check is the default; press a cell to turn it on or off for everyone with the role, and again for the default.")}>
      <div className="access-grid-wrap">
        <table className="access-grid">
          <thead>
            <tr>
              <th />
              {roles.map((r) => (
                <th key={r.id}>{t(r.name)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {v.groups.map((g) => [
              <tr key={g.name} className="access-group">
                <td colSpan={roles.length + 1} className="small muted">
                  {t(g.name)}
                </td>
              </tr>,
              ...g.grants.map((grant) => (
                <tr key={grant.id}>
                  <td className="small">
                    {t(grant.about)}
                    {grant.gated && <span className="muted"> ({t("only the repository's maintainers")})</span>}
                  </td>
                  {roles.map((r) => {
                    const base = r.grants.includes(grant.id);
                    const set = draft.roles[r.id]?.grants?.[grant.id];
                    const on = set ?? base;
                    return (
                      <td key={r.id} className="access-cell">
                        <button
                          className={`sm ghost ${set === undefined ? "" : "changed"}`}
                          title={set === undefined ? t("the default") : t("changed for the service")}
                          aria-label={`${t(r.name)}: ${t(grant.about)}`}
                          onClick={() => change(r.id, grant.id, set === undefined ? !base : undefined)}
                        >
                          {on ? <Icon name="check" size={13} /> : <span className="muted">—</span>}
                        </button>
                      </td>
                    );
                  })}
                </tr>
              )),
            ])}
          </tbody>
        </table>
      </div>
      <div className="row" style={{ gap: 6, flexWrap: "wrap", marginTop: 8 }}>
        <span className="small muted">{t("Limits for a role")}:</span>
        {roles.map((r) => (
          <button key={r.id} className={`sm ${limitsOf === r.id ? "primary" : "ghost"}`} onClick={() => setLimitsOf(limitsOf === r.id ? null : r.id)}>
            {t(r.name)}
          </button>
        ))}
      </div>
      {limitsOf && (
        <div className="card" style={{ marginTop: 8 }}>
          <LimitsForm
            v={v}
            limits={draft.roles[limitsOf]?.limits ?? {}}
            inherit={t("no limit")}
            onChange={(limits) => setDraft({ ...draft, roles: { ...draft.roles, [limitsOf]: { ...(draft.roles[limitsOf] ?? {}), limits } } })}
          />
        </div>
      )}
    </Section>
  );
}

function MappingSection({ v, draft, setDraft }: DraftProps) {
  const t = useT();
  const withRoles = v.repos.filter((r) => r.people_roles.length);
  return (
    <Section title={t("People records")} hint={t("Which service role each role in a repository's People record gives its members. GitHub write access still gives at least Reviewer.")}>
      {!withRoles.length ? (
        <Empty icon="repo">{t("No repository the service serves has a People record yet.")}</Empty>
      ) : (
        <div className="stack" style={{ gap: 12 }}>
          {withRoles.map(({ repo, people_roles }) => (
            <div key={repo} className="stack" style={{ gap: 4 }}>
              <strong>{repo}</strong>
              {people_roles.map((pr) => {
                const mapped = draft.repos[repo]?.people_roles?.[pr.id];
                return (
                  <label key={pr.id} className="row small" style={{ gap: 8, flexWrap: "wrap" }}>
                    <span className="access-role-name">
                      {pr.name} <span className="muted">({pr.members})</span>
                      {pr.maintains && <span className="muted"> · {t("maintains")}</span>}
                    </span>
                    <RoleSelect
                      v={v}
                      value={mapped}
                      inherit={`${t("Default")}: ${t(roleName(v, pr.default))}`}
                      onChange={(role) => {
                        const roles = { ...(draft.repos[repo]?.people_roles ?? {}) };
                        if (role) roles[pr.id] = role;
                        else delete roles[pr.id];
                        setDraft({ ...draft, repos: { ...draft.repos, [repo]: { people_roles: roles } } });
                      }}
                    />
                  </label>
                );
              })}
            </div>
          ))}
        </div>
      )}
    </Section>
  );
}

function AdminsSection({ v, draft, setDraft }: DraftProps) {
  const t = useT();
  const [login, setLogin] = useState("");
  return (
    <Section title={t("Service admins")} hint={v.owner ? t("They manage people's access with you; only you name them.") : t("Only the service's owner names the service admins.")}>
      <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
        {draft.admins.length === 0 && <span className="small muted">{t("None yet: only you manage access.")}</span>}
        {draft.admins.map((a) => (
          <span key={a} className="pill row" style={{ gap: 4 }}>
            <Person login={a} />
            {v.owner && (
              <button className="sm ghost" aria-label={t("Remove")} onClick={() => setDraft({ ...draft, admins: draft.admins.filter((x) => x !== a) })}>
                <Icon name="x" size={11} />
              </button>
            )}
          </span>
        ))}
      </div>
      {v.owner && (
        <div className="row" style={{ gap: 8, marginTop: 8 }}>
          <input className="mono" style={{ flex: "0 1 220px" }} value={login} placeholder={t("GitHub login")} onChange={(e) => setLogin(e.target.value)} />
          <button
            className="sm ghost"
            disabled={!login.trim() || draft.admins.some((a) => a.toLowerCase() === login.trim().toLowerCase())}
            onClick={() => {
              setDraft({ ...draft, admins: [...draft.admins, login.trim().replace(/^@/, "")] });
              setLogin("");
            }}
          >
            <Icon name="plus" size={13} />
            {t("Add a service admin")}
          </button>
        </div>
      )}
    </Section>
  );
}

function FileSection({ v }: { v: AccessView }) {
  const t = useT();
  const [shown, setShown] = useState(false);
  return (
    <Section
      title={t("The file")}
      hint={<span className="mono">{v.path}</span>}
      aside={
        <button className="sm ghost" onClick={() => setShown(!shown)}>
          {shown ? t("Hide") : t("Show")}
        </button>
      }
    >
      {shown && <pre className="mono small access-file">{v.text || t("(empty: every role has its defaults)")}</pre>}
    </Section>
  );
}
