// How a PR's review goes, for its owners (design 5.13): who reviewed it and how much of
// each reviewer's feedback is addressed (their review threads resolved), the approvals
// against what the branch needs, and a timeline of reviews and pushes. The PR list
// shows the summary in a cell; the PR page shows it all on its Review progress tab.

import { useQuery } from "@tanstack/react-query";
import { api, type PrProgress, type ProgressEvent, type ProgressSummary, type ReviewerProgress } from "./api";
import { useAliasOf } from "./aliases";
import { Avatar, Named } from "./avatar";
import { Empty, ErrorBox, Section, When } from "./components";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { Skeleton } from "./ui";

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
  if (e.kind === "opened") return e.commits ? `${t("opened the PR with")} ${e.commits} ${t("commits")}` : t("opened the PR");
  if (e.kind === "role") return `${e.state === "owner" ? t("became an owner") : t("became a reviewer")} (${e.detail.startsWith("assigned by") ? `${t("assigned by")} ${name(e.detail.slice(12))}` : t("claimed")})`;
  if (e.kind === "commits") return `${t("pushed")} ${e.commits} ${e.commits === 1 ? t("commit") : t("commits")}${e.detail ? `: ${e.detail}` : ""}`;
  const verdict =
    e.state === "APPROVED" ? t("approved") : e.state === "CHANGES_REQUESTED" ? t("requested changes") : e.state === "DISMISSED" ? t("had a review dismissed") : t("commented");
  return e.threads ? `${verdict}, ${e.threads} ${t("threads")} (${e.resolved} ${t("resolved now")})` : verdict;
}

const ICONS: Record<ProgressEvent["kind"], "pr" | "check" | "upload" | "message"> = { opened: "pr", role: "check", commits: "upload", review: "message" };

/** The PR page's Review progress tab. */
export function ReviewProgressPanel({ repo, number }: { repo: string; number: number }) {
  const t = useT();
  const aliasOf = useAliasOf();
  const q = useQuery({ queryKey: ["pr", repo, String(number), "progress"], queryFn: () => api.get<PrProgress>(`/api/prs/${repo}/${number}/progress`) });
  if (q.isLoading) return <Skeleton rows={5} />;
  if (!q.data) return <ErrorBox error={q.error} />;
  const d = q.data;
  const need = d.required_approvals;
  const name = (l: string) => aliasOf(l) ?? l;
  return (
    <>
      <Section title={t("Review progress")} hint={t("for the PR's owners")}>
        <div className="progress-stats">
          <div className="stack" style={{ gap: 4 }}>
            <span className="small muted">{t("Feedback addressed")}</span>
            <span className="row" style={{ gap: 8 }}>
              <strong style={{ fontSize: 20 }}>{percent(d.addressed)}</strong>
              <AddressedBar value={d.addressed} width={120} />
            </span>
            <span className="small muted">
              {d.resolved}/{d.threads} {t("review threads resolved")}
              {d.outdated > 0 && ` · ${d.outdated} ${t("changed since, not resolved")}`}
            </span>
          </div>
          <div className="stack" style={{ gap: 4 }}>
            <span className="small muted">{t("Approvals")}</span>
            <strong style={{ fontSize: 20 }}>
              {d.approved_by.length}
              {need ? <span className="muted" style={{ fontSize: 14 }}> / {need} {t("required")}</span> : null}
            </strong>
            <span className="small muted">
              {d.approved_by.length ? d.approved_by.map(name).join(", ") : t("none yet")}
              {d.approved_by.length > d.approved_at_head.length && ` · ${d.approved_by.length - d.approved_at_head.length} ${t("on an earlier commit")}`}
            </span>
          </div>
          <div className="stack" style={{ gap: 4 }}>
            <span className="small muted">{t("Changes requested")}</span>
            <strong style={{ fontSize: 20 }}>{d.changes_requested_by.length}</strong>
            <span className="small muted">{d.changes_requested_by.length ? d.changes_requested_by.map(name).join(", ") : t("by nobody")}</span>
          </div>
        </div>
      </Section>
      <Section title={t("Reviewers")} hint={t("assigned here first, then anyone else who reviewed")}>
        {d.reviewers.length === 0 ? (
          <Empty icon="pr">{t("Nobody reviews this PR yet.")}</Empty>
        ) : (
          <div className="list">
            {d.reviewers.map((r, i) => (
              <div key={r.login} className={`list-row progress-reviewer${!r.assigned && i > 0 && d.reviewers[i - 1].assigned ? " first-other" : ""}`}>
                <Avatar login={r.login} size={24} />
                <span className="main">
                  <Named login={r.login} alias={aliasOf(r.login)} />
                  {r.code_owner && <span className="pill status-info" style={{ marginLeft: 6 }}>{t("code owner")}</span>}
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
                <span className="row small" style={{ gap: 6, flexWrap: "nowrap", minWidth: 150 }} title={r.outdated ? `${r.outdated} ${t("changed since, not resolved")}` : undefined}>
                  <AddressedBar value={r.addressed} width={60} />
                  {r.threads ? `${r.resolved}/${r.threads} ${t("resolved")}` : <span className="muted">{t("no threads")}</span>}
                </span>
              </div>
            ))}
          </div>
        )}
      </Section>
      <CodeOwnerApprovals d={d} />
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
function CodeOwnerApprovals({ d }: { d: PrProgress }) {
  const t = useT();
  const aliasOf = useAliasOf();
  const c = d.code_owners;
  if (!c.file) return null;
  const people = c.people;
  return (
    <Section
      title={t("Code owners")}
      hint={c.required ? t("GitHub merges only with their approval") : undefined}
      aside={
        c.parts > 0 ? (
          <span className={`pill ${c.approved_parts === c.parts ? "status-ok" : c.required ? "status-warn" : "status-muted"}`}>
            {c.approved_parts}/{c.parts} {t("parts approved")}
          </span>
        ) : undefined
      }
    >
      {people.length === 0 ? (
        <span className="small muted">{t("No code owner owns the files this PR changes.")}</span>
      ) : (
        <div className="list">
          {people.map((o) => (
            <div key={o.login} className="list-row progress-reviewer">
              {o.team ? <Icon name="list" size={18} /> : <Avatar login={o.login} size={24} />}
              <span className="main">
                <Named login={o.login} alias={o.team ? undefined : aliasOf(o.login)} />
                <span className="small muted" style={{ display: "block" }}>
                  {[o.team ? t("a team") : null, `${o.files} ${o.files === 1 ? t("file") : t("files")}`, o.requested ? t("asked on GitHub") : null].filter(Boolean).join(" · ")}
                </span>
              </span>
              {o.author ? (
                <span className="pill status-muted" title={t("GitHub does not count an author's approval of their own PR")}>
                  {t("author")}
                </span>
              ) : o.team ? (
                <span className="pill status-muted">{o.requested ? t("review requested") : t("not asked")}</span>
              ) : (
                <Verdict r={{ verdict: o.verdict as ReviewerProgress["verdict"], at_head: o.at_head }} />
              )}
            </div>
          ))}
        </div>
      )}
    </Section>
  );
}
