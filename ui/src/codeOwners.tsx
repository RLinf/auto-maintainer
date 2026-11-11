// A PR's approvals as GitHub counts them, and its code owners: whom the PR's reviewers
// ask for the approvals a merge waits on. Code owners stay apart from the PR's owners on
// this dashboard (the People card), because GitHub asks its code owners to review
// nearly every PR, and being asked makes nobody responsible for one here.
//
// Built for the PR page's narrow sidebar as much as for the wide Review progress tab:
// one column, names that wrap as text, and the count of approvals as a bar. Each
// CODEOWNERS group the PR touches is one line: the owners any one of whom approves it,
// how many files it holds, and who approved it.

import { useQuery } from "@tanstack/react-query";
import { api, type ApprovalStatus, type CodeOwnerStanding, type CodeOwners } from "./api";
import { useAliasOf } from "./aliases";
import { Avatar } from "./avatar";
import { ErrorBox, Section } from "./components";
import { useMe } from "./data";
import { forgeOf, links } from "./forges";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { Skeleton } from "./ui";

type Part = CodeOwners["parts"][number];

/** The page of a person or team that CODEOWNERS names, on the repository's forge. */
function ownerUrl(repo: string, login: string, team: boolean): string | null {
  const prefix = repo.includes(":") ? repo.slice(0, repo.indexOf(":") + 1) : "";
  if (login.includes("@")) return null;
  if (!team) return links.user(prefix + login);
  const forge = forgeOf(repo);
  const web = forge.web_url.replace(/\/+$/, "");
  const [org, ...rest] = login.split("/");
  return forge.kind === "github" ? `${web}/orgs/${org}/teams/${rest.join("/")}` : `${web}/${login}`;
}

/** The verdict to show beside an owner's name, when it says more than the line does. */
function verdictNote(o: CodeOwnerStanding | undefined, t: (s: string) => string): { text: string; tone: string } | null {
  if (!o) return null;
  if (o.verdict === "CHANGES_REQUESTED") return { text: t("changes requested"), tone: "danger-text" };
  if (o.verdict === "APPROVED" && !o.at_head) return { text: t("approved an earlier commit"), tone: "muted" };
  if (o.verdict === "DISMISSED") return { text: t("review dismissed"), tone: "muted" };
  return null;
}

/** A short verdict for a card's header: GitHub's, or the count of approvals. */
export function useApprovalBadge() {
  const t = useT();
  return (a: ApprovalStatus | undefined, waiting: string[][]): { text: string; tone: string } | null => {
    if (!a) return null;
    if (a.changes_requested_by.length) return { text: t("Changes requested"), tone: "status-bad" };
    if (a.decision === "APPROVED") return { text: t("Approved"), tone: "status-ok" };
    if (a.missing) return { text: t("Not approved yet"), tone: "status-warn" };
    if (waiting.length && a.code_owners_required !== false) return { text: t("Code owner needed"), tone: "status-warn" };
    if (a.decision === "REVIEW_REQUIRED") return { text: t("Review required"), tone: "status-warn" };
    return a.approved_by.length ? { text: t("Approved"), tone: "status-ok" } : null;
  };
}

/** One person, or a team, by name with an avatar: their own name links to their page. */
function Person({ repo, login, me, people }: { repo: string; login: string; me?: string; people?: Map<string, CodeOwnerStanding> }) {
  const t = useT();
  const aliasOf = useAliasOf();
  const prefix = repo.includes(":") ? repo.slice(0, repo.indexOf(":") + 1) : "";
  const team = login.includes("/");
  const url = ownerUrl(repo, login, team);
  const name = team ? login : (aliasOf(login) ?? login);
  const note = verdictNote(people?.get(login.toLowerCase()), t);
  const you = !!me && login.toLowerCase() === me.toLowerCase();
  const body = (
    <>
      {team ? <Icon name="list" size={13} /> : <Avatar login={prefix + login} size={16} />}
      <span title={team ? t("A team: any member's approval counts") : name !== login ? login : undefined}>{name}</span>
    </>
  );
  return (
    <span className="appr-person">
      {url ? (
        <a href={url} target="_blank" rel="noreferrer">
          {body}
        </a>
      ) : (
        body
      )}
      {you && <span className="muted"> ({t("you")})</span>}
      {note && <span className={`small ${note.tone}`}> · {note.text}</span>}
    </span>
  );
}

