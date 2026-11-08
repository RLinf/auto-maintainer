// Queries shared by several pages, so the header, the home page, the palette and the
// pages read one cache entry each and the live event stream refreshes them all.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { openJob } from "./openJob";
import {
  api,
  type AgentQuestion,
  type Me,
  type MemoryChange,
  type PrRow,
  type ProposalWithAction,
  type QualityStart,
  type RepoRow,
  type ServiceView,
  type SettingsView,
} from "./api";

export function useMe() {
  return useQuery({
    queryKey: ["me"],
    queryFn: () => api.get<Me>("/api/me"),
    // Signed out, or the host is not set up: checking again when the tab regains focus
    // would redraw the sign-in or setup page from scratch, losing a code in progress.
    refetchOnWindowFocus: (query) => query.state.data !== undefined,
  });
}

export function useProposals(enabled = true) {
  return useQuery({
    queryKey: ["proposals", "pending"],
    queryFn: () => api.get<ProposalWithAction[]>("/api/proposals"),
    enabled,
  });
}

export function useQuestions(state: "pending" | "answered" | "expired" | "cancelled" = "pending", enabled = true) {
  return useQuery({
    queryKey: ["questions", state],
    queryFn: () => api.get<AgentQuestion[]>(`/api/questions?state=${state}`),
    enabled,
  });
}

/** Every open PR the user can see; pages filter and sort on the client. */
export function usePrs(enabled = true) {
  return useQuery({ queryKey: ["prs", "all"], queryFn: () => api.get<PrRow[]>("/api/prs"), enabled });
}

export function useRepos(enabled = true) {
  return useQuery({ queryKey: ["repos"], queryFn: () => api.get<RepoRow[]>("/api/repos"), enabled });
}

export function useMemoryChanges(enabled = true) {
  return useQuery({
    queryKey: ["memory", "changes"],
    queryFn: () => api.get<MemoryChange[]>("/api/memory/changes"),
    enabled,
  });
}

export function useService(enabled = true) {
  return useQuery({ queryKey: ["service"], queryFn: () => api.get<ServiceView>("/api/service"), enabled });
}

export function useSettings(enabled = true) {
  return useQuery({ queryKey: ["settings"], queryFn: () => api.get<SettingsView>("/api/settings"), enabled });
}

export const REVIEW_STATES: Record<string, string> = {
  NEW_REVIEW: "Needs review",
  FOLLOWUP_REVIEW: "Needs follow-up",
  APPROVED_NEW_COMMITS: "New commits after approval",
  UP_TO_DATE: "Reviewed",
  SELF_PR: "Maintainer's own",
  DRAFT: "Draft",
  CLOSED: "Closed",
};

export const REVIEW_ORDER = ["NEW_REVIEW", "FOLLOWUP_REVIEW", "APPROVED_NEW_COMMITS", "UP_TO_DATE", "SELF_PR", "DRAFT", "CLOSED"];

export const TIER_ORDER = ["P0", "P1", "P2", "P3"];

export function prKey(p: { repo: string; number: number }) {
  return `${p.repo}#${p.number}`;
}

export interface EvaluationStart {
  repo: string;
  number: number;
  run: "evaluate" | "joined" | "skip";
  event?: string;
  reason?: string;
}

/** Score PRs' priority again, every dimension, with the repository's lessons as they are now. */
export function useReevaluate() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (prs: { repo: string; number: number }[]) => api.post<EvaluationStart[]>("/api/evaluations", { prs }),
    // The scoring jobs exist once the request returns: show their progress at once.
    onSuccess: () => void client.invalidateQueries({ queryKey: ["work"] }),
  });
}

/** Have an agent score each PR's quality: a reviewed PR from its review, any other
 *  (or every one, with ``quick``) quickly from its diff. */
export function useScoreQuality() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ prs, quick = false }: { prs: { repo: string; number: number }[]; quick?: boolean }) =>
      api.post<QualityStart[]>("/api/quality", { prs, quick }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["work"] }),
  });
}

/** What to tell the person after asking for quality scores. */
export function qualityToast(out: QualityStart[], t: (s: string) => string) {
  const first = out[0];
  return {
    title: out.length === 1 ? (first?.run === "joined" ? t("Already being scored") : t("Scoring quality")) : `${t("Scoring quality")}: ${out.length}`,
    body: t("An agent judges each PR by the repository's quality guide; the score appears when it finishes."),
    icon: "sparkles" as const,
    action: first?.event ? { label: t("Watch"), onClick: () => openJob(first.event) } : undefined,
  };
}

/** Read PRs from GitHub now: every repository the user sees, or the ones given. */
export function useSyncPrs() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (repos: string[] = []) => api.post<{ started: string[]; running: string[] }>("/api/prs/refresh", { repos }),
    onSuccess: () => {
      // The polls run in the background; look again once the first ones are likely done.
      window.setTimeout(() => void client.invalidateQueries({ queryKey: ["prs"] }), 8000);
    },
  });
}

/** What to tell the person after asking for PRs to be scored again. */
export function evaluationToast(out: EvaluationStart[], t: (s: string) => string) {
  const started = out.filter((o) => o.run === "evaluate");
  const joined = out.filter((o) => o.run === "joined");
  const first = started[0] ?? joined[0];
  if (!first) return { title: t("Priority is off for these repositories"), icon: "check" as const };
  return {
    title: out.length === 1 ? (started.length ? t("Scoring again") : t("Already being scored again")) : `${t("Scoring again")}: ${started.length + joined.length}`,
    body: t("The new priority appears as each agent finishes."),
    icon: "sparkles" as const,
    action: first.event ? { label: t("Watch"), onClick: () => openJob(first.event as string) } : undefined,
  };
}
