// How one repository uses the test machines the service offers it: whether it uses
// each one, the environment to set up before every reproduction, a time limit, and
// what its scripts must never do. The machines themselves (how to reach them, their
// limits) are the service's, on the Settings page.

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type MachineCheck } from "./api";
import { useAgent } from "./agent";
import { Empty, ErrorBox, Section, useAct } from "./components";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { Skeleton, useToast } from "./ui";

export interface ReproducePolicy {
  level: "off" | "shadow" | "propose" | "auto";
  conditions?: {
    author?: "any" | "team" | "external";
    recommendation?: string | null;
    paths?: string[];
  };
  otherwise?: "off" | "shadow" | "propose" | "auto";
}

export interface RepoMachine {
  name: string;
  /** The signed-in person's own machine: their work uses it on any repository. */
  mine?: boolean;
  description: string;
  transport: string;
  host?: string | null;
  workdir?: string;
  machine_setup: string;
  machine_timeout_seconds: number;
  machine_forbid?: string[];
  used: boolean;
  setup: string;
  timeout_seconds: number | null;
  forbid: string[];
  protected_paths: string[];
}

export interface RepoMachines {
  repo: string;
  machines: RepoMachine[];
  /** The repository's ``lab.review`` rule: whose PRs reviews test on its machines. */
  review_policy?: ReproducePolicy | null;
  can_configure: boolean;
  reason: string;
}

export function useRepoMachines(repo: string, enabled = true) {
  return useQuery({
    queryKey: ["labs", repo],
    queryFn: () => api.get<RepoMachines>(`/api/labs?repo=${encodeURIComponent(repo)}`),
    enabled,
  });
}

interface UseBody {
  use: boolean;
  setup: string;
  timeout_seconds: number | null;
  forbid: string[];
  protected_paths: string[];
}

export function useSetMachine(repo: string) {
  const t = useT();
  const toast = useToast();
  const act = useAct(
    ({ name, body }: { name: string; body: UseBody }) =>
      api.put<{ applied: boolean; reason: string }>(`/api/repos/${repo}/labs/${name}`, body),
    [["labs"], ["profile"], ["memory"]],
  );
  const save = (name: string, body: UseBody, done?: () => void) =>
    act.mutate(
      { name, body },
      {
        onSuccess: (r) => {
          toast({
            title: r.applied ? t("Saved") : t("Change proposed"),
            body: r.applied ? undefined : `${t("A maintainer of the repository applies it.")} ${r.reason}`,
            icon: r.applied ? "check" : "inbox",
          });
          done?.();
        },
      },
    );
  return { save, act };
}

/** Whose PRs a review tests on the machine, in its own session (``lab.review``). */
const REVIEW_PRESETS = [
  {
    id: "auto",
    label: "Every PR",
    body: { level: "auto", conditions: { author: "any" }, otherwise: "shadow" },
  },
  {
    id: "team_auto",
    label: "Team PRs only",
    body: { level: "auto", conditions: { author: "team" }, otherwise: "off" },
  },
  {
    id: "off",
    label: "Off",
    body: { level: "off", conditions: { author: "any" }, otherwise: "off" },
  },
] as const;

function activePreset(policy: ReproducePolicy): string {
  const author = policy.conditions?.author ?? "any";
  if (policy.level === "auto" && author === "team") return "team_auto";
  return policy.level === "auto" ? "auto" : "off";
}

