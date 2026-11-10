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
import { api, type CodeOwnerStanding, type CodeOwners } from "./api";
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

/** Who approved, who the merge still waits on, and who is not needed. */
export function OwnerGroups({ repo, people, waiting }: { repo: string; people: CodeOwnerStanding[]; waiting: string[][] }) {
  const t = useT();
  const aliasOf = useAliasOf();
  const prefix = repo.includes(":") ? repo.slice(0, repo.indexOf(":") + 1) : "";
  const byLogin = new Map(people.map((p) => [p.login.toLowerCase(), p]));
  const chip = (login: string, quiet = false) => {
    const o = byLogin.get(login.toLowerCase()) ?? unknownOwner(login);
    const url = ownerUrl(repo, o);
    const s = standing(o, t);
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
  const summary = useCodeOwnerSummary()(d?.people ?? [], d?.waiting ?? [], d?.required ?? null);
  return (
    <Section
      title={t("Code owners")}
      hint={d?.file ? <code title={`${d.file} @ ${d.base}`}>{d.file.split("/").pop()}</code> : undefined}
      aside={summary ? <span className={`pill ${summary.tone}`}>{summary.text}</span> : undefined}
    >
      {q.isLoading ? (
        <Skeleton rows={2} />
      ) : !d ? (
        <ErrorBox error={q.error} />
      ) : (
        <div className="stack" style={{ gap: 10 }}>
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
