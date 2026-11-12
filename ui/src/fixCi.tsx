// Fix a PR's failing CI: an agent finds why each failed job fails, fixes the PR's code,
// and, on a test machine the person picks, copies its change there and runs the failing
// steps until they pass. The press approves the machine's session; the fix comes back
// as a draft the person pushes to the PR as themselves.

import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "./api";
import { AgentPicker, useAgentChoice } from "./agentPicker";
import { ErrorBox, useAct } from "./components";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { useRepoMachines } from "./machines";
import { openJob } from "./openJob";
import { Popover, Skeleton, useToast } from "./ui";

const FAILED = new Set(["failure", "timed_out", "startup_failure"]);

export function failedChecks(checks: { name: string; conclusion: string | null }[]): string[] {
  return checks.filter((c) => c.conclusion && FAILED.has(c.conclusion)).map((c) => c.name);
}

export function FixCiForm({
  repo,
  number,
  failed,
  canPush,
  onClose,
}: {
  repo: string;
  number: number;
  failed: string[];
  canPush: boolean;
  onClose: () => void;
}) {
  const t = useT();
  const toast = useToast();
  const view = useRepoMachines(repo, true);
  const used = (view.data?.machines ?? []).filter((m) => m.used);
  const [lab, setLab] = useState<string | null>(null);
  const [gpu, setGpu] = useState(false);
  const [note, setNote] = useState("");
  const [agent, setAgent] = useAgentChoice("fix");
  // A machine by default when the repository uses one; "" is the choice of none.
  const chosen = lab === null ? (used[0]?.name ?? "") : lab;
  const start = useAct(
    () =>
      api.post<{ event: string; session: { status: string; reason: string | null } | null }>(`/api/prs/${repo}/${number}/fix-ci`, {
        agent: agent || null,
        lab: chosen || null,
        gpu,
        note,
      }),
    [["work"], ["pr"], ["proposals"]],
  );
  return (
    <div className="stack" style={{ gap: 8, padding: 6, width: "min(360px, calc(100vw - 24px))", maxWidth: "100%" }}>
      <span className="small muted">
        {t("An agent reads why these jobs fail and fixes the PR's code:")} <strong>{failed.join(", ")}</strong>.{" "}
        {t("On a test machine it copies its change there and runs the failing steps until they pass. You get the fix as a draft and push it as yourself.")}
      </span>
      {view.isLoading ? (
        <Skeleton rows={2} />
      ) : (
        <div className="stack" style={{ gap: 4 }} role="radiogroup" aria-label={t("Test machine")}>
          {used.map((m) => (
            <label key={m.name} className={`machine-choice ${chosen === m.name ? "on" : ""}`}>
              <input type="radio" name="fix-machine" checked={chosen === m.name} onChange={() => setLab(m.name)} />
              <span className="stack" style={{ gap: 0 }}>
                <strong className="small">
                  {m.name}
                  {m.mine && <span className="muted"> · {t("your own")}</span>}
                </strong>
                {m.description && <span className="small muted clamp2">{m.description}</span>}
              </span>
            </label>
          ))}
          <label className={`machine-choice ${chosen === "" ? "on" : ""}`}>
            <input type="radio" name="fix-machine" checked={chosen === ""} onChange={() => setLab("")} />
            <span className="stack" style={{ gap: 0 }}>
              <strong className="small">{t("No test machine")}</strong>
              <span className="small muted">{t("The agent fixes the code without running it.")}</span>
            </span>
          </label>
          {!used.length && (
            <Link className="small" to={`/repos/${repo}?tab=labs`}>
              {t("This repository uses no test machine yet.")} →
            </Link>
          )}
        </div>
      )}
      {chosen && (
        <label className="small row" style={{ gap: 6 }}>
          <input type="checkbox" checked={gpu} onChange={(e) => setGpu(e.target.checked)} />
          {t("The tests need a GPU")}
        </label>
      )}
      <textarea
        className="prose"
        rows={2}
        style={{ minHeight: 0, padding: "6px 10px" }}
        placeholder={t("Anything the agent should know? (optional)")}
        value={note}
        onChange={(e) => setNote(e.target.value)}
      />
      <div className="row">
        <AgentPicker value={agent} onChange={setAgent} task="fix" />
        <button
          className="primary right"
          style={{ flexShrink: 0 }}
          disabled={start.isPending}
          onClick={() =>
            start.mutate(undefined, {
              onSuccess: (out) => {
                onClose();
                setNote("");
                const refused = out.session && !["queued", "intent", "executed"].includes(out.session.status);
                toast({
                  title: t("Fixing the CI"),
                  body: refused
                    ? `${t("The test machine was not approved, so the agent fixes without running anything:")} ${out.session?.reason || out.session?.status}`
                    : chosen
                      ? `${t("The agent tests its fix on")} ${chosen}. ${t("The fix comes back to you as a draft.")}`
                      : t("The fix comes back to you as a draft."),
                  icon: "activity",
                  action: { label: t("Watch"), onClick: () => openJob(out.event) },
                });
              },
            })
          }
        >
          {start.isPending ? t("Starting…") : t("Start fixing")}
        </button>
      </div>
      {!canPush && <span className="small muted">{t("Sign in with GitHub to push the fix as yourself when it is ready.")}</span>}
      <ErrorBox error={start.error || view.error} />
    </div>
  );
}

export function FixCiButton({
  repo,
  number,
  failed,
  canPush,
  size,
}: {
  repo: string;
  number: number;
  failed: string[];
  canPush: boolean;
  size?: "sm";
}) {
  const t = useT();
  if (!failed.length) return null;
  return (
    <Popover
      align="right"
      label={t("Fix CI")}
      trigger={(isOpen, toggle) => (
        <button
          className={[size, isOpen ? "on" : ""].filter(Boolean).join(" ")}
          aria-expanded={isOpen}
          title={`${t("Have an agent fix the failing CI jobs:")} ${failed.join(", ")}`}
          onClick={toggle}
        >
          <Icon name="activity" size={14} />
          {t("Fix CI")}
        </button>
      )}
    >
      {(close) => <FixCiForm repo={repo} number={number} failed={failed} canPush={canPush} onClose={close} />}
    </Popover>
  );
}
