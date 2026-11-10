// Pure view rules for the PR detail page's action bar, agent choice, and contextual CI
// controls. Kept free of JSX and package imports so unit tests run it directly in Node.

export type RunCiState = "gated" | "failed" | "approval" | "running" | "passed" | "none";

const NAMES: Record<string, string> = {
  "claude-code": "Claude Code",
  codex: "Codex",
  antigravity: "Antigravity",
  opencode: "OpenCode",
  gemini: "Gemini CLI",
  pi: "pi",
  dsh: "DeepSeek Harness",
};

export function agentName(name: string | null | undefined): string {
  return name ? NAMES[name] ?? name : "";
}

export interface AgentOptionInput {
  name: string;
  enabled: boolean;
  installed?: boolean | null;
  usable?: boolean | null;
  mine?: string | null;
}

export interface RouteOptionInput {
  task: string;
  primary: string;
  runs_on?: string | null;
}

/** Resolve the usable agents, the default agent for ``task``, and the normalized selection. */
export function resolveAgentChoices<T extends AgentOptionInput>(
  value: string,
  task: string,
  settingsLoaded: boolean,
  allAgents: T[],
  routing: RouteOptionInput[],
): { agents: T[]; primary: string | undefined; current: string; others: T[] } {
  const agents = allAgents.filter((a) => a.enabled && a.installed !== false && (a.usable !== false || !!a.mine));
  const route = routing.find((r) => r.task === task);
  // What the person asks for tries their own agents first (verification keeps its route).
  const own = task === "verify" ? undefined : agents.find((a) => a.mine);
  const primary = own?.name ?? route?.runs_on ?? route?.primary;
  // A remembered agent that was disabled since, or matches the default, falls back to "".
  const valid = !value || agents.some((a) => a.name === value) || !settingsLoaded;
  const current = !valid || (primary && value === primary) ? "" : value;
  const others = agents.filter((a) => a.name !== primary);
  return { agents, primary, current, others };
}

export interface RunCiDisplay {
  /** How the contextual CI control renders in section headers. */
  inline: "button" | "running" | "pill" | "none";
  /** Whether the ``Actions`` menu includes an actionable CI item. */
  actionable: boolean;
  disabled: boolean;
}

export function describeRunCi(state: RunCiState, canPush: boolean): RunCiDisplay {
  if (state === "none") return { inline: "none", actionable: false, disabled: true };
  if (state === "passed") return { inline: "pill", actionable: false, disabled: true };
  if (state === "running") return { inline: "running", actionable: false, disabled: true };
  if (state === "approval") return { inline: "button", actionable: true, disabled: !canPush };
  return { inline: "button", actionable: true, disabled: false };
}

export type PrMenuAction = "fix" | "reproduce" | "fix-ci" | "run-ci" | "merge-base" | "open-forge";
export type PrActionPanel = "menu" | "fix" | "reproduce" | "fix-ci" | "merge-base";

export interface PrMenuState {
  isOpen: boolean;
  hasReviewFindings: boolean;
  failedCount: number;
  ciState: RunCiState;
}

/** Which items appear in the PR page's ``Actions`` menu for the current PR state. */
export function prMenuItems(s: PrMenuState): PrMenuAction[] {
  const items: PrMenuAction[] = [];
  if (s.hasReviewFindings) items.push("fix");
  items.push("reproduce");
  if (s.isOpen && s.failedCount > 0) items.push("fix-ci");
  if (describeRunCi(s.ciState, true).actionable) items.push("run-ci");
  if (s.isOpen) items.push("merge-base");
  items.push("open-forge");
  return items;
}

/** Keep the inline popover sub-panel valid when live updates change the PR under it. */
export function activePrActionPanel(panel: PrActionPanel, s: PrMenuState): PrActionPanel {
  if (panel === "menu") return "menu";
  if (panel === "fix" && !s.hasReviewFindings) return "menu";
  if (panel === "fix-ci" && (!s.isOpen || s.failedCount === 0)) return "menu";
  if (panel === "merge-base" && !s.isOpen) return "menu";
  return panel;
}
