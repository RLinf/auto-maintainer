// Pure view rules for the PR detail page's action bar, agent choice, and contextual CI
// controls. Kept free of JSX and package imports so unit tests run it directly in Node.

export type RunCiState = "gated" | "failed" | "approval" | "running" | "passed" | "none";

export interface CheckLink {
  name: string;
  workflow?: string | null;
  details_url?: string | null;
  status?: string | null;
  conclusion?: string | null;
}

export interface DocsLink {
  url: string;
  /** ``preview`` is the built docs site; ``build`` is a build status/log page. */
  kind: "preview" | "build";
  state: "ready" | "running" | "failed" | "other";
  name: string;
}

/** A docs build's check: Read the Docs' PR preview and the like, by name or by the
 * host its details link points at. */
const DOCS_CHECK = /readthedocs|build[-_ ]?preview/i;
const DOCS_HOST = /(^|\.)readthedocs\.(io|org|build)$/i;
const RTD_BUILD_PAGE = /^https?:\/\/(?:app\.)?readthedocs\.(?:org|io)\/projects\/[^/]+\/builds\//i;
const CI_RUN_PAGE = /^https?:\/\/[^/]+\/[^/]+\/[^/]+\/(?:actions\/runs\/|-\/(?:jobs|pipelines)\/)/i;
const ZH_DOC = /(?:^|\/|[-_:])(?:zh(?:[-_]cn)?|cn)(?:$|\/|[-_:])/i;
const FAILED_CONCLUSIONS = new Set(["failure", "timed_out", "cancelled", "startup_failure"]);

export function isDocsCheck(check: CheckLink): boolean {
  return DOCS_CHECK.test(`${check.name} ${check.workflow ?? ""}`);
}

/** Whether ``url`` points to a docs build log / CI run page rather than the rendered
 * docs site itself (for example Read the Docs while building or after a failure). */
export function isDocsBuildPage(url: string | null | undefined): boolean {
  if (!url) return false;
  return RTD_BUILD_PAGE.test(url) || CI_RUN_PAGE.test(url);
}

function hostOf(url: string | null | undefined): string {
  if (!url) return "";
  try {
    return new URL(url).hostname;
  } catch {
    return "";
  }
}

function pickForLang(items: CheckLink[], lang?: string): CheckLink {
  if (items.length <= 1 || !lang) return items[0];
  const isZh = (c: CheckLink) => ZH_DOC.test(`${c.name} ${c.details_url ?? ""}`);
  if (lang === "zh") return items.find(isZh) ?? items[0];
  return items.find((c) => !isZh(c)) ?? items[0];
}

/** The best docs link among a PR's checks: prefers a rendered docs preview URL over a
 * build status/log page (such as an in-progress or failed Read the Docs build). */
export function docsCheckLink(checks: CheckLink[], lang?: string): DocsLink | null {
  const matched = checks.filter(
    (c) => !!c.details_url && (isDocsCheck(c) || DOCS_HOST.test(hostOf(c.details_url))),
  );
  if (!matched.length) return null;
  const previews = matched.filter(
    (c) =>
      !isDocsBuildPage(c.details_url) &&
      !FAILED_CONCLUSIONS.has((c.conclusion ?? "").toLowerCase()),
  );
  if (previews.length) {
    const pick = pickForLang(previews, lang);
    return { url: pick.details_url!, kind: "preview", state: "ready", name: pick.name };
  }
  const running = matched.filter((c) => c.status === "in_progress" || c.status === "queued");
  const pick = pickForLang(running.length ? running : matched, lang);
  const concl = (pick.conclusion ?? "").toLowerCase();
  const state =
    pick.status === "in_progress" || pick.status === "queued"
      ? "running"
      : FAILED_CONCLUSIONS.has(concl)
        ? "failed"
        : "other";
  return { url: pick.details_url!, kind: "build", state, name: pick.name };
}

/** The docs link's URL among a PR's checks, preferring the built docs preview. */
export function docsCheckUrl(checks: CheckLink[], lang?: string): string | null {
  return docsCheckLink(checks, lang)?.url ?? null;
}

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

