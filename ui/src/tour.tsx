// The walkthroughs (tours.ts) on the live pages: a card that goes step by step, opening
// each step's page and spotlighting the control it explains; where the page has none
// yet (no draft, an empty failure lane), the card shows an example of what will be
// there. The page stays usable underneath, so a person can try what a step shows.
// Which tours someone finished is theirs alone, kept in their browser.

import { useQuery } from "@tanstack/react-query";
import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore, type CSSProperties, type ReactElement } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { api, type Me, type PrRow } from "./api";
import { prPath } from "./components";
import type { Role, Text } from "./guide";
import { useLang, useT } from "./i18n";
import { Icon, type IconName } from "./icons";
import { Rich } from "./rich";
import { targetSelector, tourById, TOURS, type ExampleId, type Tour, type TourStep } from "./tours";

// ---- which tour runs, and which are done ----------------------------------------------------

const DONE_KEY = "am.tours.done";
const HIDDEN_KEY = "am.tours.hidden";

type Run = { id: string; step: number } | null;
let run: Run = null;
let done: Set<string> = readDone();
const listeners = new Set<() => void>();

function readDone(): Set<string> {
  try {
    const raw = JSON.parse(localStorage.getItem(DONE_KEY) ?? "[]");
    return new Set(Array.isArray(raw) ? raw.filter((x): x is string => typeof x === "string") : []);
  } catch {
    return new Set();
  }
}

function emit() {
  for (const f of listeners) f();
}

function subscribe(f: () => void) {
  listeners.add(f);
  return () => listeners.delete(f);
}

/** Start a walkthrough at its first step, from anywhere. */
export function startTour(id: string) {
  if (!tourById(id)) return;
  run = { id, step: 0 };
  emit();
}

function goTo(step: number) {
  if (run) run = { ...run, step };
  emit();
}

function endTour(finished: boolean) {
  if (run && finished) {
    done = new Set(done).add(run.id);
    try {
      localStorage.setItem(DONE_KEY, JSON.stringify([...done]));
    } catch {
      // the browser keeps no storage here: the tour shows as not done next time
    }
  }
  run = null;
  emit();
}

export function useTourRun(): Run {
  return useSyncExternalStore(subscribe, () => run);
}

export function useToursDone(): Set<string> {
  return useSyncExternalStore(subscribe, () => done);
}

// ---- who a tour is for ---------------------------------------------------------------------

interface MyAccess {
  owner: boolean;
  admin: boolean;
  repos: { repo: string; role: string | null }[];
}

function useMyAccess(enabled = true) {
  return useQuery({ queryKey: ["access", "me"], queryFn: () => api.get<MyAccess>("/api/access/me"), enabled, staleTime: 60_000 });
}

/** The guide's roles this person has: a reviewer always; a maintainer where a
 *  repository makes them one; an admin when they own or administer the service. */
function rolesOf(access: MyAccess | undefined): Set<Role> {
  const roles = new Set<Role>(["reviewer", "owner"]);
  if (access?.repos.some((r) => r.role === "maintainer")) roles.add("maintainer");
  if (access?.owner || access?.admin) roles.add("admin");
  return roles;
}

// ---- examples for a page that has nothing to point at yet --------------------------------------

function WorkExample() {
  const t = useT();
  return (
    <div className="tour-mock">
      <div className="row" style={{ gap: 8 }}>
        <span className="tour-spin" aria-hidden="true" />
        <strong className="small">{t("Reviewing")} #128</strong>
        <span className="small muted">· Claude Code · 3 min</span>
      </div>
      <ul className="tour-mock-steps small">
        <li>
          <Icon name="check" size={12} /> {t("Read the diff and the files it touches")}
        </li>
        <li>
          <Icon name="check" size={12} /> {t("Ran the tests it changes")}
        </li>
        <li className="muted">{t("Writing the review…")}</li>
      </ul>
    </div>
  );
}

function DraftExample() {
  const t = useT();
  return (
    <div className="tour-mock">
      <div className="row" style={{ gap: 8 }}>
        <span className="kind-chip">{t("Review draft")}</span>
        <strong className="small">#128</strong>
      </div>
      <p className="small" style={{ margin: "6px 0" }}>
        <span className="kind-chip warn">{t("major")}</span> <code>src/sampler.py:42</code> {t("The seed is read after the workers start, so runs differ.")}
      </p>
      <div className="row" style={{ gap: 6 }}>
        <span className="tour-mock-button primary">{t("Post as me")}</span>
        <span className="tour-mock-button">{t("Discuss")}</span>
        <span className="tour-mock-button">{t("Reject")}</span>
      </div>
    </div>
  );
}

