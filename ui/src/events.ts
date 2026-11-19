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
  "action.executed": [["pr"], ["prs"], ["proposals"], ["audit"], ["service"], ["settings"], ["memory"], ["lessons"]],
  "action.failed": [["pr"], ["proposals"], ["audit"], ["service"]],
  "memory.changed": [["memory"], ["profile"], ["repos"], ["settings"], ["my-agents"], ["lessons"]],
  "pr.opened": [["prs"], ["repos"], ["merge-schedule"]],
  "pr.synchronized": [["prs"], ["pr"], ["merge-schedule"]],
  "pr.updated": [["prs"], ["pr"], ["merge-schedule"]],
  "pr.labeled": [["prs"], ["pr"], ["merge-schedule"]],
  "pr.closed": [["prs"], ["repos"], ["merge-schedule"]],
  "pr.review_submitted": [["pr"], ["prs"], ["merge-schedule"]],
  "pr.comment": [["pr"], ["prs"]],
  "pr.refreshed": [["pr"], ["prs"], ["merge-schedule"]],
  "checks.completed": [["pr"], ["ci"], ["merge-schedule"]],
  "merge.armed": [["pr"], ["merge-schedule"]],
  "merge.disarmed": [["pr"], ["merge-schedule"]],
  "merge.queue": [["merge-schedule"]],
  "priority.overridden": [["prs"], ["pr"]],
  "repo.added": [["repos"]],
  "code.changed": [["service"]],
  "service.changed": [["agents"], ["agents-usage"], ["service-sign-ins"], ["my-agents"], ["settings"]],
  "question.asked": [["questions"], ["me"]],
  "question.answered": [["questions"], ["me"], ["audit"], ["job"], ["work"]],
  // An agent's steps: only the views of that one job follow at once (``refreshJob``);
  // lists follow at most every few seconds (``SLOW``), since agents report a step a
  // second each.
  "job.progress": [],
  // A job's end: what it changed follows through ``SLOW``.
  "job.finished": [],
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
  "job.progress": [["work"]],
  // A finished job may have changed a PR's priority, review state, or score.
  "job.finished": [["work"], ["prs"], ["pr"]],
};
const SLOW_MS = 3000;

/** Queries that a general refresh leaves alone: each read starts every agent CLI or
 * reads their limits (``agents``, ``agents-usage``), or does not change after an
 * action (``forges``, ``auth-config``, ``diff``); their own events refresh them. */
const COSTLY = new Set(["agents", "agents-usage", "forges", "auth-config", "diff"]);

/** Reload the views of job ``job`` alone: those keyed by its id, and one keyed by the
 * request that started it once that request resolved to it. Every other view stays:
 * one agent's progress reloading every open job view in every tab kept the
 * service's one core busy. */
function refreshJob(client: QueryClient, job: string | undefined) {
  if (!job) return;
  void client.invalidateQueries({
    predicate: (q) => {
      const key = q.queryKey as unknown[];
      if (key[0] === "job") return key[1] === job || (q.state.data as { id?: string } | undefined)?.id === job;
      if (key[0] === "chat" && key[1] === "steps") return key.includes(job);
      return false;
    },
  });
}

function jobOf(raw: string): string | undefined {
  try {
    return (JSON.parse(raw) as { job?: string }).job;
  } catch {
    return undefined;
  }
}

/** Reload every query on screen (and mark the rest stale), but the costly ones; with
 * ``only``, a PR page's queries only for the PRs it names (``repo#number``): what an
 * event about one PR changes shows on that PR's page, not on every PR's. */
export function refreshShown(client: QueryClient, only?: Set<string> | null) {
  void client.invalidateQueries({
    predicate: (q) => {
      const key = q.queryKey as unknown[];
      if (COSTLY.has(String(key[0]))) return false;
      if (only && key[0] === "pr" && key.length >= 3) return only.has(`${key[1]}#${key[2]}`);
      return true;
    },
  });
}

/** The PR an event names, as ``repo#number``; ``null`` for one about no single PR. */
function prOf(raw: string): string | null {
  try {
    const e = JSON.parse(raw) as { repo?: string | null; number?: number | null };
    return e.repo && e.number != null ? `${e.repo}#${e.number}` : null;
  } catch {
    return null;
  }
}

/** Reload the page of the one PR ``pr`` (``repo#number``) names. */
function refreshPr(client: QueryClient, pr: string) {
  void client.invalidateQueries({
    predicate: (q) => {
      const key = q.queryKey as unknown[];
      return key[0] === "pr" && key.length >= 3 && `${key[1]}#${key[2]}` === pr;
    },
  });
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
    // A broad refresh runs at once, and once more at the end of a burst. ``scope``
    // holds the PRs the burst's events named; ``null`` once one named none.
    let broadTimer: number | null = null;
    let broadAgain = false;
    let scope: Set<string> | null = new Set();
    const broad = () => {
      if (broadTimer !== null) {
        broadAgain = true;
        return;
      }
      const only = scope;
      scope = new Set();
      refreshShown(client, only);
      broadTimer = window.setTimeout(() => {
        broadTimer = null;
        if (broadAgain) {
          broadAgain = false;
          broad();
        }
      }, SLOW_MS);
    };
    // A hidden tab reloads nothing as events come; it catches up once when shown.
    let missed = false;
    const shown = () => {
      if (!document.hidden && missed) {
        missed = false;
        scope = null;
        broad();
      }
    };
    document.addEventListener("visibilitychange", shown);
    const handler = (e: MessageEvent) => {
      heard = Date.now();
      if (e.type === "question.asked") alertReviewer(e.data, login);
      if (e.type === "action.executed" || e.type === "action.failed") reportAction(e.data);
      if (document.hidden) {
        missed = true;
        return;
      }
      if (e.type === "job.progress") refreshJob(client, jobOf(e.data));
      const pr = prOf(e.data);
      const keys = INVALIDATES[e.type];
      // A PR's page follows the events about it alone.
      if (keys) keys.forEach((queryKey) => (queryKey.length === 1 && queryKey[0] === "pr" && pr ? refreshPr(client, pr) : client.invalidateQueries({ queryKey })));
      if (BROAD.has(e.type)) {
        if (pr === null) scope = null;
        else scope?.add(pr);
        broad();
      }
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
    };
    const connect = () => {
      source?.close();
      heard = Date.now();
      const s = new EventSource("/api/events", { withCredentials: true });
      s.addEventListener("open", () => {
        heard = Date.now();
        // Whatever happened while the page was not listening.
        if (connected) {
          scope = null;
          broad();
        }
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
      document.removeEventListener("visibilitychange", shown);
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