export interface PersonalDefaultInput {
  agent: string;
  own: boolean;
}

/** Resolve the usable agents, the default agent for ``task``, and the normalized selection. */
export function resolveAgentChoices<T extends AgentOptionInput>(
  value: string,
  task: string,
  settingsLoaded: boolean,
  allAgents: T[],
  routing: RouteOptionInput[],
  myDefault?: PersonalDefaultInput | null,
): { agents: T[]; primary: string | undefined; primaryOnService: boolean; current: string; others: T[] } {
  const agents = allAgents.filter((a) => a.enabled && a.installed !== false && (a.usable !== false || !!a.mine));
  const route = routing.find((r) => r.task === task);
  const routePrimary = route?.runs_on ?? route?.primary;
  // Matches AgentRouter.firsts / Service.runs_on_for: verification keeps its route;
  // otherwise the person's default (when ready, and usable if on the service's account
  // or theirs if on their own account), else their own agents, else the route.
  let primary = routePrimary;
  let primaryOnService = false;
  if (task !== "verify") {
    const chosen = myDefault
      ? allAgents.find((a) => a.name === myDefault.agent && a.enabled && a.installed !== false)
      : undefined;
    if (chosen && !myDefault!.own) {
      if (chosen.usable !== false) {
        primary = chosen.name;
        primaryOnService = true;
      }
    } else if (chosen && myDefault!.own && chosen.mine) {
      primary = chosen.name;
    } else {
      const own = agents.find((a) => a.mine);
      primary = own?.name ?? routePrimary;
    }
  }
  // A remembered agent that was disabled since, or matches the default, falls back to "".
  const valid = !value || agents.some((a) => a.name === value) || !settingsLoaded;
  const current = !valid || (primary && value === primary) ? "" : value;
  const others = agents.filter((a) => a.name !== primary);
  return { agents, primary, primaryOnService, current, others };
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

export type PrMenuAction = "fix" | "change" | "reproduce" | "fix-ci" | "run-ci" | "merge-base";
export type PrActionPanel = "menu" | "fix" | "change" | "reproduce" | "fix-ci" | "merge-base";

export interface PrMenuState {
  isOpen: boolean;
  hasReviewFindings: boolean;
  failedCount: number;
  ciState: RunCiState;
}

/** Which items appear in the PR page's ``Actions`` menu for the current PR state.
 * Opening the PR on its forge is a button of its own beside the menu, since people
 * use it often. */
export function prMenuItems(s: PrMenuState): PrMenuAction[] {
  const items: PrMenuAction[] = [];
  // A review's findings to fix, and always a change in the person's own words.
  if (s.hasReviewFindings) items.push("fix");
  if (s.isOpen) items.push("change");
  items.push("reproduce");
  if (s.isOpen && s.failedCount > 0) items.push("fix-ci");
  if (describeRunCi(s.ciState, true).actionable) items.push("run-ci");
  if (s.isOpen) items.push("merge-base");
  return items;
}

/** Keep the inline popover sub-panel valid when live updates change the PR under it. */
export function activePrActionPanel(panel: PrActionPanel, s: PrMenuState): PrActionPanel {
  if (panel === "menu") return "menu";
  if (panel === "fix" && !s.hasReviewFindings) return "menu";
  if (panel === "change" && !s.isOpen) return "menu";
  if (panel === "fix-ci" && (!s.isOpen || s.failedCount === 0)) return "menu";
  if (panel === "merge-base" && !s.isOpen) return "menu";
  return panel;
}

/** How many findings "Fix with agent" works on from a review's ``result``: a full
 *  review's findings, or a follow-up's new findings and the earlier ones it does not
 *  mark resolved (``playbooks/fix.py:followup_findings``). */
export function fixableFindings(result: unknown): number {
  const r = (result ?? {}) as { findings?: unknown[]; new_findings?: unknown[]; statuses?: { status?: string }[] };
  return (r.findings?.length ?? 0) + (r.new_findings?.length ?? 0) + (r.statuses ?? []).filter((s) => s.status !== "resolved").length;
}