function FailureExample() {
  const t = useT();
  return (
    <div className="tour-mock">
      <div className="row" style={{ gap: 8 }}>
        <strong className="small">#131</strong>
        <span className="small muted">{t("it conflicts with")} main</span>
      </div>
      <div className="row" style={{ gap: 6, marginTop: 6, flexWrap: "wrap" }}>
        <span className="tour-mock-button">
          <Icon name="merge" size={12} /> {t("Resolve conflicts")}
        </span>
        <span className="tour-mock-button">{t("Fix CI")}</span>
        <span className="tour-mock-button">{t("Retry")}</span>
      </div>
    </div>
  );
}

/** Every example a step may name (tours.ts `ExampleId`). */
export const EXAMPLES: Record<ExampleId, () => ReactElement> = {
  work: WorkExample,
  draft: DraftExample,
  failure: FailureExample,
};

// ---- the walkthrough on the page --------------------------------------------------------------

const SEEK_MS = 2500;
const GAP = 12;

type Found = { phase: "seeking" } | { phase: "found"; el: Element } | { phase: "missing" };

function visible(el: Element): boolean {
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
}

/** The step's page with `{repo}` and `{pr}` filled in; null while what fills them
 *  loads, "" when the person has none. */
function useStepPath(step: TourStep | undefined, me: Me): string | null {
  const needsPr = !!step?.path?.includes("{pr}");
  const needsRepo = !!step?.path?.includes("{repo}");
  const prs = useQuery({ queryKey: ["prs", "all"], queryFn: () => api.get<PrRow[]>("/api/prs"), enabled: needsPr });
  const access = useMyAccess(needsRepo);
  if (!step?.path) return "";
  let path = step.path;
  if (needsPr) {
    if (prs.isLoading) return null;
    const rows = prs.data ?? [];
    const pr = rows.find((p) => p.review_activity.posted || p.review_activity.draft) ?? rows[0];
    if (!pr) return "";
    path = path.replace("/pr/{pr}", prPath(pr.repo, pr.number));
  }
  if (needsRepo) {
    if (access.isLoading) return null;
    const repo = access.data?.repos.find((r) => r.role === "maintainer")?.repo ?? me.repos[0];
    if (!repo) return "";
    path = path.replace("{repo}", repo);
  }
  return path;
}

export function TourLayer({ me }: { me: Me }) {
  const current = useTourRun();
  const tour = current ? tourById(current.id) : undefined;
  if (!current || !tour) return null;
  return <TourStepView key={`${tour.id}:${current.step}`} me={me} tour={tour} index={current.step} />;
}

