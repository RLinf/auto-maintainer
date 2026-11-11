// A PR's people in the service (design 2.3): its owners, who answer for the whole PR's
// progress, and its reviewers, who review it. Anyone who may use the repository's
// dashboard claims either role or leaves it, and a PR has any number of each. The
// repository's maintainers assign both, and the PR's owners assign its reviewers, by
// any GitHub login that can see the repository, whether or not that person ever signed
// in here; assigning a reviewer can also request their review on GitHub, as the
// repository's rule for that allows. Whom GitHub asks to review on its own (its code
// owners) shows apart, in the Code owners card.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Assignable, type Assignee, type AssignResult } from "./api";
import { useAliasOf } from "./aliases";
import { Avatar, Named } from "./avatar";
import { ErrorBox, Section, useAct, When } from "./components";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { useToast } from "./ui";
import { links } from "./forges";

const KEYS = [["pr"], ["prs"], ["assignable"]];

export function useAssignable(repo: string) {
  return useQuery({ queryKey: ["assignable", repo], queryFn: () => api.get<Assignable>(`/api/repos/${repo}/assignable`), staleTime: 60_000 });
}

/** The logins in what someone typed: separated by spaces or commas, with or without "@". */
function loginsIn(text: string): string[] {
  return [...new Set(text.split(/[\s,]+/).map((x) => x.replace(/^@/, "").trim()).filter(Boolean))];
}

export type Role = "owner" | "reviewer";

/** Claim a role on the PR, or leave it. */
export function ClaimButton({ repo, number, role, assignees, me }: { repo: string; number: number; role: Role; assignees: Assignee[]; me: string }) {
  const t = useT();
  const base = `/api/prs/${repo}/${number}`;
  const claim = useAct(() => api.post(`${base}/claim`, { role }), KEYS);
  const leave = useAct(() => api.del(`${base}/claim?role=${role}`), KEYS);
  const holders = assignees.filter((a) => a.role === role);
  const mine = holders.some((a) => a.login.toLowerCase() === me.toLowerCase());
  const others = holders.filter((a) => a.login.toLowerCase() !== me.toLowerCase()).map((a) => a.login);
  return mine ? (
    <button className="sm" disabled={leave.isPending} onClick={() => leave.mutate(undefined)} title={role === "owner" ? t("Stop owning this PR; the other owners stay") : t("Stop reviewing this PR; the other reviewers stay")}>
      {t("Leave")}
    </button>
  ) : (
    <button
      className="sm"
      disabled={claim.isPending}
      onClick={() => claim.mutate(undefined)}
      title={
        others.length
          ? `${role === "owner" ? t("Own it with:") : t("Review it with:")} ${others.join(", ")}`
          : role === "owner"
            ? t("Say you answer for this PR's progress")
            : t("Say you are reviewing this PR")
      }
    >
      {t("Claim")}
    </button>
  );
}

/** A PR's owners and reviewers here (design 2.3): owners answer for the whole PR's
 * progress, reviewers review it; anyone claims either, maintainers assign both, and
 * the PR's owners assign its reviewers. */
export function PrPeopleCards({ repo, number, author, assignees, me }: { repo: string; number: number; author: string; assignees: Assignee[]; me: string }) {
  const t = useT();
  const who = useAssignable(repo);
  const iOwn = assignees.some((a) => a.role === "owner" && a.login.toLowerCase() === me.toLowerCase());
  const maintainer = !!who.data?.can_assign;
  return (
    <Section title={t("People")}>
      <div className="stack" style={{ gap: 10 }}>
        <RoleGroup role="owner" repo={repo} number={number} author={author} assignees={assignees} me={me} canAssign={maintainer} />
        <RoleGroup role="reviewer" repo={repo} number={number} author={author} assignees={assignees} me={me} canAssign={maintainer || iOwn} />
      </div>
    </Section>
  );
}