/** Several people any one of whom will do: "A, B or C". */
function AnyOf({ repo, logins, me, people }: { repo: string; logins: string[]; me?: string; people?: Map<string, CodeOwnerStanding> }) {
  const t = useT();
  return (
    <span className="appr-names">
      {logins.map((l, i) => (
        <span key={l}>
          {i > 0 && <span className="muted">{i === logins.length - 1 ? ` ${t("or")} ` : ", "}</span>}
          <Person repo={repo} login={l} me={me} people={people} />
        </span>
      ))}
    </span>
  );
}

/** The approvals the base branch requires, against those the PR has: a bar of the
 * count, who approved, who does not count and why, and GitHub's own verdict. */
export function ApprovalsBlock({ a, repo }: { a: ApprovalStatus; repo: string }) {
  const t = useT();
  const me = useMe().data?.login;
  const count = a.approved_by.length;
  const required = a.required;
  const decision = a.decision ? { APPROVED: t("approved"), CHANGES_REQUESTED: t("changes requested"), REVIEW_REQUIRED: t("review required") }[a.decision] : undefined;
  return (
    <div className="appr-block">
      <div className="appr-head" title={t("Approving reviews from people with write access, which the branch requires apart from the code owners")}>
        <span className="appr-title">
          {t("Approving reviews")}
          {required != null && (
            <strong className="appr-count">
              {" "}
              {count}/{required}
            </strong>
          )}
        </span>
        {decision && (
          <span className="small muted" title={t("GitHub's own verdict on this branch's review rules")}>
            GitHub: {decision}
          </span>
        )}
      </div>
      {required != null && required > 0 && (
        <span className="appr-bar" aria-hidden>
          {Array.from({ length: Math.max(required, count) }, (_, i) => (
            <span key={i} className={i < count ? "on" : ""} />
          ))}
        </span>
      )}
      {count > 0 && (
        <div className="appr-line">
          <Icon name="check" size={13} />
          <AnyOf repo={repo} logins={a.approved_by} me={me} />
        </div>
      )}
      {a.missing ? (
        <span className="small muted">
          {a.missing === 1 ? t("Needs 1 more approval from anyone with write access.") : `${t("Needs")} ${a.missing} ${t("more approvals from anyone with write access.")}`}
        </span>
      ) : null}
      {a.required == null && <span className="small muted">{t("The approvals this base branch requires could not be read; GitHub's verdict above still applies.")}</span>}
      {a.changes_requested_by.length > 0 && (
        <div className="appr-line danger-text">
          <Icon name="x" size={13} />
          <span>
            {t("Changes requested by")} <AnyOf repo={repo} logins={a.changes_requested_by} me={me} />.{" "}
            <span className="muted">{t("They approve, or their review is dismissed, before GitHub merges.")}</span>
          </span>
        </div>
      )}
      {a.not_counted.map((n) => (
        <div key={n.login} className="appr-line small muted">
          <Icon name="question" size={13} />
          <span>
            <Person repo={repo} login={n.login} me={me} /> {t("does not count")}: {t(n.reason)}
          </span>
        </div>
      ))}
      {a.last_push_approval && <span className="small muted">{t("This branch counts only approvals of the latest push.")}</span>}
    </div>
  );
}

/** The code owners' lines: for each group of files, the owners any one of whom
 * approves it, and whether one did. ``parts`` (the PR page's card) adds how many files
 * each group holds and lists them; without it (the Review progress tab), the lines
 * come from the approvals still missing. */
