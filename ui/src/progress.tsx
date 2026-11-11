// How a PR's review goes, for its owners (design 5.13): who reviewed it and how much of
// each reviewer's feedback is addressed (their review threads resolved), the approvals
// against what the branch needs, and a timeline of reviews and pushes. The PR list
// shows the summary in a cell; the PR page shows it all on its Review progress tab.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type CommentItem, type PrProgress, type ProgressEvent, type ProgressSummary, type ReviewerProgress } from "./api";
import { useAliasOf } from "./aliases";
import { Avatar, Named } from "./avatar";
import { ApprovalsBlock, OwnerGroups, useApprovalBadge } from "./codeOwners";
import { ConnectGitHub } from "./connectGitHub";
import { Empty, ErrorBox, Section, useAct, When } from "./components";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { Popover, Skeleton, useToast } from "./ui";

function percent(x: number | null): string {
  return x === null ? "—" : `${Math.round(x * 100)}%`;
}

/** A thin bar of the share addressed. */
export function AddressedBar({ value, width = 72 }: { value: number | null; width?: number }) {
  return (
    <span className="addressed-bar" style={{ width }} aria-hidden>
      <span style={{ width: `${Math.round((value ?? 0) * 100)}%` }} />
    </span>
  );
}

/** Review progress in one short line: feedback addressed, approvals, changes
 * requested, and how many reviewers reviewed; nothing before any review. */
export function ProgressInline({ p }: { p: ProgressSummary | undefined }) {
  const t = useT();
  if (!p || (!p.reviewed && !p.threads)) return null;
  return (
    <span
      className="progress-inline small"
      title={`${p.resolved}/${p.threads} ${t("review threads resolved")} · ${p.approved} ${t("approved")} · ${p.changes_requested} ${t("changes requested")} · ${p.reviewed}/${p.reviewers} ${t("reviewed")}`}
    >
      {p.threads > 0 && (
        <>
          <AddressedBar value={p.addressed} width={40} />
          <span>{percent(p.addressed)}</span>
        </>
      )}
      {p.approved > 0 && <span className="check-ok">✓{p.approved}</span>}
      {p.changes_requested > 0 && <span style={{ color: "var(--bad)" }}>✗{p.changes_requested}</span>}
      <span className="muted">
        {p.reviewed}/{p.reviewers}
      </span>
    </span>
  );
}

/** The PR list's cell: feedback addressed, approvals, and changes requested. */
export function ProgressCell({ p }: { p: ProgressSummary | undefined }) {
  const t = useT();
  if (!p || (!p.reviewed && !p.threads)) return <span className="muted small">{t("no review yet")}</span>;
  return (
    <span
      className="stack small"
      style={{ gap: 2 }}
      title={`${p.resolved}/${p.threads} ${t("review threads resolved")} · ${p.approved} ${t("approved")} · ${p.changes_requested} ${t("changes requested")}`}
    >
      <span className="row" style={{ gap: 6, flexWrap: "nowrap" }}>
        <AddressedBar value={p.addressed} width={56} />
        <span>{percent(p.addressed)}</span>
      </span>
      <span className="muted" style={{ whiteSpace: "nowrap" }}>
        {p.approved > 0 && <span className="check-ok">✓{p.approved} </span>}
        {p.changes_requested > 0 && <span style={{ color: "var(--bad)" }}>✗{p.changes_requested} </span>}
        {p.reviewed}/{p.reviewers} {t("reviewed")}
      </span>
    </span>
  );
}

function Verdict({ r }: { r: Pick<ReviewerProgress, "verdict" | "at_head"> }) {
  const t = useT();
  if (!r.verdict) return <span className="pill status-muted">{t("not reviewed yet")}</span>;
  const [label, tone] =
    r.verdict === "APPROVED"
      ? [t("approved"), "status-ok"]
      : r.verdict === "CHANGES_REQUESTED"
        ? [t("changes requested"), "status-bad"]
        : r.verdict === "DISMISSED"
          ? [t("review dismissed"), "status-muted"]
          : [t("commented"), "status-info"];
  return (
    <span className={`pill ${tone}`} title={r.at_head ? t("At the current head") : t("On an earlier commit; the PR changed since")}>
      {label}
      {!r.at_head && ` · ${t("earlier commit")}`}
    </span>
  );
}

