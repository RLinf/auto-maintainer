// Reproduce a PR on a test machine: pick a machine, say what to check if the PR does
// not make it obvious, and an agent writes the script. Nothing runs until the person who
// asked approves the script; the report comes back as a draft comment for them.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "./api";
import { AgentPicker, useAgentChoice } from "./agentPicker";
import { ErrorBox, useAct } from "./components";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { openJob } from "./openJob";
import { Popover, Skeleton, useToast } from "./ui";

interface MachineRow {
  name: string;
  description: string;
  transport: string;
}

export function ReproduceButton({ repo, number, align = "right" }: { repo: string; number: number; align?: "left" | "right" }) {
  const t = useT();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const machines = useQuery({
    queryKey: ["labs", repo],
    queryFn: () => api.get<MachineRow[]>(`/api/labs?repo=${encodeURIComponent(repo)}`),
    enabled: open,
  });
  const [lab, setLab] = useState("");
  const [note, setNote] = useState("");
  const [agent, setAgent] = useAgentChoice("reproduce");
  const chosen = lab || machines.data?.[0]?.name || "";
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
          {machines.isLoading ? (
            <Skeleton rows={2} />
          ) : !machines.data?.length ? (
            <div className="small">
              {t("No test machine is set up for this repository yet.")} <Link to="/settings?tab=labs">{t("Set one up")}</Link>
            </div>
          ) : (
            <>
              <div className="stack" style={{ gap: 4 }} role="radiogroup" aria-label={t("Test machine")}>
                {machines.data.map((m) => (
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
          <ErrorBox error={start.error || machines.error} />
        </div>
      )}
    </Popover>
  );
}
