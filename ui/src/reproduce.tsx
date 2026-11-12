// Reproduce a PR on a test machine: pick a machine and say what to check if the PR
// does not make it obvious. The review skill's reproduction agent starts at once on
// the machine, takes the GPUs the run needs when they are idle, and its record goes
// into the PR's review.

import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "./api";
import { AgentPicker, useAgentChoice } from "./agentPicker";
import { ErrorBox, useAct } from "./components";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { useRepoMachines, useSetMachine } from "./machines";
import { openJob } from "./openJob";
import { Popover, Skeleton, useToast } from "./ui";

export function ReproduceForm({ repo, number, onClose }: { repo: string; number: number; onClose: () => void }) {
  const t = useT();
  const toast = useToast();
  const view = useRepoMachines(repo, true);
  const used = (view.data?.machines ?? []).filter((m) => m.used);
  const unused = (view.data?.machines ?? []).filter((m) => !m.used);
  const { save, act: enable } = useSetMachine(repo);
  const [lab, setLab] = useState("");
  const [note, setNote] = useState("");
  const [agent, setAgent] = useAgentChoice("reproduce");
  const chosen = (used.some((m) => m.name === lab) ? lab : "") || used[0]?.name || "";
  const start = useAct(
    () => api.post<{ event: string; lab: string }>(`/api/prs/${repo}/${number}/reproduce`, { lab: chosen, note, agent: agent || null }),
    [["work"], ["pr"]],
  );
  return (
    <div className="stack" style={{ gap: 10, padding: 6, width: "min(360px, calc(100vw - 24px))", maxWidth: "100%" }}>
      <span className="small muted">{t("The review skill's reproduction agent runs it on the machine at once, taking the GPUs it needs when they are idle; its record goes into the PR's review.")}</span>
      {view.isLoading ? (
        <Skeleton rows={2} />
      ) : !used.length ? (
        <div className="small stack" style={{ gap: 8 }}>
          <span>{t("This repository uses no test machine yet.")}</span>
          {unused.map((m) => (
            <div key={m.name} className="row">
              <span className="stack" style={{ gap: 0, flex: 1 }}>
                <strong>{m.name}</strong>
                {m.description && <span className="muted">{m.description}</span>}
              </span>
              <button
                className="sm"
                disabled={enable.isPending}
                onClick={() => save(m.name, { use: true, setup: m.setup, timeout_seconds: m.timeout_seconds, forbid: m.forbid, protected_paths: m.protected_paths })}
              >
                {view.data?.can_configure ? t("Use it here") : `${t("Use it here")} (${t("propose")})`}
              </button>
            </div>
          ))}
          {!unused.length && (
            <span>
              {t("No test machine is offered to it.")} <Link to="/settings?tab=labs">{t("Set one up")}</Link>
            </span>
          )}
          <Link to={`/repos/${repo}?tab=labs`}>{t("How this repository uses test machines")}</Link>
        </div>
      ) : (
        <>
          <div className="stack" style={{ gap: 4 }} role="radiogroup" aria-label={t("Test machine")}>
            {used.map((m) => (
              <label key={m.name} className={`machine-choice ${chosen === m.name ? "on" : ""}`}>
                <input type="radio" name="machine" checked={chosen === m.name} onChange={() => setLab(m.name)} />
                <span className="stack" style={{ gap: 0 }}>
                  <strong className="small">{m.name}</strong>
                  {m.description && <span className="small muted">{m.description}</span>}
                </span>
              </label>
            ))}
          </div>
          <textarea className="prose" rows={2} placeholder={t("What should be checked? (optional)")} value={note} onChange={(e) => setNote(e.target.value)} />
          <div className="row">
            <AgentPicker value={agent} onChange={setAgent} task="reproduce" />
            <button
              className="primary right"
              disabled={!chosen || start.isPending}
              onClick={() =>
                start.mutate(undefined, {
                  onSuccess: (out) => {
                    onClose();
                    setNote("");
                    toast({
                      title: t("Reproduction started"),
                      body: `${t("The reproduction agent is starting on")} ${out.lab}.`,
                      icon: "server",
                      action: { label: t("Watch"), onClick: () => openJob(out.event) },
                    });
                  },
                })
              }
            >
              {start.isPending ? t("Starting…") : t("Start")}
            </button>
          </div>
        </>
      )}
      {used.length > 0 && unused.length > 0 && (
        <Link className="small" to={`/repos/${repo}?tab=labs`}>
          {unused.length} {t("more offered but not used here")} →
        </Link>
      )}
      <ErrorBox error={start.error || view.error || enable.error} />
    </div>
  );
}

export function ReproduceButton({ repo, number, align = "right" }: { repo: string; number: number; align?: "left" | "right" }) {
  const t = useT();
  return (
    <Popover
      align={align}
      label={t("Reproduce")}
      trigger={(isOpen, toggle) => (
        <button
          className={isOpen ? "on" : ""}
          aria-expanded={isOpen}
          title={t("Run what the PR claims on a test machine, before and after the change")}
          onClick={toggle}
        >
          <Icon name="server" size={14} />
          {t("Reproduce")}
        </button>
      )}
    >
      {(close) => <ReproduceForm repo={repo} number={number} onClose={close} />}
    </Popover>
  );
}
