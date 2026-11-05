// Repositories: onboarding, the approved profile, pending candidates, settings, and agreement scores.

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, type Agreement, type GateDecision, type MemoryChange, type ProfileResponse, type RepoRow } from "../api";
import { Diff, Empty, ErrorBox, Loading, Section, Status, useAct, When } from "../components";
import { useT } from "../i18n";

export function ReposPage() {
  const t = useT();
  const repos = useQuery({ queryKey: ["repos"], queryFn: () => api.get<RepoRow[]>("/api/repos") });
  const [name, setName] = useState("");
  const add = useAct((repo: string) => api.post("/api/repos", { repo }), [["repos"]]);
  return (
    <>
      <h1>{t("Repositories")}</h1>
      <ErrorBox error={repos.error} />
      {repos.isLoading ? <Loading /> : !repos.data?.length ? <Empty>No repositories you maintain are configured.</Empty> : (
        <div className="card table-wrap">
          <table>
            <thead>
              <tr>
                <th>{t("Repository")}</th>
                <th>State</th>
                <th className="num">Open PRs</th>
                <th className="num">Proposals</th>
                <th>Profile</th>
                <th className="hide-narrow">Last poll</th>
              </tr>
            </thead>
            <tbody>
              {repos.data.map((r) => (
                <tr key={r.repo}>
                  <td>
                    <Link to={`/repos/${r.repo}`}>{r.repo}</Link>
                  </td>
                  <td className="row">
                    <Status value={r.active ? "executed" : "pending"} />
                    <span className="small">{r.active ? "active" : "waiting for an approved profile"}</span>
                    {r.paused && <span className="pill status-bad">paused</span>}
                  </td>
                  <td className="num">{r.open_prs}</td>
                  <td className="num">{r.pending_proposals}</td>
                  <td className="small">
                    {r.candidate_error ? <span style={{ color: "var(--bad)" }}>{r.candidate_error}</span> : r.candidate_pending ? "candidate pending" : "up to date"}
                  </td>
                  <td className="small hide-narrow">
                    <When at={r.last_poll_at} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Section title={t("Add repository")}>
        <form
          className="row"
          onSubmit={(e) => {
            e.preventDefault();
            add.mutate(name.trim());
          }}
        >
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="owner/repo" />
          <button className="primary" disabled={!/^[\w.-]+\/[\w.-]+$/.test(name.trim()) || add.isPending}>
            {t("Add repository")}
          </button>
          {add.isSuccess && <span className="small muted">Onboarding started. Approve the drafted profile in Memory when it appears.</span>}
        </form>
        <ErrorBox error={add.error} />
      </Section>
    </>
  );
}

export function RepoDetailPage() {
  const t = useT();
  const { owner, repo } = useParams();
  const full = `${owner}/${repo}`;
  const base = `/api/repos/${full}`;
  const profile = useQuery({ queryKey: ["profile", full], queryFn: () => api.get<ProfileResponse>(`${base}/profile`) });
  const agreement = useQuery({ queryKey: ["profile", full, "agreement"], queryFn: () => api.get<Agreement>(`${base}/agreement`) });
  const repos = useQuery({ queryKey: ["repos"], queryFn: () => api.get<RepoRow[]>("/api/repos") });
  const row = repos.data?.find((r) => r.repo === full);
  const keys = [["profile"], ["repos"], ["memory"]];
  const approve = useAct((hash: string) => api.post<{ change: MemoryChange | null }>(`${base}/profile/approve`, { hash }), keys);
  const pause = useAct(() => api.post(`${base}/pause`, { reason: "paused from the dashboard" }), keys);
  const resume = useAct(() => api.post(`${base}/resume`), keys);

  if (profile.isLoading) return <Loading />;
  if (profile.error) return <ErrorBox error={profile.error} />;
  const p = profile.data!;
  const candidateHash = p.candidate.candidate_hash;

  return (
    <>
      <div className="row">
        <h1 style={{ margin: 0 }}>{full}</h1>
        {row?.paused ? (
          <button onClick={() => resume.mutate(undefined)}>{t("Resume")}</button>
        ) : (
          <button className="danger" onClick={() => pause.mutate(undefined)}>
            {t("Pause")}
          </button>
        )}
        {!p.can_configure && <span className="small muted">{p.gate.reason}</span>}
      </div>
      <ErrorBox error={pause.error || resume.error} />

      <div className="grid2">
        <Section title={t("Approved profile")}>
          {p.lock ? (
            <div className="stack small">
              <div>
                Hash <span className="mono">{p.lock.hash}</span> · approved by {p.lock.approved_by.join(", ") || "—"} · <When at={p.lock.approved_at} /> · source {p.lock.source}
              </div>
              <table>
                <thead>
                  <tr>
                    <th>Layer</th>
                    <th>Approved hash</th>
                    <th>Current hash</th>
                  </tr>
                </thead>
                <tbody>
                  {p.layers.map((l) => (
                    <tr key={l.name}>
                      <td>
                        {l.name} <span className="muted">({l.source})</span>
                      </td>
                      <td className="mono">{p.lock?.layers[l.name] ?? "—"}</td>
                      <td className="mono" style={{ color: p.lock?.layers[l.name] === l.hash ? undefined : "var(--warn)" }}>
                        {l.hash}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty>No approved profile yet. The service only reads this repository until a maintainer approves one.</Empty>
          )}
        </Section>
        <Section title={t("Agreement")}>
          {agreement.data && Object.keys(agreement.data.kinds).length ? (
            <>
            <p className="muted">{agreement.data.shadowed} shadow actions recorded.</p>
            <table>
              <thead>
                <tr>
                  <th>Action kind</th>
                  <th className="num">Matched</th>
                  <th className="num">Compared</th>
                  <th className="num">Agreement</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(agreement.data.kinds).map(([kind, a]) => (
                  <tr key={kind}>
                    <td className="mono">{kind}</td>
                    <td className="num">{a.matched}</td>
                    <td className="num">{a.total}</td>
                    <td className="num">{a.agreement === null ? "—" : `${Math.round(a.agreement * 100)}%`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            </>
          ) : (
            <Empty>No shadow actions to compare yet.</Empty>
          )}
          <ErrorBox error={agreement.error} />
        </Section>
      </div>

      <Section
        title={t("Pending candidate")}
        aside={
          candidateHash ? (
            <button className="primary" disabled={!p.can_configure || approve.isPending} title={p.can_configure ? "" : p.gate.reason} onClick={() => approve.mutate(candidateHash)}>
              {t("Approve")} {candidateHash}
            </button>
          ) : null
        }
      >
        {p.candidate.candidate_error && <div className="error">{p.candidate.candidate_error}</div>}
        {candidateHash ? <Diff text={p.candidate.candidate_diff} /> : <Empty>The approved profile matches every layer.</Empty>}
        {approve.data?.change && (
          <div className="notice small">
            Memory change {approve.data.change.id} is {approve.data.change.state}.
          </div>
        )}
        <ErrorBox error={approve.error} />
      </Section>

      <SettingsEditor base={base} initial={p.memory_layer} canConfigure={p.can_configure} gateReason={p.gate.reason} />
    </>
  );
}

function SettingsEditor({ base, initial, canConfigure, gateReason }: { base: string; initial: string; canConfigure: boolean; gateReason: string }) {
  const t = useT();
  const [text, setText] = useState(initial);
  const [title, setTitle] = useState("profile: update settings");
  useEffect(() => setText(initial), [initial]);
  const save = useAct(
    () => api.put<{ change: MemoryChange | null; approved: boolean; gate: GateDecision; diff: string }>(`${base}/settings`, { yaml: text, title }),
    [["profile"], ["memory"], ["repos"]],
  );
  return (
    <Section title={t("Memory layer")}>
      <p className="small muted">
        The private layer of this repository's profile. Saving opens a memory change; it applies only after a maintainer of this repository approves it.
        {canConfigure ? " You qualify, so saving is your approval." : ` ${gateReason}`}
      </p>
      <textarea value={text} onChange={(e) => setText(e.target.value)} rows={Math.min(30, Math.max(8, text.split("\n").length + 2))} spellCheck={false} />
      <div className="row" style={{ marginTop: 8 }}>
        <input value={title} onChange={(e) => setTitle(e.target.value)} style={{ flex: 1, minWidth: 200 }} />
        <button className="primary" disabled={save.isPending || text === initial} onClick={() => save.mutate(undefined)}>
          {t("Save")}
        </button>
      </div>
      {save.data && (
        <div className="stack" style={{ marginTop: 8 }}>
          <div className="notice small">
            {save.data.approved
              ? `Saved; memory change ${save.data.change?.id} is ${save.data.change?.state}.`
              : `Saved as memory change ${save.data.change?.id}; it waits for a maintainer's approval (${save.data.gate.reason}).`}
          </div>
          <Diff text={save.data.diff} />
        </div>
      )}
      <ErrorBox error={save.error} />
    </Section>
  );
}