function TourStepView({ me, tour, index }: { me: Me; tour: Tour; index: number }) {
  const t = useT();
  const { lang } = useLang();
  const tx = (text: Text) => text[lang];
  const navigate = useNavigate();
  const location = useLocation();
  const step = tour.steps[index];
  const path = useStepPath(step, me);
  const [found, setFound] = useState<Found>({ phase: "seeking" });
  const [rect, setRect] = useState<DOMRect | null>(null);
  const card = useRef<HTMLDivElement>(null);
  const [cardSize, setCardSize] = useState({ w: 360, h: 220 });
  const here = location.pathname + location.search;
  const onPage = !path || here === path;
  const last = index === tour.steps.length - 1;

  // Open the step's page once.
  useEffect(() => {
    if (path && here !== path) navigate(path);
  }, [path]);

  // Find what the step points at, as the page loads.
  useEffect(() => {
    // No target, or a page the person has none of (no repository, no open PR).
    if (!step.target || (step.path && path === "")) {
      setFound({ phase: "missing" });
      return;
    }
    if (path === null || !onPage) {
      setFound({ phase: "seeking" });
      return;
    }
    const selector = targetSelector(step.target);
    const began = Date.now();
    let scrolled = false;
    const look = () => {
      const el = [...document.querySelectorAll(selector)].find(visible);
      if (el) {
        if (!scrolled) {
          el.scrollIntoView({ block: "center", behavior: "smooth" });
          scrolled = true;
        }
        setFound((old) => (old.phase === "found" && old.el === el ? old : { phase: "found", el }));
      } else if (Date.now() - began > SEEK_MS) {
        setFound({ phase: "missing" });
      }
    };
    look();
    const timer = window.setInterval(look, 150);
    return () => window.clearInterval(timer);
  }, [step.target, path, onPage]);

  // Follow it as the page scrolls and changes.
  useEffect(() => {
    if (found.phase !== "found") {
      setRect(null);
      return;
    }
    let frame = 0;
    const update = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => setRect(found.el.isConnected ? found.el.getBoundingClientRect() : null));
    };
    update();
    const timer = window.setInterval(update, 250);
    window.addEventListener("scroll", update, true);
    window.addEventListener("resize", update);
    return () => {
      cancelAnimationFrame(frame);
      window.clearInterval(timer);
      window.removeEventListener("scroll", update, true);
      window.removeEventListener("resize", update);
    };
  }, [found]);

  useLayoutEffect(() => {
    if (card.current) {
      const { offsetWidth: w, offsetHeight: h } = card.current;
      if (w !== cardSize.w || h !== cardSize.h) setCardSize({ w, h });
    }
  });

  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      const typing = e.target instanceof HTMLElement && e.target.closest("input, textarea, select, [contenteditable]");
      if (e.key === "Escape") endTour(false);
      else if (typing) return;
      else if (e.key === "ArrowRight") last ? endTour(true) : goTo(index + 1);
      else if (e.key === "ArrowLeft" && index > 0) goTo(index - 1);
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [index, last]);

  const missing = found.phase === "missing" && !!step.target;
  const Example = missing && step.example ? EXAMPLES[step.example] : null;
  const narrow = window.innerWidth < 640;
  const pad = 6;
  let style: CSSProperties | undefined;
  if (rect && !narrow) {
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const left = Math.min(Math.max(16, rect.left), vw - cardSize.w - 16);
    if (rect.bottom + pad + GAP + cardSize.h < vh - 16) style = { top: rect.bottom + pad + GAP, left };
    else if (rect.top - pad - GAP - cardSize.h > 16) style = { top: rect.top - pad - GAP - cardSize.h, left };
    else style = { bottom: 16, left };
  }

  return (
    <>
      {rect ? (
        <div
          className="tour-hole"
          aria-hidden="true"
          style={{ top: rect.top - pad, left: rect.left - pad, width: rect.width + pad * 2, height: rect.height + pad * 2 }}
        />
      ) : (
        <div className="tour-dim" aria-hidden="true" />
      )}
      <div ref={card} className={`tour-card ${style ? "placed" : ""}`} style={style} role="dialog" aria-label={tx(tour.title)}>
        <div className="row" style={{ gap: 8 }}>
          <Icon name={tour.icon as IconName} size={14} />
          <span className="small muted grow">
            {tx(tour.title)} · {index + 1} / {tour.steps.length}
          </span>
          <button className="ghost icon sm" onClick={() => endTour(false)} aria-label={t("End the walkthrough")} title={t("End the walkthrough")}>
            <Icon name="x" size={14} />
          </button>
        </div>
        <strong className="tour-title">{tx(step.title)}</strong>
        <p className="tour-body">
          <Rich text={tx(step.body)} />
        </p>
        {Example && (
          <div className="stack" style={{ gap: 6 }}>
            <span className="small muted">{t("Not on this page yet. It will look like this:")}</span>
            <Example />
          </div>
        )}
        {missing && !Example && (
          <p className="small muted" style={{ margin: 0 }}>
            {step.missing ? <Rich text={tx(step.missing)} /> : t("Not on this page yet.")}
          </p>
        )}
        {path && !onPage && (
          <button className="sm" style={{ alignSelf: "flex-start" }} onClick={() => navigate(path)}>
            {t("Show me")}
          </button>
        )}
        <div className="row" style={{ gap: 8 }}>
          <span className="tour-dots" aria-hidden="true">
            {tour.steps.map((_, i) => (
              <span key={i} className={i === index ? "on" : i < index ? "past" : ""} />
            ))}
          </span>
          <span className="spacer" />
          {index > 0 && (
            <button className="ghost sm" onClick={() => goTo(index - 1)}>
              {t("Back")}
            </button>
          )}
          <button className="primary sm" autoFocus onClick={() => (last ? endTour(true) : goTo(index + 1))}>
            {last ? t("Done") : t("Next step")}
            {!last && <Icon name="chevronRight" size={13} />}
          </button>
        </div>
      </div>
    </>
  );
}

