// Which agent does a review: the route's default, or one the reviewer picks. Only
// enabled agents are offered.

import type { ReviewStart } from "./api";
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

export function AgentPicker({ value, onChange, compact }: { value: string; onChange: (v: string) => void; compact?: boolean }) {
  const t = useT();
  const settings = useSettings();
  const agents = (settings.data?.agents ?? []).filter((a) => a.enabled);
  return (
    <label className="row" style={{ gap: 6 }} title={t("Which agent reviews")}>
      {!compact && <Icon name="cpu" size={14} />}
      <select value={value} onChange={(e) => onChange(e.target.value)} aria-label={t("Which agent reviews")} style={{ height: compact ? 28 : 34 }}>
        <option value="">{t("Default agent")}</option>
        {agents.map((a) => (
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
    action: started || taken.length ? { label: t("Proposals"), onClick: openProposals } : undefined,
  };
}