function RoleGroup({
  role,
  repo,
  number,
  author,
  assignees,
  me,
  canAssign,
}: {
  role: Role;
  repo: string;
  number: number;
  author: string;
  assignees: Assignee[];
  me: string;
  canAssign: boolean;
}) {
  const t = useT();
  const toast = useToast();
  const who = useAssignable(repo);
  const base = `/api/prs/${repo}/${number}`;
  const remove = useAct((login: string) => api.del(`${base}/assignees/${encodeURIComponent(login)}?role=${role}`), KEYS);
  const assign = useAct((body: { logins: string[]; role: Role; request_review: boolean }) => api.post<AssignResult>(`${base}/assignees`, body), KEYS);
  const [assigning, setAssigning] = useState(false);
  const [text, setText] = useState("");
  const [ask, setAsk] = useState(true);
  const holders = assignees.filter((a) => a.role === role);
  const on = new Set(holders.map((a) => a.login.toLowerCase()));
  const suggestions = (who.data?.people ?? []).filter((p) => !on.has(p.login.toLowerCase()) && p.login.toLowerCase() !== author.toLowerCase());
  const logins = loginsIn(text);
  const owner = role === "owner";
  const aliasOf = useAliasOf();

  const submit = () =>
    assign.mutate(
      { logins, role, request_review: !owner && ask },
      {
        onSuccess: (r) => {
          setText("");
          setAssigning(false);
          const q = r.requested;
          toast(
            !q
              ? { title: t("Assigned"), icon: "check" }
              : q.status === "executed" || q.status === "queued" || q.status === "intent"
                ? { title: t("Assigned, and their review requested on GitHub"), icon: "check" }
                : q.status === "proposed"
                  ? { title: t("Assigned"), body: t("Requesting their review on GitHub waits for approval in the inbox."), icon: "inbox" }
                  : q.status === "shadowed"
                    ? { title: t("Assigned here only"), body: t("This instance only records what it would do, so GitHub was not asked."), icon: "eye" }
                    : { title: t("Assigned here only"), body: q.error || q.reason || q.status, icon: "x" },
          );
        },
      },
    );

  return (
    <div className="pr-role-group">
      <div className="pr-role-head">
        <span className="pr-role-title" title={owner ? t("answer for the PR's progress") : t("review it")}>
          {owner ? t("Owners") : t("Reviewers")}
        </span>
        <span className="row right" style={{ gap: 4 }}>
          {canAssign && !assigning && (
            <button
              type="button"
              className="ghost sm"
              aria-expanded={false}
              title={owner ? t("GitHub logins to make owners") : t("GitHub logins to assign")}
              onClick={() => setAssigning(true)}
            >
              <Icon name="plus" size={13} />
              {t("Assign")}
            </button>
          )}
          <ClaimButton repo={repo} number={number} role={role} assignees={assignees} me={me} />
        </span>
      </div>
      {holders.length === 0 && <span className="small muted">{owner ? t("Nobody owns this PR yet.") : t("Nobody reviews this PR yet.")}</span>}
      {holders.map((a) => (
        <div key={a.login} className="assignee">
          <Avatar login={a.login} size={24} />
          <span className="main">
            <a href={links.user(a.login)} target="_blank" rel="noreferrer">
              <Named login={a.login} alias={aliasOf(a.login)} />
            </a>
            {a.login.toLowerCase() === me.toLowerCase() && <span className="muted"> ({t("you")})</span>}
            <span className="small muted" style={{ display: "block" }}>
              {a.kind === "assigned" ? `${t("assigned by")} ${aliasOf(a.by) ?? a.by}` : a.kind === "forge" ? `${t("asked on")} ${a.by}` : t("claimed")} · <When at={a.at} />
            </span>
          </span>
          {(canAssign || a.login.toLowerCase() === me.toLowerCase()) && (
            <button
              className="ghost sm icon"
              aria-label={`${t("Take off")} ${a.login}`}
              title={owner ? t("Take them off as an owner") : t("Take them off this PR here; a review requested on GitHub stays requested")}
              disabled={remove.isPending}
              onClick={() => remove.mutate(a.login)}
            >
              <Icon name="x" size={13} />
            </button>
          )}
        </div>
      ))}
      {canAssign && assigning && (
        <form
          className="stack"
          style={{ gap: 6 }}
          onSubmit={(e) => {
            e.preventDefault();
            if (logins.length) submit();
          }}
        >
          <input
            autoFocus
            list={`assignable-${repo}-${role}`}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={t("GitHub login, or several")}
            aria-label={owner ? t("GitHub logins to make owners") : t("GitHub logins to assign")}
            style={{ width: "100%", minWidth: 0 }}
          />
          <datalist id={`assignable-${repo}-${role}`}>
            {suggestions.map((p) => (
              <option key={p.login} value={p.login}>
                {[aliasOf(p.login), p.role, p.areas.join(", ")].filter(Boolean).join(" · ")}
              </option>
            ))}
          </datalist>
          {suggestions.length > 0 && !text && (
            <div className="row" style={{ gap: 4, flexWrap: "wrap" }}>
              {suggestions.slice(0, 8).map((p) => (
                <button key={p.login} type="button" className="chip-person" title={[p.role, p.areas.join(", ")].filter(Boolean).join(" · ") || undefined} onClick={() => setText(p.login)}>
                  <Avatar login={p.login} size={16} />
                  <Named login={p.login} alias={aliasOf(p.login)} />
                </button>
              ))}
            </div>
          )}
          {!owner && (
            <label className="small row" style={{ gap: 6 }}>
              <input type="checkbox" checked={ask} onChange={(e) => setAsk(e.target.checked)} />
              {t("Also request their review on GitHub")}
            </label>
          )}
          <div className="row" style={{ gap: 6 }}>
            <button className="primary sm" type="submit" disabled={!logins.length || assign.isPending}>
              <Icon name="plus" size={13} />
              {assign.isPending ? t("Assigning…") : t("Assign")}
            </button>
            <button
              type="button"
              className="ghost sm"
              onClick={() => {
                setAssigning(false);
                setText("");
              }}
            >
              {t("Cancel")}
            </button>
          </div>
        </form>
      )}
      <ErrorBox error={assign.error || remove.error} />
    </div>
  );
}