// ---- the list of walkthroughs ---------------------------------------------------------------------

function readHidden(): boolean {
  try {
    return localStorage.getItem(HIDDEN_KEY) === "1";
  } catch {
    return false;
  }
}

/** The walkthroughs, the ones for this person first. On Home (`welcome`) it greets a
 *  newcomer with a single compact banner for the most relevant next walkthrough,
 *  leaving the full list on the Guide page (`<ToursCard />`), and hides once they
 *  dismiss it or have walked through every one meant for them. */
export function ToursCard({ welcome = false }: { welcome?: boolean }) {
  const t = useT();
  const { lang } = useLang();
  const tx = (text: Text) => text[lang];
  const finished = useToursDone();
  const access = useMyAccess();
  const [hidden, setHidden] = useState(() => welcome && readHidden());
  const roles = rolesOf(access.data);
  const forMe = (tour: Tour) => tour.who.some((r) => roles.has(r));
  const tours = [...TOURS].sort((a, b) => Number(forMe(b)) - Number(forMe(a)));
  const remaining = tours.filter((tour) => forMe(tour) && !finished.has(tour.id));
  const next = remaining[0];
  if (welcome && (hidden || !next)) return null;

  if (welcome && next) {
    const dismiss = () => {
      setHidden(true);
      try {
        localStorage.setItem(HIDDEN_KEY, "1");
      } catch {
        // it then hides for this page only
      }
    };
    return (
      <section className="card welcome-banner" id="tours">
        <div className="welcome-banner-head">
          <span className="tour-item-icon">
            <Icon name={next.icon as IconName} size={15} />
          </span>
          <div className="welcome-banner-text">
            <div className="row" style={{ gap: 6 }}>
              <strong style={{ fontSize: 14.5 }}>{finished.size ? tx(next.title) : `${t("New here?")} ${tx(next.title)}`}</strong>
              <span className="small muted">
                · {next.steps.length} {t("steps")}
              </span>
            </div>
            <span className="small muted">{tx(next.summary)}</span>
          </div>
          <button className="ghost icon sm welcome-banner-close" onClick={dismiss} aria-label={t("Hide")} title={t("Hide")}>
            <Icon name="x" size={14} />
          </button>
        </div>
        <div className="welcome-banner-actions">
          <button className="primary sm" onClick={() => startTour(next.id)}>
            <Icon name="play" size={13} />
            {t("Start walkthrough")}
          </button>
          <Link className="button ghost sm" to="/guide#tours">
            {t("All walkthroughs")}
            {remaining.length > 1 ? ` (${remaining.length})` : ""}
          </Link>
          <Link className="button ghost sm" to="/guide">
            {t("Open the guide")}
          </Link>
        </div>
      </section>
    );
  }

  return (
    <section className="card tours" id="tours">
      <div className="row" style={{ marginBottom: 10, flexWrap: "wrap" }}>
        <Icon name="compass" />
        <strong style={{ fontSize: 15 }}>{t("Walkthroughs")}</strong>
        <span className="small muted">{t("A few steps on the real pages: each points at what you use and says what it does.")}</span>
      </div>
      <div className="tour-list">
        {tours.map((tour) => {
          const isDone = finished.has(tour.id);
          return (
            <div key={tour.id} className={`tour-item ${isDone ? "done" : ""}`}>
              <span className="tour-item-icon">{isDone ? <Icon name="check" size={15} /> : <Icon name={tour.icon as IconName} size={15} />}</span>
              <span className="stack" style={{ gap: 2, minWidth: 0, flex: 1 }}>
                <span className="row" style={{ gap: 6 }}>
                  <strong>{tx(tour.title)}</strong>
                  {forMe(tour) && !isDone && <span className="pill">{t("For you")}</span>}
                </span>
                <span className="small muted">
                  {tx(tour.summary)} · {tour.steps.length} {t("steps")}
                </span>
              </span>
              <button className={`sm ${tour.id === next?.id ? "primary" : ""}`} onClick={() => startTour(tour.id)}>
                {isDone ? t("Again") : t("Start")}
              </button>
            </div>
          );
        })}
      </div>
    </section>
  );
}
