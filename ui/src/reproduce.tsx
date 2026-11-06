// Reproduce a PR on a test machine: pick a machine, say what to check if the PR does
// not make it obvious, and an agent writes the script. Nothing runs until the person who
// asked approves the script; the report comes back as a draft comment for them.

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

export function ReproduceButton({ repo, number, align = "right" }: { repo: string; number: number; align?: "left" | "right" }) {
  const t = useT();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const view = useRepoMachines(repo, open);
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
    <Popover
      align={align}
      label={t("Reproduce")}
      trigger={(isOpen, toggle) => (
        <button
          className={isOpen ? "on" : ""}
          aria-expanded={isOpen}
          title={t("Run what the PR claims on a test machine, before and after the change")}
          onClick={() => {
            setOpen(true);
            toggle();
          }}
        >
          <Icon name="server" size={14} />
          {t("Reproduce")}
        </button>
      )}
    >
      {(close) => (
        <div className="stack" style={{ gap: 10, padding: 6, width: 360, maxWidth: "calc(100vw - 48px)" }}>
          <span className="small muted">{t("An agent writes a script that checks what the PR claims. You read and approve it; then it runs on the machine before and after the change, and you get a draft report.")}</span>
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
              <Link to={`/repos/${repo}?tab=settings`}>{t("How this repository uses test machines")}</Link>
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
                        close();
                        setNote("");
                        toast({
                          title: t("Reproduction started"),
                          body: `${t("The agent writes a script; you approve it before it runs on")} ${out.lab}.`,
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
            <Link className="small" to={`/repos/${repo}?tab=settings`}>
              {unused.length} {t("more offered but not used here")} →
            </Link>
          )}
          <ErrorBox error={start.error || view.error || enable.error} />
        </div>
      )}
    </Popover>
  );
}
