// Live updates: the /api/events server-sent event stream invalidates cached queries,
// so pages refresh when something changes instead of polling. The stream sends a
// ping every 15 seconds; one quiet for longer is dead (a network that dropped it
// without saying), so the page connects again, and after every reconnect it reloads
// what it shows, for the events it missed meanwhile.

import { useEffect } from "react";
import { type QueryClient, useQueryClient } from "@tanstack/react-query";

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
  "pr.review_submitted": [["pr"], ["prs"]],
  "pr.comment": [["pr"], ["prs"]],
  "pr.refreshed": [["pr"], ["prs"]],
  "checks.completed": [["pr"], ["ci"]],
  "merge.armed": [["pr"]],
  "merge.disarmed": [["pr"]],
  "priority.overridden": [["prs"], ["pr"]],
  "repo.added": [["repos"]],
  "code.changed": [["service"]],
  "service.changed": [["agents"], ["agents-usage"], ["service-sign-ins"], ["my-agents"]],
  "question.asked": [["questions"], ["me"]],
  "question.answered": [["questions"], ["me"], ["audit"], ["job"], ["work"]],
  "job.progress": [["job"], ["work"], ["chat", "steps"]],
};

/** Events after which everything on screen loads again (at most once per
 * ``SLOW_MS``): what an approval, a write, or a settings change touches shows in more
 * places than any list here can name (a review's job, a chat's request card, the
 * People record). */
const BROAD = new Set([
  "proposal.created",
  "proposal.decided",
  "action.executed",
  "action.failed",
  "memory.changed",
  "service.changed",
  "question.answered",
  "repo.added",
  "code.changed",
]);

/** Keys refreshed after a burst of events, at most once per ``SLOW_MS``: lists that are
 * costly to reload and change with a job's status rather than with each step. */
const SLOW: Record<string, string[][]> = {
  // A finished job may have changed a PR's priority or review state.
  "job.progress": [["prs"], ["pr"]],
};
const SLOW_MS = 3000;

/** Queries that a general refresh leaves alone: each read starts every agent CLI or
 * reads their limits (``agents``, ``agents-usage``), or does not change after an
 * action (``forges``, ``auth-config``, ``diff``); their own events refresh them. */
const COSTLY = new Set(["agents", "agents-usage", "forges", "auth-config", "diff"]);

/** Reload every query on screen (and mark the rest stale), but the costly ones. */
export function refreshShown(client: QueryClient) {
  void client.invalidateQueries({ predicate: (q) => !COSTLY.has(String(q.queryKey[0])) });
}

const PING_MS = 15_000;
const DEAD_MS = 3 * PING_MS;

/** Dispatched with an ``ActionOutcome`` when an action this page approved ran. */
export const ACTION_EVENT = "am-action";

export interface ActionOutcome {
  label: string;
  ok: boolean;
  status: string;
  error: string | null;
}

/** Actions approved from this page, waiting to run, with how to name them. */
const watched = new Map<string, { label: string; at: number }>();

/** Say how the action ``id`` went once it runs: an approval returns before its
 * action does (a push, a post), and a failure would otherwise show nowhere the
 * approver looks. Called before the approval is sent, so a quick run is not missed. */
export function watchAction(id: string, label: string) {
  const now = Date.now();
  for (const [key, w] of watched) if (now - w.at > 30 * 60_000) watched.delete(key);
  watched.set(id, { label, at: now });
}

export function unwatchAction(id: string) {
  watched.delete(id);
}

function reportAction(raw: string) {
  try {
    const action = (JSON.parse(raw) as { action?: { action_id?: string; status?: string; error?: string | null } }).action;
    const w = action?.action_id ? watched.get(action.action_id) : undefined;
    if (!action?.action_id || !w) return;
    watched.delete(action.action_id);
    const status = action.status ?? "";
    const detail: ActionOutcome = { label: w.label, ok: status === "executed" || status === "noop", status, error: action.error ?? null };
    window.dispatchEvent(new CustomEvent(ACTION_EVENT, { detail }));
  } catch {
    // an outcome the page cannot read stays in the PR's timeline
  }
}

/** Called for every ``question.asked`` event, to alert reviewers. */
export const QUESTION_EVENT = "am-question";

/** Keeps the dashboard's queries current; alerts ``login`` to questions meant for them
 * (or for any reviewer). */
export function useLiveEvents(enabled: boolean, login?: string) {
  const client = useQueryClient();
  useEffect(() => {
    if (!enabled || typeof EventSource === "undefined") return;
    const timers = new Map<string, number>();
    let source: EventSource | null = null;
    let heard = Date.now();
    let connected = false;
    // A broad refresh runs at once, and once more at the end of a burst.
    let broadTimer: number | null = null;
    let broadAgain = false;
    const broad = () => {
      if (broadTimer !== null) {
        broadAgain = true;
        return;
      }
      refreshShown(client);
      broadTimer = window.setTimeout(() => {
        broadTimer = null;
        if (broadAgain) {
          broadAgain = false;
          broad();
        }
      }, SLOW_MS);
    };
    const handler = (e: MessageEvent) => {
      heard = Date.now();
      const keys = INVALIDATES[e.type];
      if (keys) keys.forEach((queryKey) => client.invalidateQueries({ queryKey }));
      if (BROAD.has(e.type)) broad();
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
      if (e.type === "action.executed" || e.type === "action.failed") reportAction(e.data);
    };
    const connect = () => {
      source?.close();
      heard = Date.now();
      const s = new EventSource("/api/events", { withCredentials: true });
      s.addEventListener("open", () => {
        heard = Date.now();
        // Whatever happened while the page was not listening.
        if (connected) broad();
        connected = true;
      });
      s.addEventListener("ping", () => {
        heard = Date.now();
      });
      Object.keys(INVALIDATES).forEach((kind) => s.addEventListener(kind, handler as EventListener));
      source = s;
    };
    connect();
    const watchdog = window.setInterval(() => {
      if (Date.now() - heard > DEAD_MS) connect();
    }, PING_MS);
    return () => {
      source?.close();
      window.clearInterval(watchdog);
      if (broadTimer !== null) window.clearTimeout(broadTimer);
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
