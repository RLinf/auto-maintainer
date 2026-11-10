// The code owners of a PR's changed files, from CODEOWNERS on its base branch: whom the
// PR's reviewers ask for the approvals a merge waits on. They stay apart from the PR's
// owners on this dashboard (the Reviewers card), because GitHub asks its code owners to
// review nearly every PR, and being asked makes nobody responsible for one here.
//
// Shown as people: who approved, who the merge still waits on (someone who alone owns
// files nobody approved yet, or any one of several who share them), and who is not
// needed because another owner's approval covers their files.

import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api, type ApprovalStatus, type CodeOwnerStanding, type CodeOwners } from "./api";
import { useAliasOf } from "./aliases";
import { Avatar, Named } from "./avatar";
import { ErrorBox, Section } from "./components";
import { forgeOf, links } from "./forges";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { Skeleton } from "./ui";

/** The page of a person or team that CODEOWNERS names, on the repository's forge. */
function ownerUrl(repo: string, owner: CodeOwnerStanding): string | null {
  const prefix = repo.includes(":") ? repo.slice(0, repo.indexOf(":") + 1) : "";
  if (owner.login.includes("@")) return null;
  if (!owner.team) return links.user(prefix + owner.login);
  const forge = forgeOf(repo);
  const web = forge.web_url.replace(/\/+$/, "");
  const [org, ...rest] = owner.login.split("/");
  return forge.kind === "github" ? `${web}/orgs/${org}/teams/${rest.join("/")}` : `${web}/${owner.login}`;
}

/** Where one owner stands, in a word: their verdict, or whether GitHub asks them. */
function standing(o: CodeOwnerStanding, t: (s: string) => string): { text: string; tone: string } | null {
  if (o.author) return { text: t("author"), tone: "status-muted" };
  if (o.verdict === "APPROVED") return o.at_head ? { text: t("approved"), tone: "status-ok" } : { text: t("approved an earlier commit"), tone: "status-ok" };
  if (o.verdict === "CHANGES_REQUESTED") return { text: t("changes requested"), tone: "status-bad" };
  if (o.verdict === "DISMISSED") return { text: t("review dismissed"), tone: "status-muted" };
  if (o.requested) return { text: t("review requested"), tone: "status-info" };
  return null;
}

function unknownOwner(login: string): CodeOwnerStanding {
  return { login, team: login.includes("/"), author: false, verdict: null, at_head: false, requested: false, files: 0, need: "covered" };
}

/** One line about the code owners: who approved, or who the merge still waits on. */
export function useCodeOwnerSummary() {
  const t = useT();
  const aliasOf = useAliasOf();
  const name = (login: string) => (login.includes("/") ? login : (aliasOf(login) ?? login));
  return (people: CodeOwnerStanding[], waiting: string[][], required: boolean | null): { text: string; tone: string } | null => {
    if (!people.length) return null;
    if (!waiting.length) {
      const approvers = people.filter((p) => p.need === "approved").map((p) => name(p.login));
      return { text: approvers.length ? `${t("Approved by")} ${approvers.join(", ")}` : t("No approval needed"), tone: "status-ok" };
    }
    const groups = waiting.map((g) => (g.length === 0 ? t("another approver for the author's files") : g.length === 1 ? name(g[0]) : `${t("one of")} ${g.map(name).join(" / ")}`));
    return { text: `${t("Waiting on")} ${groups.join(", ")}`, tone: required ? "status-warn" : "status-muted" };
  };
}

/** One line on a PR's approvals, as GitHub counts them: changes requested, the
 * approvals still missing toward the branch's number and from code owners, or who
 * approved. GitHub's own verdict wins where it disagrees with the count. */
export function useApprovalSummary() {
  const t = useT();
  const aliasOf = useAliasOf();
  const name = (login: string) => (login.includes("/") ? login : (aliasOf(login) ?? login));
  return (a: ApprovalStatus | undefined): { text: string; tone: string } | null => {
    if (!a) return null;
    if (a.changes_requested_by.length) return { text: `${t("Changes requested by")} ${a.changes_requested_by.map(name).join(", ")}`, tone: "status-bad" };
    const missing: string[] = [];
    if (a.missing) missing.push(`${t("Needs")} ${a.missing} ${a.missing === 1 ? t("more approval") : t("more approvals")}`);
    if (a.code_owners_waiting.length && a.code_owners_required !== false)
      missing.push(
        `${t("Waiting on")} ${a.code_owners_waiting
          .map((g) => (g.length === 0 ? t("another approver for the author's files") : g.length === 1 ? name(g[0]) : `${t("one of")} ${g.map(name).join(" / ")}`))
          .join(", ")} (${t("code owner")})`,
      );
    if (a.decision === "APPROVED" || (!missing.length && a.decision !== "REVIEW_REQUIRED"))
      return a.approved_by.length || a.decision === "APPROVED"
        ? { text: a.approved_by.length ? `${t("Approved by")} ${a.approved_by.map(name).join(", ")}` : t("Approved"), tone: "status-ok" }
        : null;
    if (!missing.length) return { text: t("GitHub still requires a review"), tone: "status-warn" };
    return { text: missing.join(" · "), tone: "status-warn" };
  };
}