function eventText(e: ProgressEvent, t: (s: string) => string, name: (login: string) => string): string {
  if (e.kind === "check") return `${t("checked")} ${e.threads} ${t("comments against the code")} (${e.resolved} ${t("addressed")}) ${t("at")} ${e.detail}`;
  if (e.kind === "opened") return e.commits ? `${t("opened the PR with")} ${e.commits} ${t("commits")}` : t("opened the PR");
  if (e.kind === "role") return `${e.state === "owner" ? t("became an owner") : t("became a reviewer")} (${e.detail.startsWith("assigned by") ? `${t("assigned by")} ${name(e.detail.slice(12))}` : t("claimed")})`;
  if (e.kind === "commits") return `${t("pushed")} ${e.commits} ${e.commits === 1 ? t("commit") : t("commits")}${e.detail ? `: ${e.detail}` : ""}`;
  const verdict =
    e.state === "APPROVED" ? t("approved") : e.state === "CHANGES_REQUESTED" ? t("requested changes") : e.state === "DISMISSED" ? t("had a review dismissed") : t("commented");
  return e.threads ? `${verdict}, ${e.threads} ${t("threads")} (${e.resolved} ${t("resolved now")})` : verdict;
}

const ICONS: Record<ProgressEvent["kind"], "pr" | "check" | "upload" | "message" | "search"> = { opened: "pr", role: "check", commits: "upload", review: "message", check: "search" };

/** The PR page's Review progress tab. */
export function ReviewProgressPanel({ repo, number, me }: { repo: string; number: number; me: string }) {
  const t = useT();
  const toast = useToast();
  const aliasOf = useAliasOf();
  const [checkingSince, setCheckingSince] = useState<number | null>(null);
  const q = useQuery({
    queryKey: ["pr", repo, String(number), "progress"],
    queryFn: () => api.get<PrProgress>(`/api/prs/${repo}/${number}/progress`),
    // While a check runs, look again every few seconds for its results (at most 3 minutes).
    refetchInterval: checkingSince && Date.now() - checkingSince < 180_000 ? 4000 : false,
  });
  const check = useAct((ids: string[]) => api.post<{ event: string }>(`/api/prs/${repo}/${number}/comment-check`, { thread_ids: ids }), []);
  const startCheck = (ids: string[]) =>
    check.mutate(ids, {
      onSuccess: () => {
        setCheckingSince(Date.now());
        toast({ title: t("Checking the comments against the code"), body: t("Results appear here in a minute or two."), icon: "search" });
      },
    });
  if (q.isLoading) return <Skeleton rows={5} />;
  if (!q.data) return <ErrorBox error={q.error} />;
  const d = q.data;
  // The approvals that count under the branch's rules, as GitHub counts them.
  const counted = d.approvals?.approved_by ?? d.approved_by;
  const need = d.approvals ? d.approvals.required : d.required_approvals;
  const name = (l: string) => aliasOf(l) ?? l;
  return (
    <>
      <Section
        title={t("Review progress")}
        hint={t("for the PR's owners")}
        aside={
          d.threads > 0 ? (
            <CommentBulkActions d={d} me={me} repo={repo} number={number} checking={check.isPending} onCheck={startCheck} />
          ) : undefined
        }
      >
        <div className="progress-stats">
          <div className="stack" style={{ gap: 4 }}>
            <span className="small muted">{t("Feedback addressed")}</span>
            <span className="row" style={{ gap: 8 }}>
              <strong style={{ fontSize: 20 }}>{percent(d.addressed)}</strong>
              <AddressedBar value={d.addressed} width={120} />
            </span>
            <span className="small muted">
              {d.resolved} {t("resolved on GitHub")} · {d.fixed} {t("fixed in the code")} · {d.threads} {t("comments")}
              {d.outdated > 0 && ` · ${d.outdated} ${t("changed since, not resolved")}`}
            </span>
          </div>
          <div className="stack" style={{ gap: 4 }}>
            <span className="small muted">{t("Approvals")}</span>
            <strong style={{ fontSize: 20 }}>
              {counted.length}
              {need ? <span className="muted" style={{ fontSize: 14 }}> / {need} {t("required")}</span> : null}
            </strong>
            <span className="small muted">
              {counted.length ? counted.map(name).join(", ") : t("none yet")}
              {d.approvals?.missing ? ` · ${t("needs")} ${d.approvals.missing} ${t("more from anyone with write access")}` : ""}
            </span>
          </div>
          <div className="stack" style={{ gap: 4 }}>
            <span className="small muted">{t("Changes requested")}</span>
            <strong style={{ fontSize: 20 }}>{d.changes_requested_by.length}</strong>
            <span className="small muted">{d.changes_requested_by.length ? d.changes_requested_by.map(name).join(", ") : t("by nobody")}</span>
          </div>
        </div>
        <ErrorBox error={check.error} />
      </Section>
      <Section title={t("Reviewers")} hint={t("assigned here first, then anyone else who reviewed")}>
        {!d.signed_in && d.can_resolve && d.threads > d.resolved && (
          <div style={{ marginBottom: 10 }}>
            <ConnectGitHub why={t("Sign in with GitHub to resolve comments as yourself.")} />
          </div>
        )}
        {d.reviewers.length === 0 ? (
          <Empty icon="pr">{t("Nobody reviews this PR yet.")}</Empty>
        ) : (
          <div className="list">
            {d.reviewers.map((r, i) => (
              <ReviewerRow
                key={r.login}
                r={r}
                first={!r.assigned && i > 0 && d.reviewers[i - 1].assigned}
                mayResolve={d.can_resolve || r.login.toLowerCase() === me.toLowerCase()}
                signedIn={d.signed_in}
                repo={repo}
                number={number}
                onCheck={(id) => startCheck([id])}
                checking={check.isPending}
              />
            ))}
          </div>
        )}
      </Section>
      <CodeOwnerApprovals d={d} repo={repo} />
      <Section title={t("Timeline")}>
        {d.timeline.length === 0 ? (
          <Empty icon="clock">{t("Nothing happened yet.")}</Empty>
        ) : (
          <ol className="progress-timeline">
            {d.timeline.map((e, i) => (
              <li key={i} className={`step-${e.kind}`}>
                <span className="dot">
                  <Icon name={ICONS[e.kind]} size={12} />
                </span>
                <span className="small">
                  <strong>{name(e.login)}</strong> {eventText(e, t, name)}
                  <span className="muted"> · {e.at ? <When at={e.at} /> : t("time unknown")}</span>
                </span>
              </li>
            ))}
          </ol>
        )}
      </Section>
    </>
  );
}

