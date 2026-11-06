// Queries shared by several pages, so the header, the home page, the palette and the
// pages read one cache entry each and the live event stream refreshes them all.

import { useQuery } from "@tanstack/react-query";
import {
  api,
  type AgentQuestion,
  type Me,
  type MemoryChange,
  type PrRow,
  type ProposalWithAction,
  type RepoRow,
  type ServiceView,
  type SettingsView,
} from "./api";

export function useMe() {
  return useQuery({ queryKey: ["me"], queryFn: () => api.get<Me>("/api/me") });
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