function ReviewPolicyBar({ repo, policy, canConfigure }: { repo: string; policy: ReproducePolicy; canConfigure: boolean }) {
  const t = useT();
  const toast = useToast();
  const set = useAct(
    (body: (typeof REVIEW_PRESETS)[number]["body"]) =>
      api.put<{ applied: boolean; reason: string }>(`/api/repos/${repo}/policy/lab.review`, body),
    [["labs"], ["profile"], ["memory"]],
  );
  const current = activePreset(policy);
  return (
    <div className="card stack" style={{ gap: 8, padding: "12px 14px", background: "var(--surface-2)" }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
        <div className="stack" style={{ gap: 2 }}>
          <strong className="small">{t("Which PRs reviews test on the machine")}</strong>
          <span className="small muted">
            {t("The reviewing agent tests and reproduces the PR in a session of its own on the machine, as it sees fit. An outside contributor's PR is code nobody has checked yet. Anyone with access can still click Reproduce on a PR.")}
            {!canConfigure && ` ${t("Changes you make here are proposals for a maintainer of the repository.")}`}
          </span>
        </div>
        <code className="small muted">lab.review</code>
      </div>
      <div className="segmented" role="radiogroup" aria-label={t("Which PRs reviews test on the machine")}>
        {REVIEW_PRESETS.map((p) => (
          <button
            key={p.id}
            type="button"
            role="radio"
            aria-checked={current === p.id}
            className={current === p.id ? "on" : ""}
            disabled={set.isPending}
            onClick={() =>
              set.mutate(p.body, {
                onSuccess: (r) =>
                  toast({
                    title: r.applied ? t("Saved") : t("Change proposed"),
                    body: r.applied ? undefined : `${t("A maintainer of the repository applies it.")} ${r.reason}`,
                    icon: r.applied ? "check" : "inbox",
                  }),
              })
            }
          >
            {t(p.label)}
          </button>
        ))}
      </div>
      <ErrorBox error={set.error} />
    </div>
  );
}

/** The repository page's section: every machine offered to the repository. */
export function RepoMachinesSection({ repo }: { repo: string }) {
  const t = useT();
  const agent = useAgent();
  const view = useRepoMachines(repo);
  const d = view.data;
  return (
    <Section
      title={t("Test machines")}
      hint={t("where this repository's PRs are reproduced")}
      aside={
        <div className="row" style={{ gap: 6 }}>
          <Link className="button sm ghost" to="/settings?tab=labs">
            <Icon name="server" size={13} />
            {t("Manage machines")}
          </Link>
          <button
            className="agent sm"
            onClick={() =>
              agent.open({
                context: { page: "repository", repo, number: null, label: repo },
                prompt: t("Set up how this repository uses the test machines: "),
              })
            }
          >
            <Icon name="sparkles" size={14} />
            {t("Describe a change")}
          </button>
        </div>
      }
    >
      <div className="stack" style={{ gap: 12 }}>
        <p className="small muted" style={{ margin: 0 }}>
          {t("The service offers these machines; this repository decides which it uses and how. A machine it does not use is not offered when someone reproduces a PR here.")}{" "}
          {d && !d.can_configure && `${t("Changes you make here are proposals for a maintainer of the repository.")}`}
        </p>
        {d?.review_policy && <ReviewPolicyBar repo={repo} policy={d.review_policy} canConfigure={d.can_configure} />}
        {view.isLoading ? (
          <Skeleton rows={2} />
        ) : !d?.machines.length ? (
          <Empty icon="server">
            {t("No test machine is offered to this repository.")} <Link to="/settings?tab=labs">{t("Test machines")}</Link>
          </Empty>
        ) : (
          <div className="stack" style={{ gap: 10 }}>
            {d.machines.filter((m) => !m.mine).map((m) => (
              <MachineUse key={m.name} repo={repo} m={m} canConfigure={d.can_configure} />
            ))}
          </div>
        )}
        <ErrorBox error={view.error} />
      </div>
    </Section>
  );
}

function MachineUse({ repo, m, canConfigure }: { repo: string; m: RepoMachine; canConfigure: boolean }) {
  const t = useT();
  const { save, act } = useSetMachine(repo);
  const check = useAct(() => api.post<MachineCheck>(`/api/settings/labs/${m.name}/check`), []);
  const [setup, setSetup] = useState(m.setup);
  const [minutes, setMinutes] = useState(m.timeout_seconds ? String(Math.round(m.timeout_seconds / 60)) : "");
  const [forbid, setForbid] = useState(m.forbid.join("\n"));
  const [paths, setPaths] = useState(m.protected_paths.join("\n"));
  useEffect(() => {
    setSetup(m.setup);
    setMinutes(m.timeout_seconds ? String(Math.round(m.timeout_seconds / 60)) : "");
    setForbid(m.forbid.join("\n"));
    setPaths(m.protected_paths.join("\n"));
  }, [m]);
  const lines = (s: string) =>
    s
      .split("\n")
      .map((x) => x.trim())
      .filter(Boolean);
  const edited: UseBody = {
    use: m.used,
    setup: setup.trim(),
    timeout_seconds: minutes ? Math.max(1, Number(minutes)) * 60 : null,
    forbid: lines(forbid),
    protected_paths: lines(paths),
  };
  const dirty =
    edited.setup !== m.setup.trim() ||
    edited.timeout_seconds !== m.timeout_seconds ||
    edited.forbid.join("\n") !== m.forbid.join("\n") ||
    edited.protected_paths.join("\n") !== m.protected_paths.join("\n");
  const verb = canConfigure ? t("Save") : t("Propose");
  const c = check.data;
  return (
    <div className={`machine-use stack ${m.used ? "on" : ""}`} style={{ gap: 10 }}>
      <div className="row" style={{ alignItems: "flex-start" }}>
        <Icon name="server" />
        <span className="stack" style={{ gap: 2, flex: 1, minWidth: 0 }}>
          <span className="row" style={{ gap: 6 }}>
            <strong>{m.name}</strong>
            <span className="muted small">{m.transport === "ssh" ? m.host || m.name : t("this host")}</span>
            {m.used ? <span className="pill status-ok">{t("used here")}</span> : <span className="pill">{t("not used here")}</span>}
          </span>
          <span className="small muted">
            {m.description || t("No description")} · {t("at most")} {Math.round(Math.min(m.timeout_seconds ?? Infinity, m.machine_timeout_seconds) / 60)}{" "}
            {t("minutes")}
            {m.workdir ? (
              <>
                {" "}
                · <code>{m.workdir}</code>
              </>
            ) : null}
            {m.machine_setup ? (
              <>
                {" "}
                · {t("machine setup")}: <code>{m.machine_setup}</code>
              </>
            ) : null}
          </span>
          {c && (
            <span className={`small ${c.ok ? "" : "danger-text"}`} style={{ marginTop: 2 }}>
              {c.ok ? (
                <>
                  <Icon name="check" size={12} /> {c.system} · {c.cpus} CPUs · {c.memory_gb} GB
                  {c.gpus?.length ? ` · ${c.gpus.filter((g) => g.idle).length}/${c.gpus.length} ${t("GPUs idle")}` : ` · ${t("no GPUs")}`}
                </>
              ) : (
                <>
                  <Icon name="x" size={12} /> {c.error}
                </>
              )}
            </span>
          )}
        </span>
        <span className="row" style={{ gap: 6 }}>
          <button className="ghost sm" disabled={check.isPending} onClick={() => check.mutate(undefined)}>
            {check.isPending ? t("Checking…") : t("Check")}
          </button>
          <button
            className={m.used ? "ghost sm danger-text" : "primary sm"}
            disabled={act.isPending}
            onClick={() => save(m.name, { ...edited, use: !m.used })}
          >
            {m.used ? t("Stop using") : `${t("Use it here")}${canConfigure ? "" : ` (${t("propose")})`}`}
          </button>
        </span>
      </div>
      <div className="grid2" style={{ gap: 10 }}>
        <label className="stack" style={{ gap: 4 }}>
          <span className="small muted">
            {t("Set up this repository's environment before each reproduction")}
            {m.machine_setup ? ` (${t("after the machine's")}: ${m.machine_setup})` : ""}
          </span>
          <input className="mono-input" value={setup} onChange={(e) => setSetup(e.target.value)} placeholder="source ~/envs/my-repo/bin/activate" />
        </label>
        <label className="stack" style={{ gap: 4 }}>
          <span className="small muted">
            {t("Time limit in minutes (optional)")} · {t("the machine allows")} {Math.round(m.machine_timeout_seconds / 60)}
          </span>
          <input
            type="number"
            min={1}
            max={Math.round(m.machine_timeout_seconds / 60)}
            placeholder={String(Math.round(m.machine_timeout_seconds / 60))}
            value={minutes}
            onChange={(e) => setMinutes(e.target.value)}
          />
        </label>
        <label className="stack" style={{ gap: 4 }}>
          <span className="small muted">
            {t("Scripts must not match (one regular expression per line)")}
            {m.machine_forbid?.length ? ` · ${t("machine forbids")}: ${m.machine_forbid.join(", ")}` : ""}
          </span>
          <textarea className="mono-input" rows={2} value={forbid} onChange={(e) => setForbid(e.target.value)} placeholder="\bwget\b" />
        </label>
        <label className="stack" style={{ gap: 4 }}>
          <span className="small muted">{t("Paths scripts must never touch (one per line)")}</span>
          <textarea className="mono-input" rows={2} value={paths} onChange={(e) => setPaths(e.target.value)} placeholder="/data/checkpoints" />
        </label>
      </div>
      {dirty && (
        <div className="row">
          <button className="primary sm" disabled={act.isPending} onClick={() => save(m.name, edited)}>
            {verb}
          </button>
          {!m.used && (
            <button className="sm" disabled={act.isPending} onClick={() => save(m.name, { ...edited, use: true })}>
              {canConfigure ? t("Save and use here") : `${t("Save and use here")} (${t("propose")})`}
            </button>
          )}
          <button
            className="ghost sm"
            onClick={() => {
              setSetup(m.setup);
              setMinutes(m.timeout_seconds ? String(Math.round(m.timeout_seconds / 60)) : "");
              setForbid(m.forbid.join("\n"));
              setPaths(m.protected_paths.join("\n"));
            }}
          >
            {t("Cancel")}
          </button>
        </div>
      )}
      <ErrorBox error={act.error || check.error} />
    </div>
  );
}