/** Whether the code owners approved: each owner's verdict, and how many parts of the
 * change have an approval from one of their owners. */
function CodeOwnerApprovals({ d, repo }: { d: PrProgress; repo: string }) {
  const t = useT();
  const c = d.code_owners;
  const badge = useApprovalBadge()(d.approvals, c.waiting ?? []);
  return (
    <Section title={t("Approvals")} aside={badge ? <span className={`pill ${badge.tone}`}>{badge.text}</span> : undefined}>
      <div className="stack" style={{ gap: 14 }}>
        {d.approvals && <ApprovalsBlock a={d.approvals} repo={repo} />}
        {c.file && (
          <div className="appr-block">
            <span className="appr-title" title={c.required ? t("GitHub merges only with their approval") : undefined}>
              {t("Code owners")}
            </span>
            {c.people.length === 0 ? (
              <span className="small muted">{t("No code owner owns the files this PR changes.")}</span>
            ) : (
              <OwnerGroups repo={repo} people={c.people} waiting={c.waiting ?? []} />
            )}
          </div>
        )}
      </div>
    </Section>
  );
}

/** Check every open comment against the code again, or resolve them on GitHub as
 * oneself: those the check found fixed, or all the person may resolve. */
function CommentBulkActions({
  d,
  me,
  repo,
  number,
  checking,
  onCheck,
}: {
  d: PrProgress;
  me: string;
  repo: string;
  number: number;
  checking: boolean;
  onCheck: (ids: string[]) => void;
}) {
  const t = useT();
  const toast = useToast();
  const open = d.reviewers.flatMap((r) => r.comments.filter((c) => !c.resolved).map((c) => ({ c, mine: d.can_resolve || r.login.toLowerCase() === me.toLowerCase() })));
  const resolvable = open.filter((o) => o.mine).map((o) => o.c);
  const fixed = resolvable.filter((c) => c.check?.status === "addressed");
  const resolve = useAct(
    (ids: string[]) => api.post<{ resolved: number; skipped: { thread_id: string; reason: string }[] }>(`/api/prs/${repo}/${number}/threads/resolve`, { thread_ids: ids }),
    [["pr", repo, String(number), "progress"]],
  );
  const run = (ids: string[], close: () => void) =>
    resolve.mutate(ids, {
      onSuccess: (r) => {
        close();
        toast({
          title: `${r.resolved} ${r.resolved === 1 ? t("comment resolved") : t("comments resolved")}`,
          body: r.skipped.length ? `${r.skipped.length} ${t("left as they were")}: ${[...new Set(r.skipped.map((s) => s.reason))].join("; ")}` : undefined,
          icon: r.resolved ? "check" : "x",
        });
      },
    });
  return (
    <span className="row" style={{ gap: 6 }}>
      <button
        className="sm"
        disabled={checking || open.length === 0}
        onClick={() => onCheck(open.map((o) => o.c.thread_id))}
        title={`${t("Check every open comment against the code now, again")}${d.comment_checks === "scan" ? ` (${t("also checked on the repository's schedule")})` : ""}`}
      >
        <Icon name="search" size={13} />
        {t("Check all")}
        {open.length > 0 && <span className="muted"> {open.length}</span>}
      </button>
      {resolvable.length > 0 && (
        <Popover
          align="right"
          label={t("Resolve all")}
          trigger={(isOpen, toggle) => (
            <button
              className={`sm ${isOpen ? "on" : ""}`}
              aria-expanded={isOpen}
              disabled={!d.signed_in || resolve.isPending}
              title={d.signed_in ? t("Resolve comments on GitHub, as you") : t("Sign in with GitHub to resolve comments as yourself")}
              onClick={toggle}
            >
              <Icon name="check" size={13} />
              {resolve.isPending ? t("Resolving…") : t("Resolve all")}
            </button>
          )}
        >
          {(close) => (
            <div className="stack" style={{ gap: 8, padding: 8, width: "min(320px, calc(100vw - 24px))" }}>
              <strong className="small">{t("Resolve on GitHub, as you")}</strong>
              <button className="primary sm" disabled={fixed.length === 0 || resolve.isPending} onClick={() => run(fixed.map((c) => c.thread_id), close)}>
                {t("The ones fixed in the code")} ({fixed.length})
              </button>
              <button className="sm" disabled={resolve.isPending} onClick={() => run(resolvable.map((c) => c.thread_id), close)}>
                {t("Every open comment")} ({resolvable.length})
              </button>
              <span className="small muted">
                {resolvable.length < open.length
                  ? `${open.length - resolvable.length} ${t("others are not yours to resolve: their reviewers, the PR's owners, or a maintainer resolve them.")}`
                  : t("Only the latest check says which are fixed; check all first for a fresh answer.")}
              </span>
              <ErrorBox error={resolve.error} />
            </div>
          )}
        </Popover>
      )}
    </span>
  );
}

