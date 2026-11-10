// The code owners of a PR's changed files, from CODEOWNERS on its base branch: whom the
// PR's reviewers ask for the approvals a merge waits on. They stay apart from the PR's
// owners on this dashboard (the Reviewers card), because GitHub asks its code owners to
// review nearly every PR, and being asked makes nobody responsible for one here.

import { useQuery } from "@tanstack/react-query";
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

function standing(o: CodeOwnerStanding, t: (s: string) => string): { text: string; tone: string } | null {
  if (o.author) return { text: t("author"), tone: "status-muted" };
  if (o.verdict === "APPROVED") return o.at_head ? { text: t("approved"), tone: "status-ok" } : { text: t("approved an earlier commit"), tone: "status-ok" };
  if (o.verdict === "CHANGES_REQUESTED") return { text: t("changes requested"), tone: "status-bad" };
  if (o.verdict === "DISMISSED") return { text: t("review dismissed"), tone: "status-muted" };
  if (o.requested) return { text: t("review requested"), tone: "status-info" };
  return null;
}

export function CodeOwnersCard({ repo, number, head }: { repo: string; number: number; head: string }) {
  const t = useT();
  const q = useQuery({
    queryKey: ["pr", repo, String(number), "code-owners", head],
    queryFn: () => api.get<CodeOwners>(`/api/prs/${repo}/${number}/code-owners`),
    staleTime: 60_000,
  });
  const d = q.data;
  const people = new Map((d?.people ?? []).map((p) => [p.login.toLowerCase(), p]));
  const approved = (d?.parts ?? []).filter((p) => p.approved_by.length > 0).length;
  const total = d?.parts.length ?? 0;
  const prefix = repo.includes(":") ? repo.slice(0, repo.indexOf(":") + 1) : "";
  const aliasOf = useAliasOf();
  return (
    <Section
      title={t("Code owners")}
      hint={d?.file ? <code title={`${d.file} @ ${d.base}`}>{d.file.split("/").pop()}</code> : undefined}
      aside={
        total > 0 ? (
          <span
            className={`pill ${approved === total ? "status-ok" : d?.required ? "status-warn" : "status-muted"}`}
            title={t("Parts of the change with an approval from one of their code owners")}
          >
            {approved}/{total} {t("approved")}
          </span>
        ) : undefined
      }
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
              ? t("GitHub merges this PR only when each part below has an approval from one of its code owners. They are not the PR's reviewers here; ask them for the approvals.")
              : d.required === false
                ? t("This branch does not wait for code owners to merge, but they know these files best.")
                : t("Each part below wants an approval from one of its code owners; whether the branch waits for it could not be read.")}
          </span>
          {total === 0 && <span className="small muted">{t("No code owner owns the files this PR changes.")}</span>}
          {d.parts.map((part) => {
            const done = part.approved_by.length > 0;
            return (
              <div key={part.owners.join(" ")} className={`owner-part ${done ? "done" : ""}`}>
                <span className="small row" style={{ gap: 6 }}>
                  <span className={done ? "check-ok" : "check-wait"}>
                    <Icon name={done ? "check" : "clock"} size={13} />
                  </span>
                  {done ? `${t("Approved by")} ${part.approved_by.map((l) => aliasOf(l) ?? l).join(", ")}` : part.owners.length > 1 ? t("Needs an approval from one of:") : t("Needs an approval from:")}
                </span>
                <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                  {part.owners.map((login) => {
                    const o = people.get(login.toLowerCase()) ?? { login, team: login.includes("/"), author: false, verdict: null, at_head: false, requested: false, files: 0 };
                    const url = ownerUrl(repo, o);
                    const s = standing(o, t);
                    const name = (
                      <>
                        {o.team ? <Icon name="list" size={13} /> : <Avatar login={prefix + o.login} size={18} />}
                        <Named login={o.login} alias={o.team ? undefined : aliasOf(o.login)} />
                      </>
                    );
                    return (
                      <span key={login} className="owner-chip" title={o.team ? t("A team: any member's approval counts") : undefined}>
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
                  })}
                </div>
                <details className="small">
                  <summary className="muted">
                    {part.files.length} {part.files.length === 1 ? t("file") : t("files")} · <code>{part.patterns.join(" ")}</code>
                  </summary>
                  <ul className="owner-files">
                    {part.files.map((f) => (
                      <li key={f}>
                        <code>{f}</code>
                      </li>
                    ))}
                  </ul>
                </details>
              </div>
            );
          })}
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
