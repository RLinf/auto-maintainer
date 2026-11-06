// Live updates: the /api/events server-sent event stream invalidates cached queries,
// so pages refresh when something changes instead of polling.

import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

const INVALIDATES: Record<string, string[][]> = {
  "proposal.created": [["proposals"], ["pr"], ["prs"], ["repos"]],
  "proposal.decided": [["proposals"], ["pr"], ["repos"]],
  "action.executed": [["pr"], ["prs"], ["proposals"], ["audit"], ["service"], ["settings"], ["memory"]],
  "action.failed": [["pr"], ["proposals"], ["audit"], ["service"]],
  "memory.changed": [["memory"], ["profile"], ["repos"], ["settings"]],
  "pr.opened": [["prs"], ["repos"]],
  "pr.synchronized": [["prs"], ["pr"]],
  "pr.updated": [["prs"], ["pr"]],
  "pr.labeled": [["prs"], ["pr"]],
  "pr.closed": [["prs"], ["repos"]],
  "pr.review_submitted": [["pr"]],
  "checks.completed": [["pr"], ["ci"]],
  "merge.armed": [["pr"]],
  "merge.disarmed": [["pr"]],
  "priority.overridden": [["prs"], ["pr"]],
  "question.asked": [["questions"], ["me"]],
  "question.answered": [["questions"], ["me"], ["audit"], ["job"], ["work"]],
  "job.progress": [["job"], ["work"]],
};

/** Keys refreshed after a burst of events, at most once per ``SLOW_MS``: lists that are
 * costly to reload and change with a job's status rather than with each step. */
const SLOW: Record<string, string[][]> = {
  "job.progress": [["prs"]],
};
const SLOW_MS = 3000;

/** Called for every ``question.asked`` event, to alert reviewers. */
export const QUESTION_EVENT = "am-question";

/** Keeps the dashboard's queries current; alerts ``login`` to questions meant for them
 * (or for any reviewer). */
export function useLiveEvents(enabled: boolean, login?: string) {
  const client = useQueryClient();
  useEffect(() => {
    if (!enabled || typeof EventSource === "undefined") return;
    const source = new EventSource("/api/events", { withCredentials: true });
    const timers = new Map<string, number>();
    const handler = (e: MessageEvent) => {
      const keys = INVALIDATES[e.type];
      if (keys) keys.forEach((queryKey) => client.invalidateQueries({ queryKey }));
      const slow = SLOW[e.type];
      if (slow && !timers.has(e.type)) {
        timers.set(
          e.type,
          window.setTimeout(() => {
            timers.delete(e.type);
            slow.forEach((queryKey) => client.invalidateQueries({ queryKey }));
          }, SLOW_MS),
        );
      }
      if (e.type === "question.asked") alertReviewer(e.data, login);
    };
    Object.keys(INVALIDATES).forEach((kind) => source.addEventListener(kind, handler as EventListener));
    return () => {
      source.close();
      timers.forEach((id) => window.clearTimeout(id));
    };
  }, [enabled, client, login]);
}

function alertReviewer(raw: string, login?: string) {
  let title = "An agent is waiting for an answer";
  try {
    const event = JSON.parse(raw) as { title?: string | null; repo?: string | null; asked_for?: string | null };
    // A question for the person who asked for the job alerts only them.
    if (event.asked_for && login && event.asked_for.toLowerCase() !== login.toLowerCase()) return;
    if (event.title) title = `${event.repo ?? "auto-maintainer"}: ${event.title}`;
  } catch {
    // keep the generic title
  }
  window.dispatchEvent(new CustomEvent(QUESTION_EVENT, { detail: title }));
  try {
    if (typeof Notification !== "undefined" && Notification.permission === "granted") {
      new Notification("auto-maintainer: agent question", { body: title });
    }
  } catch {
    // browser notifications are optional
  }
}
