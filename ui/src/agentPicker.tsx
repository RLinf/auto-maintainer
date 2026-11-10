// Which agent does a review: by default the reviewer's own agent (one they run on their
// own model API or sign-in), else the one its route runs on (the first usable agent in
// its order), or one the reviewer picks among the agents usable here.

import { useState } from "react";
import type { ReviewStart } from "./api";
import { openJob } from "./openJob";
import { useSettings } from "./data";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { agentName, resolveAgentChoices } from "./prActions";

export { agentName };

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

/** Usable agents, the default agent for ``task``, and display labels for an agent picker. */
export function useAgentOptions(value: string, task = "review") {
  const t = useT();
  const settings = useSettings();
  const { agents, primary, current, others } = resolveAgentChoices(
    value,
    task,
    !!settings.data,
    settings.data?.agents ?? [],
    settings.data?.routing ?? [],
  );
  const label = (name: string) => {
    const mine = agents.find((a) => a.name === name)?.mine;
    return mine ? `${agentName(name)} · ${mine === "api" ? t("my API") : t("my own sign-in")}` : agentName(name);
  };
  return { agents, primary, current, others, label };
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
  const { primary, current, others, label } = useAgentOptions(value, task);
  return (
    <label className="row" style={{ gap: 6, flexWrap: "nowrap", minWidth: 0 }} title={t("Which agent does it")}>
      {!compact && (
        <span style={{ display: "inline-flex", flexShrink: 0 }}>
          <Icon name="cpu" size={14} />
        </span>
      )}
      <select
        value={current}
        onChange={(e) => onChange(e.target.value)}
        aria-label={t("Which agent does it")}
        style={{ height: compact ? 28 : 34, minWidth: 0 }}
      >
        <option value="">{primary ? `${label(primary)} (${t("default")})` : t("Default agent")}</option>
        {others.map((a) => (
          <option key={a.name} value={a.name}>
            {label(a.name)}
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