/** A reviewer's progress, which opens to the comments they left. */
function ReviewerRow({
  r,
  first,
  mayResolve,
  signedIn,
  repo,
  number,
  onCheck,
  checking,
}: {
  r: ReviewerProgress;
  first: boolean;
  mayResolve: boolean;
  signedIn: boolean;
  repo: string;
  number: number;
  onCheck: (threadId: string) => void;
  checking: boolean;
}) {
  const t = useT();
  const aliasOf = useAliasOf();
  const [open, setOpen] = useState(false);
  const done = r.comments.filter((c) => c.done).length;
  const has = r.comments.length > 0;
  return (
    <div className={`progress-reviewer-block${first ? " first-other" : ""}`}>
      <div
        className={`list-row progress-reviewer${has ? " expandable" : ""}`}
        role={has ? "button" : undefined}
        tabIndex={has ? 0 : undefined}
        aria-expanded={has ? open : undefined}
        onClick={() => has && setOpen(!open)}
        onKeyDown={(e) => has && (e.key === "Enter" || e.key === " ") && (e.preventDefault(), setOpen(!open))}
      >
        <span className="chevron">{has && <Icon name={open ? "chevronDown" : "chevronRight"} size={12} />}</span>
        <Avatar login={r.login} size={24} />
        <span className="main">
          <Named login={r.login} alias={aliasOf(r.login)} />
          {r.code_owner && (
            <span className="pill status-info" style={{ marginLeft: 6 }}>
              {t("code owner")}
            </span>
          )}
          <span className="small muted" style={{ display: "block" }}>
            {[r.assigned ? t("assigned reviewer") : t("not assigned here"), r.requested ? t("asked on GitHub") : null, r.reviews ? `${r.reviews} ${r.reviews === 1 ? t("review") : t("reviews")}` : null].filter(Boolean).join(" · ")}
            {r.last_at && (
              <>
                {" · "}
                <When at={r.last_at} />
              </>
            )}
          </span>
        </span>
        <Verdict r={r} />
        <span className="row small reviewer-addressed">
          <AddressedBar value={r.addressed} width={60} />
          {has ? `${done}/${r.comments.length} ${t("done")}` : <span className="muted">{t("no comments")}</span>}
        </span>
      </div>
      {open && (
        <div className="comment-items">
          {r.comments.map((c) => (
            <CommentRow key={c.thread_id} c={c} mayResolve={mayResolve} signedIn={signedIn} repo={repo} number={number} onCheck={onCheck} checking={checking} />
          ))}
        </div>
      )}
    </div>
  );
}

