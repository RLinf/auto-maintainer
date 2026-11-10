// Which agent does a review: the one its route runs on (the first installed agent in
// its order), or one the reviewer picks among the agents installed on the host.

import { useState } from "react";
import type { ReviewStart } from "./api";
import { openJob } from "./openJob";
import { useSettings } from "./data";
import { useT } from "./i18n";
import { Icon } from "./icons";

const NAMES: Record<string, string> = {
  "claude-code": "Claude Code",
  codex: "Codex",
  antigravity: "Antigravity",
  opencode: "OpenCode",
  gemini: "Gemini CLI",
};

export function agentName(name: string | null | undefined): string {
  return name ? NAMES[name] ?? name : "";
}

/** The agent last picked for ``task``, remembered in this browser. */
export function useAgentChoice(task: string): [string, (v: string) => void] {
  const key = `am.agent.${task}`;
  const [value, setValue] = useState(() => {
    try {
      return localStorage.getItem(key) ?? "";
    } catch {
      return "";
    }
  });
  const set = (v: string) => {
    setValue(v);
    try {
      localStorage.setItem(key, v);
    } catch {
      // the choice then lasts for this page only
    }
  };
  return [value, set];
}

export function AgentPicker({
  value,
  onChange,
  compact,
  task = "review",
}: {
  value: string;
  onChange: (v: string) => void;
  compact?: boolean;
  task?: string;
}) {
  const t = useT();
  const settings = useSettings();
  // Only agents that can run here: turned on, their CLI installed on the host, and
  // signed in or given a model provider.
  const agents = (settings.data?.agents ?? []).filter((a) => a.enabled && a.installed !== false && a.usable !== false);
  const route = settings.data?.routing.find((r) => r.task === task);
  const primary = route?.runs_on ?? route?.primary;
  // A remembered agent that was disabled since falls back to the default.
  const current = value && !agents.some((a) => a.name === value) && settings.data ? "" : value;
  return (
    <label className="row" style={{ gap: 6 }} title={t("Which agent does it")}>
      {!compact && <Icon name="cpu" size={14} />}
      <select value={current} onChange={(e) => onChange(e.target.value)} aria-label={t("Which agent does it")} style={{ height: compact ? 28 : 34 }}>
        <option value="">{primary ? `${agentName(primary)} (${t("default")})` : t("Default agent")}</option>
        {agents
          .filter((a) => a.name !== primary)
          .map((a) => (
            <option key={a.name} value={a.name}>
              {agentName(a.name)}
            </option>
          ))}
      </select>
    </label>
  );
}

/** The toast after asking for reviews: what started, and what someone else already has. */
export function reviewToast(out: ReviewStart[], t: (s: string) => string, openProposals: () => void) {
  const started = out.filter((o) => o.run === "review" || o.run === "followup").length;
  const taken = out.filter((o) => o.run === "joined" || o.run === "drafted");
  const skipped = out.filter((o) => o.run === "skip");
  const firstStarted = out.find((o) => (o.run === "review" || o.run === "followup") && o.event);
  return {
    title: started ? `${started} ${t(started === 1 ? "review started" : "reviews started")}` : t("Nothing new started"),
    body: [
      started ? t("Drafts come back to you in Proposals; the agent may ask you questions meanwhile.") : "",
      ...taken.map(
        (o) =>
          `#${o.number}: ${o.run === "joined" ? t("already being reviewed by") : t("already drafted for")} ${o.by ?? "—"} (${agentName(o.agent) || t("Default agent")})`,
      ),
      ...skipped.map((o) => `#${o.number}: ${o.reason ?? ""}`),
    ]
      .filter(Boolean)
      .join(" · "),
    icon: "pr" as const,
    action: firstStarted?.event
      ? { label: t(started === 1 ? "Watch" : "Watch the first"), onClick: () => openJob(firstStarted.event as string) }
      : taken.length
        ? { label: t("Approvals"), onClick: openProposals }
        : undefined,
  };
}