const DECISIONS: Record<string, [string, string]> = {
  APPROVED: ["approved", "status-ok"],
  CHANGES_REQUESTED: ["changes requested", "status-bad"],
  REVIEW_REQUIRED: ["review required", "status-warn"],
};

/** The branch's approval rule against the PR's approvals: who counts, who does not
 * and why, how many more are needed, and what GitHub itself says. */
export function ApprovalsBlock({ a, repo }: { a: ApprovalStatus; repo: string }) {
  const t = useT();
  const aliasOf = useAliasOf();
  const prefix = repo.includes(":") ? repo.slice(0, repo.indexOf(":") + 1) : "";
  const person = (login: string, note?: string) => (
    <span key={login} className="owner-chip">
      <Avatar login={prefix + login} size={18} />
      <Named login={login} alias={aliasOf(login)} />
      {note && <span className="pill status-muted">{note}</span>}
    </span>
  );
  const decision = a.decision ? DECISIONS[a.decision] : undefined;
  return (
    <div className="stack" style={{ gap: 8 }}>
      <div className="owner-group">
        <span className="small muted" title={t("Approving reviews from people with write access, which the branch requires apart from the code owners")}>
          {t("Approvals")}
          {a.required != null && ` ${a.approved_by.length}/${a.required}`}
        </span>
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          {a.approved_by.map((l) => person(l))}
          {a.missing ? (
            <span className="pill status-warn" title={t("Any approval from someone with write access counts, code owner or not")}>
              {t("needs")} {a.missing} {t("more from anyone with write access")}
            </span>
          ) : null}
          {!a.approved_by.length && !a.missing && <span className="small muted">{t("none yet")}</span>}
          {decision && (
            <span className={`pill ${decision[1]}`} title={t("GitHub's own verdict on this branch's review rules")}>
              GitHub: {t(decision[0])}
            </span>
          )}
        </div>
      </div>
      {a.not_counted.length > 0 && (
        <div className="owner-group">
          <span className="small muted">{t("Not counted")}</span>
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            {a.not_counted.map((n) => person(n.login, t(n.reason)))}
          </div>
        </div>
      )}
      {a.changes_requested_by.length > 0 && (
        <div className="owner-group">
          <span className="small muted">{t("Changes requested")}</span>
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            {a.changes_requested_by.map((l) => person(l))}
            <span className="small muted">{t("They approve, or their review is dismissed, before GitHub merges.")}</span>
          </div>
        </div>
      )}
      {a.required == null && <span className="small muted">{t("The approvals this base branch requires could not be read; GitHub's verdict above still applies.")}</span>}
      {a.last_push_approval && <span className="small muted">{t("This branch counts only approvals of the latest push.")}</span>}
    </div>
  );
}

/** Who approved, who the merge still waits on, and who is not needed. */
export function OwnerGroups({ repo, people, waiting }: { repo: string; people: CodeOwnerStanding[]; waiting: string[][] }) {
  const t = useT();
  const aliasOf = useAliasOf();
  const prefix = repo.includes(":") ? repo.slice(0, repo.indexOf(":") + 1) : "";
  const byLogin = new Map(people.map((p) => [p.login.toLowerCase(), p]));
  const chip = (login: string, quiet = false) => {
    const o = byLogin.get(login.toLowerCase()) ?? unknownOwner(login);
    const url = ownerUrl(repo, o);
    // Why someone is not needed says more than whether GitHub asked them.
    const s =
      o.need === "covered" && o.covered_by?.length
        ? { text: `${t("covered by")} ${o.covered_by.map((l) => aliasOf(l) ?? l).join(", ")}`, tone: "status-muted" }
        : standing(o, t);
    const name = (
      <>
        {o.team ? <Icon name="list" size={13} /> : <Avatar login={prefix + o.login} size={18} />}
        <Named login={o.login} alias={o.team ? undefined : aliasOf(o.login)} />
      </>
    );
    return (
      <span key={login} className={`owner-chip ${quiet ? "muted" : ""}`} title={o.team ? t("A team: any member's approval counts") : `${o.files} ${o.files === 1 ? t("file") : t("files")}`}>
        {url ? (
          <a href={url} target="_blank" rel="noreferrer">
            {name}
          </a>
        ) : (
          name
        )}
        {s && <span className={`pill ${s.tone}`}>{s.text}</span>}
      </span>
    );
  };
  const approved = people.filter((p) => p.need === "approved");
  const notNeeded = people.filter((p) => p.need === "covered" || p.need === "author");
  const row = (label: string, body: ReactNode, hint?: string) => (
    <div className="owner-group">
      <span className="small muted" title={hint}>
        {label}
      </span>
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        {body}
      </div>
    </div>
  );
  return (
    <div className="stack" style={{ gap: 8 }}>
      {approved.length > 0 && row(t("Approved"), approved.map((p) => chip(p.login)))}
      {waiting.length > 0 &&
        row(
          t("Still needs an approval from"),
          waiting.map((g, i) =>
            g.length === 0 ? (
              <span key={i} className="small">
                {t("someone other than the author, who alone owns some of the files")}
              </span>
            ) : g.length === 1 ? (
              chip(g[0])
            ) : (
              <span key={i} className="row owner-either" style={{ gap: 6, flexWrap: "wrap" }}>
                <span className="small muted">{t("one of")}</span>
                {g.map((login) => chip(login))}
              </span>
            ),
          ),
          t("Each of these approvals is missing; where several people are named, any one of them is enough."),
        )}
      {notNeeded.length > 0 && row(t("Not needed"), notNeeded.map((p) => chip(p.login, true)), t("Another code owner's approval already covers their files, or they wrote the PR."))}
    </div>
  );
}

