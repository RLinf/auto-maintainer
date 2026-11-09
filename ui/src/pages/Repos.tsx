// Repositories: onboarding, the approved profile, pending candidates, the private
// settings layer, and agreement between the service and maintainers. Changing a
// repository's configuration needs its maintainers (the configuration gate).

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, type Agreement, type GateDecision, type MemoryChange, type ProfileResponse, type RepoRow, type ReviewGuides } from "../api";
import { useAgent } from "../agent";
import { RepoMachinesSection, useRepoMachines } from "../machines";
import { PeopleTab } from "../people";
import { UsageTab } from "../usage";
import { ProfileChanges, type Profile } from "../profileView";
import { kindLabel } from "../proposal";
import { Empty, ErrorBox, PageHead, Section, Status, timeOf, useAct, When } from "../components";
import { useRepos } from "../data";
import { useT } from "../i18n";
import { Icon } from "../icons";
import { DataTable, ListToolbar, useList, type Column } from "../list";
import { Skeleton, Tabs, useTab, useToast } from "../ui";

function repoState(r: RepoRow): string {
  if (r.paused) return "Paused";
  if (r.active) return "Active";
  return "Waiting for a profile";
}

function profileState(r: RepoRow): string {
  if (r.candidate_error) return "Error";
  if (r.candidate_pending) return "Change pending";
  return "Up to date";
}

export function ReposPage() {
  const t = useT();
  const navigate = useNavigate();
  const agent = useAgent();
  const repos = useRepos();
  const [params, setParams] = useSearchParams();
  const [adding, setAddingState] = useState(params.get("add") === "1");
  const setAdding = (open: boolean) => {
    setAddingState(open);
    if (!open && params.has("add")) {
      params.delete("add");
      setParams(params, { replace: true });
    }
  };
  const list = useList<RepoRow>({
    rows: repos.data ?? [],
    key: (r) => r.repo,
    search: (r) => `${r.repo} ${r.added_by}`,
    facets: [
      { id: "state", label: t("Status"), values: (r) => t(repoState(r)) },
      { id: "profile", label: t("Profile"), values: (r) => t(profileState(r)) },
      { id: "owner", label: t("Owner"), values: (r) => r.repo.slice(0, r.repo.lastIndexOf("/")) },
    ],
    sorts: [
      { id: "repo", label: t("Name"), value: (r) => r.repo.toLowerCase() },
      { id: "prs", label: t("Open PRs"), value: (r) => r.open_prs, descFirst: true },
      { id: "proposals", label: t("Approvals"), value: (r) => r.pending_proposals, descFirst: true },
      { id: "poll", label: t("Last poll"), value: (r) => timeOf(r.last_poll_at), descFirst: true },
      { id: "added", label: t("Added"), value: (r) => timeOf(r.added_at), descFirst: true },
    ],
    defaultSort: "repo",
  });
  const columns: Column<RepoRow>[] = [
    {
      id: "repo",
      primary: true,
      header: t("Repository"),
      sort: "repo",
      cell: (r) => (
        <span className="row" style={{ gap: 8, flexWrap: "nowrap" }}>
          <Icon name="repo" />
          <span>
            <Link to={`/repos/${r.repo}`} style={{ color: "var(--text)", fontWeight: 550 }}>
              {r.repo}
            </Link>
            <div className="small muted">
              {t("added by")} {r.added_by} · <When at={r.added_at} />
            </div>
          </span>
        </span>
      ),
    },
    {
      id: "state",
      header: t("Status"),
      cell: (r) => <Status value={r.paused ? "failed" : r.active ? "executed" : "pending"} label={t(repoState(r))} />,
    },
    {
      id: "prs",
      header: t("Open PRs"),
      sort: "prs",
      className: "num",
      cell: (r) => <Link to={`/prs?repo=${encodeURIComponent(r.repo)}`}>{r.open_prs}</Link>,
    },
    { id: "proposals", header: t("Approvals"), sort: "proposals", className: "num", cell: (r) => r.pending_proposals || <span className="muted">0</span> },
    {
      id: "profile",
      header: t("Profile"),
      cell: (r) =>
        r.candidate_error ? (
          <span className="small" style={{ color: "var(--bad)" }} title={r.candidate_error}>
            <Icon name="x" size={12} /> {t("error")}
          </span>
        ) : r.candidate_pending ? (
          <Status value="pending" label={t("change to approve")} />
        ) : (
          <span className="small muted">{t("up to date")}</span>
        ),
    },
    { id: "poll", header: t("Last poll"), sort: "poll", className: "hide-narrow small", cell: (r) => <When at={r.last_poll_at} /> },
    {
      id: "ask",
      header: "",
      width: 44,
      cell: (r) => (
        <button
          className="ghost icon sm"
          title={t("Ask agent")}
          aria-label={t("Ask agent")}
          onClick={() => agent.open({ context: { page: "repository", repo: r.repo, number: null, label: r.repo } })}
        >
          <Icon name="sparkles" size={14} />
        </button>
      ),
    },
  ];
  return (
    <>
      <PageHead
        title={t("Repositories")}
        sub={t("Each repository runs on a profile its maintainers approved. Until then the service only reads it.")}
        actions={
          <button className="primary" onClick={() => setAdding(true)}>
            <Icon name="plus" />
            {t("Add repository")}
          </button>
        }
      />
      <ListToolbar list={list} placeholder={t("Search repositories")} />
      <ErrorBox error={repos.error} />
      {repos.isLoading ? (
        <div className="card">
          <Skeleton rows={4} />
        </div>
      ) : (
        <DataTable
          list={list}
          columns={columns}
          onOpen={(r) => navigate(`/repos/${r.repo}`)}
          empty={
            <>
              {t("No repositories yet.")}{" "}
              <button className="sm" onClick={() => setAdding(true)}>
                {t("Add repository")}
              </button>
            </>
          }
        />
      )}
      {adding && <AddRepoDialog onClose={() => setAdding(false)} />}
    </>
  );
}