export function OwnerGroups({ repo, people, waiting, parts }: { repo: string; people: CodeOwnerStanding[]; waiting: string[][]; parts?: Part[] }) {
  const t = useT();
  const aliasOf = useAliasOf();
  const me = useMe().data?.login;
  const byLogin = new Map(people.map((p) => [p.login.toLowerCase(), p]));
  const author = people.find((p) => p.author)?.login;
  type Line = { owners: string[]; approvedBy: string[]; files?: string[]; patterns?: string[] };
  const lines: Line[] = parts
    ? parts.map((p) => ({ owners: p.owners, approvedBy: p.approved_by, files: p.files, patterns: p.patterns }))
    : [
        ...waiting.map((g) => ({ owners: g, approvedBy: [] })),
        ...(people.some((p) => p.need === "approved") ? [{ owners: people.filter((p) => p.need === "approved").map((p) => p.login), approvedBy: people.filter((p) => p.need === "approved").map((p) => p.login) }] : []),
      ];
  lines.sort((x, y) => Number(x.approvedBy.length > 0) - Number(y.approvedBy.length > 0));
  const open = lines.filter((l) => !l.approvedBy.length);
  // Someone in every group still waiting covers them all with one approval.
  const groups = open.map((l) => l.owners.filter((o) => o.toLowerCase() !== author?.toLowerCase()).map((o) => o.toLowerCase()));
  const everywhere = open.length > 1 && groups.every((g) => g.length) ? groups.reduce((acc, g) => acc.filter((o) => g.includes(o))) : [];
  const covering = everywhere.map((o) => open.flatMap((l) => l.owners).find((x) => x.toLowerCase() === o) ?? o);
  return (
    <div className="appr-owners">
      {lines.map((l) => {
        const done = l.approvedBy.length > 0;
        const others = l.owners.filter((o) => o.toLowerCase() !== author?.toLowerCase());
        return (
          <div key={l.owners.join(" ")} className={`appr-need ${done ? "done" : ""}`}>
            <span className={done ? "check-ok" : "check-wait"}>
              <Icon name={done ? "check" : "clock"} size={13} />
            </span>
            <span className="appr-need-main">
              {others.length ? (
                <AnyOf repo={repo} logins={others} me={me} people={byLogin} />
              ) : (
                <span className="small">{t("someone other than the author, who alone owns these files")}</span>
              )}
              {done && (
                <span className="small muted">
                  {" "}
                  · {t("approved by")} {l.approvedBy.map((x) => aliasOf(x) ?? x).join(", ")}
                </span>
              )}
              {l.files && l.files.length > 0 && (
                <details className="appr-files">
                  <summary className="small muted">
                    {l.files.length} {l.files.length === 1 ? t("file") : t("files")}
                  </summary>
                  {l.patterns && <code className="small">{l.patterns.join(" ")}</code>}
                  <ul className="owner-files">
                    {l.files.map((f) => (
                      <li key={f}>
                        <code>{f}</code>
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </span>
          </div>
        );
      })}
      {covering.length > 0 && (
        <span className="small appr-hint">
          {me && covering.some((c) => c.toLowerCase() === me.toLowerCase())
            ? t("Your approval covers every group still waiting.")
            : `${t("One approval from")} ${covering.map((c) => aliasOf(c) ?? c).join(` ${t("or")} `)} ${t("covers every group still waiting.")}`}
        </span>
      )}
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
  const badge = useApprovalBadge()(d?.approvals, d?.waiting ?? []);
  return (
    <Section title={t("Approvals")} aside={badge ? <span className={`pill ${badge.tone}`}>{badge.text}</span> : undefined}>
      {q.isLoading ? (
        <Skeleton rows={2} />
      ) : !d ? (
        <ErrorBox error={q.error} />
      ) : (
        <div className="stack" style={{ gap: 14 }}>
          {d.approvals && <ApprovalsBlock a={d.approvals} repo={repo} />}
          <CodeOwnersPart d={d} repo={repo} />
        </div>
      )}
    </Section>
  );
}

function CodeOwnersPart({ d, repo }: { d: CodeOwners; repo: string }) {
  const t = useT();
  const aliasOf = useAliasOf();
  const why =
    d.required === true
      ? t("GitHub merges this PR only when one code owner of each group of files approves it. They are not the PR's reviewers here; ask them for the approvals.")
      : d.required === false
        ? t("This branch does not wait for code owners to merge, but they know these files best.")
        : t("Each changed file wants an approval from one of its code owners; whether the branch waits for it could not be read.");
  return (
    <div className="appr-block">
      <div className="appr-head">
        <span className="appr-title" title={d.file ? why : undefined}>
          {t("Code owners")}
          {d.required === false && <span className="small muted"> · {t("not required")}</span>}
        </span>
        {d.file && (
          <code className="small" title={`${d.file} @ ${d.base}`}>
            {d.file.split("/").pop()}
          </code>
        )}
      </div>
      {!d.file ? (
        <span className="small muted">
          {t("This repository has no CODEOWNERS file on")} <code>{d.base}</code>.
        </span>
      ) : d.parts.length === 0 ? (
        <span className="small muted">{t("No code owner owns the files this PR changes.")}</span>
      ) : (
        <OwnerGroups repo={repo} people={d.people} waiting={d.waiting} parts={d.parts} />
      )}
      {(d.unowned.length > 0 || d.requested.length > 0) && (
        <span className="small muted">
          {d.unowned.length > 0 && (
            <span title={d.unowned.join("\n")}>{d.unowned.length} {d.unowned.length === 1 ? t("file has no code owner.") : t("files have no code owner.")}</span>
          )}
          {d.unowned.length > 0 && d.requested.length > 0 && " "}
          {d.requested.length > 0 && (
            <span>
              {t("Also asked to review on GitHub:")} {d.requested.map((l) => aliasOf(l) ?? l).join(", ")}
            </span>
          )}
        </span>
      )}
    </div>
  );
}