export function CodeOwnersCard({ repo, number, head }: { repo: string; number: number; head: string }) {
  const t = useT();
  const q = useQuery({
    queryKey: ["pr", repo, String(number), "code-owners", head],
    queryFn: () => api.get<CodeOwners>(`/api/prs/${repo}/${number}/code-owners`),
    staleTime: 60_000,
  });
  const d = q.data;
  const prefix = repo.includes(":") ? repo.slice(0, repo.indexOf(":") + 1) : "";
  const aliasOf = useAliasOf();
  const approvalSummary = useApprovalSummary();
  const ownerSummary = useCodeOwnerSummary();
  const summary = approvalSummary(d?.approvals) ?? ownerSummary(d?.people ?? [], d?.waiting ?? [], d?.required ?? null);
  return (
    <Section
      title={t("Approvals")}
      aside={summary ? <span className={`pill ${summary.tone}`}>{summary.text}</span> : undefined}
    >
      {q.isLoading ? (
        <Skeleton rows={2} />
      ) : !d ? (
        <ErrorBox error={q.error} />
      ) : (
        <div className="stack" style={{ gap: 10 }}>
          {d.approvals && <ApprovalsBlock a={d.approvals} repo={repo} />}
          <span className="small muted" style={{ fontWeight: 600, marginTop: 4 }}>
            {t("Code owners")}
            {d.file && (
              <code title={`${d.file} @ ${d.base}`} style={{ marginLeft: 6, fontWeight: 400 }}>
                {d.file.split("/").pop()}
              </code>
            )}
          </span>
          {!d.file && (
            <span className="small muted">
              {t("This repository has no CODEOWNERS file on")} <code>{d.base}</code>.
            </span>
          )}
          {d.file && (
            <>
              <span className="small muted">
                {d.required === true
                  ? t("GitHub merges this PR only when a code owner of each changed file approves it. They are not the PR's reviewers here; ask them for the approvals.")
                  : d.required === false
                    ? t("This branch does not wait for code owners to merge, but they know these files best.")
                    : t("Each changed file wants an approval from one of its code owners; whether the branch waits for it could not be read.")}
              </span>
              {d.people.length === 0 ? (
                <span className="small muted">{t("No code owner owns the files this PR changes.")}</span>
              ) : (
                <OwnerGroups repo={repo} people={d.people} waiting={d.waiting} />
              )}
              {d.parts.length > 0 && (
                <details className="small">
                  <summary className="muted">{t("Files by code owner")}</summary>
                  <div className="stack" style={{ gap: 8, marginTop: 8 }}>
                    {d.parts.map((part) => {
                      const done = part.approved_by.length > 0;
                      return (
                        <div key={part.owners.join(" ")} className={`owner-part ${done ? "done" : ""}`}>
                          <span className="small row" style={{ gap: 6 }}>
                            <span className={done ? "check-ok" : "check-wait"}>
                              <Icon name={done ? "check" : "clock"} size={13} />
                            </span>
                            {part.owners.map((l) => (l.includes("/") ? l : (aliasOf(l) ?? l))).join(" / ")}
                            {done && <span className="muted">· {`${t("approved by")} ${part.approved_by.map((l) => aliasOf(l) ?? l).join(", ")}`}</span>}
                          </span>
                          <span className="muted">
                            {part.files.length} {part.files.length === 1 ? t("file") : t("files")} · <code>{part.patterns.join(" ")}</code>
                          </span>
                          <ul className="owner-files">
                            {part.files.map((f) => (
                              <li key={f}>
                                <code>{f}</code>
                              </li>
                            ))}
                          </ul>
                        </div>
                      );
                    })}
                  </div>
                </details>
              )}
              {d.unowned.length > 0 && (
                <span className="small muted" title={d.unowned.join("\n")}>
                  {d.unowned.length} {d.unowned.length === 1 ? t("file has no code owner.") : t("files have no code owner.")}
                </span>
              )}
            </>
          )}
          {d.requested.length > 0 && (
            <div className="small muted row" style={{ gap: 6, flexWrap: "wrap" }}>
              {t("Also asked to review on GitHub:")}
              {d.requested.map((l) => (
                <span key={l} className="row" style={{ gap: 4 }}>
                  <Avatar login={prefix + l} size={18} />
                  <Named login={l} alias={aliasOf(l)} />
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </Section>
  );
}