function AddRepoDialog({ onClose }: { onClose: () => void }) {
  const t = useT();
  const toast = useToast();
  const [name, setName] = useState("");
  const add = useAct((repo: string) => api.post("/api/repos", { repo }), [["repos"]]);
  const valid = /^[\w.-]+\/[\w.-]+$/.test(name.trim());
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="dialog" role="dialog" aria-modal="true" aria-label={t("Add repository")}>
        <form
          className="dialog-body"
          onSubmit={(e) => {
            e.preventDefault();
            add.mutate(name.trim(), {
              onSuccess: () => {
                toast({ title: t("Onboarding started"), body: t("An agent drafts the profile. Approve it on the repository's page when it appears."), icon: "repo" });
                onClose();
              },
            });
          }}
        >
          <h2>{t("Add repository")}</h2>
          <span className="small muted">
            {t("You need maintainer access on GitHub. An agent reads the repository and drafts its profile; nothing is posted until a maintainer approves it.")}
          </span>
          <input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="owner/repo" onKeyDown={(e) => e.key === "Escape" && onClose()} />
          <ErrorBox error={add.error} />
          <div className="row" style={{ justifyContent: "flex-end" }}>
            <button type="button" className="ghost" onClick={onClose}>
              {t("Cancel")}
            </button>
            <button className="primary" disabled={!valid || add.isPending}>
              {add.isPending ? t("Starting…") : t("Start onboarding")}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export function RepoDetailPage() {
  const t = useT();
  const toast = useToast();
  const agent = useAgent();
  // The whole rest of the address: names may hold more slashes (forge:group/project).
  const full = useParams()["*"] ?? "";
  const base = `/api/repos/${full}`;
  const profile = useQuery({ queryKey: ["profile", full], queryFn: () => api.get<ProfileResponse>(`${base}/profile`) });
  const repos = useRepos();
  const row = repos.data?.find((r) => r.repo === full);
  const keys = [["profile"], ["repos"], ["memory"]];
  const approve = useAct((hash: string) => api.post<{ change: MemoryChange | null }>(`${base}/profile/approve`, { hash }), keys);
  const pause = useAct(() => api.post(`${base}/pause`, { reason: "paused from the dashboard" }), keys);
  const resume = useAct(() => api.post(`${base}/resume`), keys);
  const p = profile.data;
  const candidateHash = p?.candidate.candidate_hash ?? null;
  const labs = useRepoMachines(full);
  const usedMachines = labs.data?.machines.filter((m) => m.used).length;
  const guides = useReviewGuides(full, !!profile.data?.lock);
  const tabs = [
    { id: "overview", label: t("Overview") },
    { id: "people", label: t("People") },
    { id: "labs", label: t("Test machines"), count: usedMachines },
    { id: "usage", label: t("Usage") },
    { id: "candidate", label: t("Profile change"), count: candidateHash ? 1 : 0 },
    { id: "settings", label: t("Settings") },
  ];
  const [tab, setTab] = useTab(tabs);

  if (profile.isLoading) {
    return (
      <div className="card">
        <Skeleton rows={6} />
      </div>
    );
  }
  if (profile.error || !p) return <ErrorBox error={profile.error} />;

  return (
    <>
      <PageHead
        crumbs={[{ to: "/repos", label: t("Repositories") }]}
        title={full}
        sub={
          <span className="row" style={{ gap: 8 }}>
            {row && <Status value={row.paused ? "failed" : row.active ? "executed" : "pending"} label={t(repoState(row))} />}
            {p.can_configure ? (
              <span className="row" style={{ gap: 4 }}>
                <Icon name="shield" size={13} />
                {t("You can change this repository's configuration")}
              </span>
            ) : (
              <span className="row" style={{ gap: 4 }} title={p.gate.reason}>
                <Icon name="eye" size={13} />
                {t("View only: configuration needs")} {p.gate.required}
              </span>
            )}
          </span>
        }
        actions={
          <>
            <button className="agent" onClick={() => agent.open()}>
              <Icon name="sparkles" />
              {t("Configure with the agent")}
            </button>
            <Link className="button" to={`/prs?repo=${encodeURIComponent(full)}`}>
              <Icon name="pr" />
              {row?.open_prs ?? ""} {t("PRs")}
            </Link>
            {row?.paused ? (
              <button onClick={() => resume.mutate(undefined)}>
                <Icon name="play" />
                {t("Resume")}
              </button>
            ) : (
              <button className="danger" onClick={() => pause.mutate(undefined, { onSuccess: () => toast({ title: `${full} ${t("is paused")}`, icon: "pause" }) })}>
                <Icon name="pause" />
                {t("Pause")}
              </button>
            )}
          </>
        }
      />
      <ErrorBox error={pause.error || resume.error} />
      {candidateHash && tab !== "candidate" && (
        <div className="strip question">
          <Icon name="shield" />
          <span>{t("A profile change waits for a maintainer's approval.")}</span>
          <button className="sm right" onClick={() => setTab("candidate")}>
            {t("Review it")}
          </button>
        </div>
      )}
      {guides.data?.needs_choice && tab !== "overview" && (
        <div className="strip question">
          <Icon name="question" />
          <span>{t("This repository has no review skill of its own. How would you like its PRs reviewed?")}</span>
          <button className="sm right" onClick={() => setTab("overview")}>
            {t("Choose how")}
          </button>
        </div>
      )}
      <Tabs tabs={tabs} active={tab} onChange={setTab} />

      {tab === "overview" && (
        <div className="grid2">
          <Section title={t("Approved profile")}>
            {p.lock ? (
              <div className="stack small">
                <dl className="props">
                  <dt>{t("Hash")}</dt>
                  <dd>
                    <code>{p.lock.hash}</code>
                  </dd>
                  <dt>{t("Approved by")}</dt>
                  <dd>
                    {p.lock.approved_by.join(", ") || "—"} · <When at={p.lock.approved_at} />
                  </dd>
                  <dt>{t("Source")}</dt>
                  <dd>{p.lock.source}</dd>
                </dl>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>{t("Layer")}</th>
                        <th>{t("Approved")}</th>
                        <th>{t("Current")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {p.layers.map((l) => {
                        const same = p.lock?.layers[l.name] === l.hash;
                        return (
                          <tr key={l.name}>
                            <td>
                              {l.name} <span className="muted">({l.source})</span>
                            </td>
                            <td className="mono">{p.lock?.layers[l.name] ?? "—"}</td>
                            <td className="mono" style={{ color: same ? undefined : "var(--warn)" }}>
                              {l.hash} {!same && <Icon name="clock" size={12} />}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>
            ) : (
              <Empty icon="shield">{t("No approved profile yet. The service only reads this repository until a maintainer approves one.")}</Empty>
            )}
          </Section>
          <LevelsSection repo={full} />
          <div style={{ gridColumn: "1 / -1" }}>
            <ReviewGuidesSection repo={full} />
          </div>
        </div>
      )}

      {tab === "candidate" && (
        <Section
          title={t("Profile change")}
          aside={
            candidateHash ? (
              <button
                className="primary"
                disabled={!p.can_configure || approve.isPending}
                title={p.can_configure ? "" : p.gate.reason}
                onClick={() => approve.mutate(candidateHash, { onSuccess: () => toast({ title: t("Profile approved"), icon: "check" }) })}
              >
                <Icon name="check" size={14} />
                {t("Approve")} <code style={{ background: "transparent", color: "inherit" }}>{candidateHash}</code>
              </button>
            ) : null
          }
        >
          {p.candidate.candidate_error && <div className="error">{p.candidate.candidate_error}</div>}
          {candidateHash && p.candidate.candidate ? (
            <ProfileChanges before={p.lock?.profile ?? null} after={p.candidate.candidate} yaml={p.candidate.candidate_diff} />
          ) : (
            <Empty icon="check">{t("The approved profile matches every layer.")}</Empty>
          )}
          {!p.can_configure && candidateHash && <div className="notice small" style={{ marginTop: 10 }}>{p.gate.reason}</div>}
          {approve.data?.change && (
            <div className="notice small" style={{ marginTop: 10 }}>
              {t("Memory change")} {approve.data.change.id}: {approve.data.change.state}.
            </div>
          )}
          <ErrorBox error={approve.error} />
        </Section>
      )}

      {tab === "people" && <PeopleTab repo={full} />}

      {tab === "labs" && <RepoMachinesSection repo={full} />}
      {tab === "usage" && <UsageTab repo={full} />}

      {tab === "settings" && (
        <div className="stack" style={{ gap: 16 }}>
          <RepoMachinesSection repo={full} />
          <SettingsEditor repo={full} base={base} initial={p.memory_layer} canConfigure={p.can_configure} gateReason={p.gate.reason} />
        </div>
      )}
    </>
  );
}

function SettingsEditor({
  repo,
  base,
  initial,
  canConfigure,
  gateReason,
}: {
  repo: string;
  base: string;
  initial: string;
  canConfigure: boolean;
  gateReason: string;
}) {
  const t = useT();
  const agent = useAgent();
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(initial);
  const [title, setTitle] = useState("profile: update settings");
  useEffect(() => setText(initial), [initial]);
  const save = useAct(
    () =>
      api.put<{ change: MemoryChange | null; approved: boolean; gate: GateDecision; diff: string; before: Profile | null; after: Profile }>(`${base}/settings`, {
        yaml: text,
        title,
      }),
    [["profile"], ["memory"], ["repos"]],
  );
  return (
    <Section
      title={t("Repository settings")}
      hint={t("the private layer of the profile")}
      aside={
        <span className="row">
          <button
            className="agent sm"
            onClick={() =>
              agent.open({ context: { page: "repository", repo, number: null, label: repo }, prompt: t("I want to change how the service works on this repository: ") })
            }
          >
            <Icon name="sparkles" size={14} />
            {t("Describe a change")}
          </button>
          {!editing && (
            <button className="sm" onClick={() => setEditing(true)}>
              {t("Edit YAML")}
            </button>
          )}
        </span>
      }
    >
      <p className="small muted" style={{ marginTop: 0 }}>
        {t("Tell the agent what should change and it drafts the edit for you to approve, or edit the YAML yourself. Either way the change is a memory change that applies once a maintainer of this repository approves it.")}{" "}
        {canConfigure ? t("You qualify, so saving is your approval.") : gateReason}
      </p>
      {editing ? (
        <>
          <textarea value={text} onChange={(e) => setText(e.target.value)} rows={Math.min(30, Math.max(8, text.split("\n").length + 2))} spellCheck={false} />
          <div className="row" style={{ marginTop: 8 }}>
            <input value={title} onChange={(e) => setTitle(e.target.value)} style={{ flex: 1, minWidth: 200 }} aria-label={t("Change title")} />
            <button
              type="button"
              className="ghost"
              onClick={() => {
                setText(initial);
                setEditing(false);
              }}
            >
              {t("Cancel")}
            </button>
            <button className="primary" disabled={save.isPending || text === initial} onClick={() => save.mutate(undefined)}>
              {canConfigure ? t("Save and apply") : t("Propose")}
            </button>
          </div>
        </>
      ) : (
        <pre className="pre">{initial || "# " + t("empty")}</pre>
      )}
      {save.data && (
        <div className="stack" style={{ marginTop: 8 }}>
          <div className="notice small">
            {save.data.approved
              ? `${t("Saved; memory change")} ${save.data.change?.id} ${t("is")} ${save.data.change?.state}.`
              : `${t("Saved as memory change")} ${save.data.change?.id}; ${t("it waits for a maintainer's approval")} (${save.data.gate.reason}).`}
          </div>
          <ProfileChanges before={save.data.before} after={save.data.after} yaml={save.data.diff} />
        </div>
      )}
      <ErrorBox error={save.error} />
    </Section>
  );
}

/** How the repository's reviews are written and how their agent judges a PR's
 *  quality: plain text every review, follow-up, and revision reads. The service's
 *  default applies until a maintainer changes it; saving is a settings change that a
 *  maintainer of the repository applies. */
export function useReviewGuides(repo: string, enabled = true) {
  return useQuery({
    queryKey: ["profile", repo, "review-guides"],
    queryFn: () => api.get<ReviewGuides>(`/api/repos/${repo}/review-guides`),
    enabled,
    retry: false,
  });
}

function ReviewGuidesSection({ repo }: { repo: string }) {
  const t = useT();
  const toast = useToast();
  const agent = useAgent();
  const url = `/api/repos/${repo}/review-guides`;
  const view = useReviewGuides(repo);
  const d = view.data;
  const [skill, setSkill] = useState("");
  const [instructions, setInstructions] = useState("");
  const [writing, setWriting] = useState("");
  const [quality, setQuality] = useState("");
  const [quick, setQuick] = useState(true);
  useEffect(() => {
    if (!d) return;
    setSkill(d.skill);
    setInstructions(d.instructions ?? "");
    setWriting(d.writing_guide ?? d.default_writing_guide);
    setQuality(d.quality_guide ?? d.default_quality_guide);
    setQuick(d.quick_quality);
  }, [d]);
  const save = useAct(
    () =>
      api.put<{ applied: boolean; reason: string }>(url, {
        skill,
        instructions,
        writing_guide: writing,
        quality_guide: quality,
        quick_quality: quick,
      }),
    [["profile"], ["memory"]],
  );
  const changed =
    !!d &&
    (skill !== d.skill ||
      instructions.trim() !== (d.instructions ?? "").trim() ||
      writing.trim() !== (d.writing_guide ?? d.default_writing_guide).trim() ||
      quality.trim() !== (d.quality_guide ?? d.default_quality_guide).trim() ||
      quick !== d.quick_quality);
  return (
    <Section title={t("How PRs are reviewed")} hint={t("every review, follow-up, and revision")}>
      {!d ? (
        view.isLoading ? <Skeleton rows={4} /> : null
      ) : (
        <div className="stack" style={{ gap: 14 }} id="review-setup">
          <span className="small muted">
            {t("The skill the reviewing agent follows, and plain instructions on top of it.")}{" "}
            {d.can_configure ? t("You qualify, so saving is your approval.") : t("Changes you make here are proposals for a maintainer of the repository.")}
          </span>
          {d.needs_choice && (
            <div className="strip question">
              <Icon name="question" />
              <span>
                {t("This repository has no review skill of its own, so its PRs are reviewed with the service's built-in skill. Pick a skill below, or say in your own words how they should be reviewed.")}
              </span>
            </div>
          )}
          <SkillPicker view={d} value={skill} onChange={setSkill} onRefresh={() => view.refetch()} />
          <label className="stack" style={{ gap: 6 }}>
            <span className="row" style={{ gap: 8 }}>
              <strong>{t("Review instructions")}</strong>
              <span className={`pill ${instructions.trim() ? "status-warn" : "status-ok"}`}>{instructions.trim() ? t("this repository's own") : t("none")}</span>
              <span className="spacer" />
              <button
                type="button"
                className="ghost sm"
                onClick={() =>
                  agent.open({
                    prompt: t("Help me decide how PRs in this repository should be reviewed: read its docs and recent PRs, suggest review instructions, and set them in its settings."),
                  })
                }
              >
                <Icon name="sparkles" size={13} />
                {t("Work them out with the agent")}
              </button>
            </span>
            <span className="small muted">
              {t("In your own words: what reviews here must check, what matters most, and what to leave alone. Every review follows them over the skill where the two disagree.")}
            </span>
            <textarea
              value={instructions}
              onChange={(e) => setInstructions(e.target.value)}
              rows={Math.min(14, Math.max(4, instructions.split("\n").length + 1))}
              placeholder={t("For example: Check that every new config option is documented in docs/. Changes to the scheduler need a benchmark in the description. Do not comment on formatting; CI checks it.")}
            />
          </label>
          <GuideField
            label={t("Writing")}
            hint={t("How the review comment reads: who it is for, how long it is, and how much code it shows.")}
            value={writing}
            onChange={setWriting}
            fallback={d.default_writing_guide}
          />
          <GuideField
            label={t("Quality score")}
            hint={t("How the agent scores the PR from 0 to 100. Internal: never posted. The bands are fixed: 90 excellent, 75 good, 60 fair, 40 poor.")}
            value={quality}
            onChange={setQuality}
            fallback={d.default_quality_guide}
          />
          <label className="row" style={{ gap: 8, alignItems: "flex-start" }}>
            <input type="checkbox" checked={quick} onChange={(e) => setQuick(e.target.checked)} style={{ marginTop: 3 }} />
            <span className="stack" style={{ gap: 2 }}>
              <strong>{t("Score every open PR at a glance")}</strong>
              <span className="small muted">
                {t("Before its review, an agent gives each open PR a quick score from its diff by this guide, in the background, highest priority first, so reviewers can choose what to take first. The review then gives the accurate score.")}
              </span>
            </span>
          </label>
          <QuickLessons view={d} />
          <div className="row">
            <span className="spacer" />
            <button
              className="primary"
              disabled={!changed || save.isPending}
              onClick={() =>
                save.mutate(undefined, {
                  onSuccess: (r) =>
                    toast({
                      title: r.applied ? t("Saved") : t("Change proposed"),
                      body: r.applied ? t("The next reviews follow it.") : `${t("A maintainer of the repository applies it.")} ${r.reason}`,
                      icon: r.applied ? "check" : "inbox",
                    }),
                })
              }
            >
              {d.can_configure ? t("Save and apply") : t("Propose")}
            </button>
          </div>
        </div>
      )}
      <ErrorBox error={view.error || save.error} />
    </Section>
  );
}

/** Which skill reviews follow: automatic (the repository's review skill, else the
 *  service's), one of the repository's own skills, or the service's built-in one. */
function SkillPicker({ view, value, onChange, onRefresh }: { view: ReviewGuides; value: string; onChange: (v: string) => void; onRefresh: () => void }) {
  const t = useT();
  const own = view.repo_skills.find((s) => s.reviews);
  const options: { value: string; title: string; detail: string }[] = [
    {
      value: view.default_skill,
      title: t("Automatic"),
      detail: own
        ? `${t("The repository's review skill:")} ${own.name} (${own.path})`
        : t("The repository's review skill when it adds one; until then the service's built-in skill."),
    },
    ...view.repo_skills.map((s) => ({ value: `repo:${s.path}`, title: s.name, detail: `${s.path}${s.description ? ` · ${s.description}` : ""}` })),
    ...view.service_skills.map((s) => ({ value: `service:${s.name}`, title: `${t("Built-in:")} ${s.name}`, detail: s.description || t("The service's own review skill.") })),
  ];
  const known = options.some((o) => o.value === value);
  return (
    <div className="stack" style={{ gap: 6 }}>
      <span className="row" style={{ gap: 8 }}>
        <strong>{t("Review skill")}</strong>
        <span className="small muted">
          {view.in_use.kind === "repo"
            ? `${t("Reviews follow")} ${view.in_use.name}`
            : view.in_use.kind === "missing"
              ? t("The chosen skill is no longer in the repository; reviews follow the service's.")
              : `${t("Reviews follow the built-in")} ${view.in_use.name}`}
        </span>
        <span className="spacer" />
        <button type="button" className="ghost sm" onClick={onRefresh} title={t("Read the repository's skills from GitHub again")}>
          <Icon name="refresh" size={13} />
        </button>
      </span>
      <div className="skill-options" role="radiogroup" aria-label={t("Review skill")}>
        {options.map((o) => (
          <label key={o.value} className={`skill-option${value === o.value ? " on" : ""}`}>
            <input type="radio" name="review-skill" checked={value === o.value} onChange={() => onChange(o.value)} />
            <span className="stack" style={{ gap: 2, minWidth: 0 }}>
              <strong>{o.title}</strong>
              <span className="small muted clamp2">{o.detail}</span>
            </span>
          </label>
        ))}
        {!known && (
          <label className="skill-option on">
            <input type="radio" name="review-skill" checked readOnly />
            <span className="stack" style={{ gap: 2 }}>
              <strong>{value}</strong>
              <span className="small muted">{t("Set in the profile; not among the skills found.")}</span>
            </span>
          </label>
        )}
      </div>
      {view.repo_skills.length === 0 && <span className="small muted">{t("No skills found in the repository's skill folders (.claude/skills, .agents/skills, and others) on its default branch.")}</span>}
    </div>
  );
}

/** What reviews taught the quick scores: the rules drawn from quick scores a review
 *  corrected by a wide margin, and those corrections. Learned automatically. */
function QuickLessons({ view }: { view: ReviewGuides }) {
  const t = useT();
  const rules = view.quick_lessons.rules ?? [];
  const misses = view.quick_misses;
  if (!rules.length && !misses.length) {
    return <span className="small muted">{t("Quick scores learn from reviews: when a review scores a PR far from its quick score, the next quick scores take it into account.")}</span>;
  }
  return (
    <div className="stack small" style={{ gap: 6 }}>
      <strong>
        {t("Learned from reviews")}
        {view.quick_lessons.at && (
          <span className="muted" style={{ fontWeight: 400 }}>
            {" "}
            · <When at={view.quick_lessons.at} />
          </span>
        )}
      </strong>
      {rules.length > 0 && (
        <ul style={{ margin: 0, paddingLeft: 20 }}>
          {rules.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </ul>
      )}
      {misses.length > 0 && (
        <span className="muted">
          {t("Quick scores a review corrected:")}{" "}
          {misses
            .slice(-6)
            .map((m) => `#${m.number} ${m.quick} → ${m.review}`)
            .join(" · ")}
        </span>
      )}
    </div>
  );
}

function GuideField({
  label,
  hint,
  value,
  onChange,
  fallback,
}: {
  label: string;
  hint: string;
  value: string;
  onChange: (v: string) => void;
  fallback: string;
}) {
  const t = useT();
  const isDefault = value.trim() === fallback.trim();
  return (
    <label className="stack" style={{ gap: 6 }}>
      <span className="row" style={{ gap: 8 }}>
        <strong>{label}</strong>
        <span className={`pill ${isDefault ? "status-ok" : "status-warn"}`}>{isDefault ? t("service default") : t("this repository's own")}</span>
        <span className="spacer" />
        {!isDefault && (
          <button type="button" className="ghost sm" onClick={() => onChange(fallback)}>
            {t("Restore the default")}
          </button>
        )}
      </span>
      <span className="small muted">{hint}</span>
      <textarea value={value} onChange={(e) => onChange(e.target.value)} rows={Math.min(16, Math.max(5, value.split("\n").reduce((n, line) => n + Math.max(1, Math.ceil(line.length / 110)), 1)))} />
    </label>
  );
}

const LEVELS = [
  { id: "off", label: "Off" },
  { id: "shadow", label: "Record only" },
  { id: "propose", label: "Ask first" },
  { id: "auto", label: "Automatic" },
] as const;

/** What the service does on its own in the repository, one action at a time: off,
 * record only (shadow), ask first (propose), or automatic. Beside each, how often
 * what it recorded matched what maintainers did. Changing a level is a settings change
 * a maintainer of the repository applies. */
function LevelsSection({ repo }: { repo: string }) {
  const t = useT();
  const toast = useToast();
  const base = `/api/repos/${repo}`;
  const view = useQuery({ queryKey: ["profile", repo, "agreement"], queryFn: () => api.get<Agreement>(`${base}/agreement`) });
  const set = useAct(
    ({ kind, level }: { kind: string; level: string }) => api.put<{ applied: boolean; reason: string }>(`${base}/policy/${kind}`, { level }),
    [["profile"], ["memory"]],
  );
  const d = view.data;
  const levels = d?.levels ?? {};
  const kinds = Object.keys(levels).sort((a, b) => {
    const order = (k: string) => (levels[k] === "auto" ? 0 : levels[k] === "propose" ? 1 : levels[k] === "shadow" ? 2 : 3);
    return order(a) - order(b) || a.localeCompare(b);
  });
  return (
    <Section title={t("What the service does on its own")} hint={t("per action")}>
      {view.isLoading ? (
        <Skeleton rows={4} />
      ) : !kinds.length ? (
        <Empty icon="shield">{t("No approved profile yet. The service only reads this repository until a maintainer approves one.")}</Empty>
      ) : (
        <div className="stack small" style={{ gap: 6 }}>
          <span className="muted">
            {t("Record only: the service notes what it would do. The match rate shows how often maintainers then did the same; raise an action when it is high.")}
            {!d?.can_configure && ` ${t("Changes you make here are proposals for a maintainer of the repository.")}`}
          </span>
          {kinds.map((kind) => {
            const a = d?.kinds[kind];
            return (
              <div key={kind} className="level-row">
                <span className="level-name" title={kind}>
                  {t(kindLabel(kind))}
                </span>
                <span className="level-bar" title={a ? `${a.matched}/${a.total}` : t("nothing recorded yet")}>
                  {a && a.agreement !== null && <span style={{ width: `${a.agreement * 100}%` }} />}
                </span>
                <span className="level-rate">{a && a.agreement !== null ? `${Math.round(a.agreement * 100)}%` : "—"}</span>
                <select
                  aria-label={`${t(kindLabel(kind))}: ${t("level")}`}
                  value={levels[kind]}
                  disabled={set.isPending}
                  onChange={(e) =>
                    set.mutate(
                      { kind, level: e.target.value },
                      {
                        onSuccess: (r) =>
                          toast({
                            title: r.applied ? t("Saved") : t("Change proposed"),
                            body: r.applied ? undefined : `${t("A maintainer of the repository applies it.")} ${r.reason}`,
                            icon: r.applied ? "check" : "inbox",
                          }),
                      },
                    )
                  }
                >
                  {LEVELS.map((l) => (
                    <option key={l.id} value={l.id}>
                      {t(l.label)}
                    </option>
                  ))}
                </select>
              </div>
            );
          })}
        </div>
      )}
      <ErrorBox error={view.error || set.error} />
    </Section>
  );
}
