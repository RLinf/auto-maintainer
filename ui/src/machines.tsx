// How one repository uses the test machines the service offers it: whether it uses
// each one, the environment to set up before every reproduction, a time limit, and
// what its scripts must never do. The machines themselves (how to reach them, their
// limits) are the service's, on the Settings page.

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "./api";
import { useAgent } from "./agent";
import { Empty, ErrorBox, Section, useAct } from "./components";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { Skeleton, useToast } from "./ui";

export interface RepoMachine {
  name: string;
  description: string;
  transport: string;
  machine_setup: string;
  machine_timeout_seconds: number;
  used: boolean;
  setup: string;
  timeout_seconds: number | null;
  forbid: string[];
  protected_paths: string[];
}

export interface RepoMachines {
  repo: string;
  machines: RepoMachine[];
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

function bodyOf(m: RepoMachine): UseBody {
  return { use: m.used, setup: m.setup, timeout_seconds: m.timeout_seconds, forbid: m.forbid, protected_paths: m.protected_paths };
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
      }
    >
      <p className="small muted" style={{ marginTop: 0 }}>
        {t("The service offers these machines; this repository decides which it uses and how. A machine it does not use is not offered when someone reproduces a PR here.")}{" "}
        {d && !d.can_configure && `${t("Changes you make here are proposals for a maintainer of the repository.")}`}
      </p>
      {view.isLoading ? (
        <Skeleton rows={2} />
      ) : !d?.machines.length ? (
        <Empty icon="server">
          {t("No test machine is offered to this repository.")} <Link to="/settings?tab=labs">{t("Test machines")}</Link>
        </Empty>
      ) : (
        <div className="stack" style={{ gap: 10 }}>
          {d.machines.map((m) => (
            <MachineUse key={m.name} repo={repo} m={m} canConfigure={d.can_configure} />
          ))}
        </div>
      )}
      <ErrorBox error={view.error} />
    </Section>
  );
}

function MachineUse({ repo, m, canConfigure }: { repo: string; m: RepoMachine; canConfigure: boolean }) {
  const t = useT();
  const { save, act } = useSetMachine(repo);
  const [open, setOpen] = useState(false);
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
    setup,
    timeout_seconds: minutes ? Math.max(1, Number(minutes)) * 60 : null,
    forbid: lines(forbid),
    protected_paths: lines(paths),
  };
  const verb = canConfigure ? t("Save") : t("Propose");
  return (
    <div className={`machine-use ${m.used ? "on" : ""}`}>
      <div className="row" style={{ alignItems: "flex-start" }}>
        <Icon name="server" />
        <span className="stack" style={{ gap: 2, flex: 1, minWidth: 0 }}>
          <strong>
            {m.name} {m.used ? <span className="pill status-ok">{t("used here")}</span> : <span className="pill">{t("not used here")}</span>}
          </strong>
          <span className="small muted">
            {m.description || t("No description")} · {t("at most")} {Math.round(Math.min(m.timeout_seconds ?? Infinity, m.machine_timeout_seconds) / 60)} {t("minutes")}
            {m.used && m.setup ? (
              <>
                {" "}
                · <code>{m.setup}</code>
              </>
            ) : null}
          </span>
        </span>
        <span className="row" style={{ gap: 6 }}>
          {m.used && (
            <button className="ghost sm" onClick={() => setOpen(!open)} aria-expanded={open}>
              {t("Settings")}
              <Icon name={open ? "chevronDown" : "chevronRight"} size={12} />
            </button>
          )}
          <button
            className={m.used ? "ghost sm danger-text" : "sm"}
            disabled={act.isPending}
            onClick={() => save(m.name, { ...bodyOf(m), use: !m.used })}
          >
            {m.used ? t("Stop using") : `${t("Use it here")}${canConfigure ? "" : ` (${t("propose")})`}`}
          </button>
        </span>
      </div>
      {open && m.used && (
        <div className="grid2" style={{ gap: 10, marginTop: 10 }}>
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
            <input type="number" min={1} max={Math.round(m.machine_timeout_seconds / 60)} value={minutes} onChange={(e) => setMinutes(e.target.value)} />
          </label>
          <label className="stack" style={{ gap: 4 }}>
            <span className="small muted">{t("Scripts must not match (one regular expression per line)")}</span>
            <textarea className="mono-input" rows={2} value={forbid} onChange={(e) => setForbid(e.target.value)} placeholder="\bwget\b" />
          </label>
          <label className="stack" style={{ gap: 4 }}>
            <span className="small muted">{t("Paths scripts must never touch (one per line)")}</span>
            <textarea className="mono-input" rows={2} value={paths} onChange={(e) => setPaths(e.target.value)} placeholder="/data/checkpoints" />
          </label>
          <div className="row">
            <button className="primary sm" disabled={act.isPending} onClick={() => save(m.name, edited, () => setOpen(false))}>
              {verb}
            </button>
            <button className="ghost sm" onClick={() => setOpen(false)}>
              {t("Cancel")}
            </button>
          </div>
        </div>
      )}
      <ErrorBox error={act.error} />
    </div>
  );
}