/** One review comment: what it asks in one line, whether it is done, and buttons to
 * check it against the code now and to resolve it on GitHub as oneself. */
function CommentRow({
  c,
  mayResolve,
  signedIn,
  repo,
  number,
  onCheck,
  checking,
}: {
  c: CommentItem;
  mayResolve: boolean;
  signedIn: boolean;
  repo: string;
  number: number;
  onCheck: (threadId: string) => void;
  checking: boolean;
}) {
  const t = useT();
  const toast = useToast();
  const [resolvedHere, setResolvedHere] = useState(false);
  const resolve = useAct(
    () => api.post<{ status: string; error: string | null; reason: string | null }>(`/api/prs/${repo}/${number}/threads/${encodeURIComponent(c.thread_id)}/resolve`),
    [["pr", repo, String(number), "progress"]],
  );
  const resolved = c.resolved || resolvedHere;
  const [kind, label] = resolved
    ? ["done", t("resolved on GitHub")]
    : !c.check
      ? ["unchecked", t("not checked yet")]
      : c.check.status === "addressed"
        ? ["done", c.check.fixed_in ? `${t("fixed in the code")} (${c.check.fixed_in.slice(0, 7)})` : t("fixed in the code")]
        : c.check.status === "partly"
          ? ["partly", t("partly addressed")]
          : c.check.status === "open"
            ? ["open", t("not addressed yet")]
            : ["unclear", t("unclear from the code")];
  const mark = { done: "✓", partly: "◐", open: "○", unclear: "?", unchecked: "·" }[kind];
  return (
    <div className={`comment-item comment-${kind}`}>
      <span className="comment-mark" aria-hidden>
        {mark}
      </span>
      <span className="main">
        <span className="comment-summary">{c.check?.summary || c.excerpt || t("(no text)")}</span>
        <span className="small muted" style={{ display: "block" }}>
          {c.url ? (
            <a href={c.url} target="_blank" rel="noreferrer">
              <code>{c.path ? `${c.path}${c.line ? `:${c.line}` : ""}` : t("the comment")}</code>
            </a>
          ) : (
            c.path && <code>{c.path}</code>
          )}
          {" · "}
          <span className={kind === "done" ? "check-ok" : undefined}>{label}</span>
          {c.check && !resolved && (
            <>
              {" · "}
              {t("checked")} <When at={c.check.checked_at} />
            </>
          )}
        </span>
        {c.check?.reason && !resolved && <span className="small muted comment-reason">{c.check.reason}</span>}
      </span>
      {!resolved && (
        <span className="row comment-actions" style={{ gap: 4 }}>
          <button className="ghost sm" disabled={checking} onClick={() => onCheck(c.thread_id)} title={t("Check whether the code now does what this comment asks")}>
            {t("Check")}
          </button>
          {mayResolve && (
            <button
              className="sm"
              disabled={!signedIn || resolve.isPending}
              title={signedIn ? t("Resolve this comment on GitHub, as you") : t("Sign in with GitHub to resolve comments as yourself")}
              onClick={() =>
                resolve.mutate(undefined, {
                  onSuccess: (r) => {
                    if (["queued", "intent", "executed"].includes(r.status)) {
                      setResolvedHere(true);
                      toast({ title: t("Comment resolved"), icon: "check" });
                    } else {
                      toast({ title: t("Not resolved"), body: r.error || r.reason || r.status, icon: "x" });
                    }
                  },
                })
              }
            >
              {t("Resolve")}
            </button>
          )}
        </span>
      )}
      <ErrorBox error={resolve.error} />
    </div>
  );
}
