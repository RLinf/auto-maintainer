// The reviewers working on a PR in the service (design 2.3). Anyone who may use the
// repository's dashboard claims a PR or leaves it, and a PR has any number of them. The
// repository's maintainers assign others, by any GitHub login that can see the
// repository, whether or not that person ever signed in here; assigning can also
// request their review on GitHub, as the repository's rule for that allows.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Assignable, type Assignee, type AssignResult } from "./api";
import { Avatar } from "./avatar";
import { ErrorBox, Section, useAct, When } from "./components";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { useToast } from "./ui";

const KEYS = [["pr"], ["prs"], ["assignable"]];

export function useAssignable(repo: string) {
  return useQuery({ queryKey: ["assignable", repo], queryFn: () => api.get<Assignable>(`/api/repos/${repo}/assignable`), staleTime: 60_000 });
}

/** The logins in what someone typed: separated by spaces or commas, with or without "@". */
function loginsIn(text: string): string[] {
  return [...new Set(text.split(/[\s,]+/).map((x) => x.replace(/^@/, "").trim()).filter(Boolean))];
}

export function ClaimButton({ repo, number, assignees, me }: { repo: string; number: number; assignees: Assignee[]; me: string }) {
  const t = useT();
  const base = `/api/prs/${repo}/${number}`;
  const claim = useAct(() => api.post(`${base}/claim`), KEYS);
  const leave = useAct(() => api.del(`${base}/claim`), KEYS);
  const mine = assignees.some((a) => a.login.toLowerCase() === me.toLowerCase());
  const others = assignees.filter((a) => a.login.toLowerCase() !== me.toLowerCase()).map((a) => a.login);
  return mine ? (
    <button disabled={leave.isPending} onClick={() => leave.mutate(undefined)} title={t("Stop working on this PR; the others stay on it")}>
      {t("Leave")}
    </button>
  ) : (
    <button
      disabled={claim.isPending}
      onClick={() => claim.mutate(undefined)}
      title={others.length ? `${t("Join the reviewers on it:")} ${others.join(", ")}` : t("Say you are reviewing this PR")}
    >
      {t("Claim")}
    </button>
  );
}

export function ReviewersCard({
  repo,
  number,
  author,
  assignees,
  requested,
  me,
}: {
  repo: string;
  number: number;
  author: string;
  assignees: Assignee[];
  requested: string[];
  me: string;
}) {
  const t = useT();
  const toast = useToast();
  const who = useAssignable(repo);
  const base = `/api/prs/${repo}/${number}`;
  const remove = useAct((login: string) => api.del(`${base}/assignees/${encodeURIComponent(login)}`), KEYS);
  const assign = useAct((body: { logins: string[]; request_review: boolean }) => api.post<AssignResult>(`${base}/assignees`, body), KEYS);
  const [text, setText] = useState("");
  const [ask, setAsk] = useState(true);
  const canAssign = !!who.data?.can_assign;
  const on = new Set(assignees.map((a) => a.login.toLowerCase()));
  const onGitHub = requested.filter((l) => !on.has(l.toLowerCase()));
  const suggestions = (who.data?.people ?? []).filter((p) => !on.has(p.login.toLowerCase()) && p.login.toLowerCase() !== author.toLowerCase());
  const logins = loginsIn(text);

  const submit = () =>
    assign.mutate(
      { logins, request_review: ask },
      {
        onSuccess: (r) => {
          setText("");
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
    <Section title={t("Reviewers")} aside={<ClaimButton repo={repo} number={number} assignees={assignees} me={me} />}>
      <div className="stack" style={{ gap: 8 }}>
        {assignees.length === 0 && <span className="small muted">{t("Nobody has claimed this PR or been assigned to it.")}</span>}
        {assignees.map((a) => (
          <div key={a.login} className="assignee">
            <Avatar login={a.login} size={28} />
            <span className="main">
              <a href={`https://github.com/${encodeURIComponent(a.login)}`} target="_blank" rel="noreferrer">
                {a.login}
              </a>
              {a.login.toLowerCase() === me.toLowerCase() && <span className="muted"> ({t("you")})</span>}
              <span className="small muted" style={{ display: "block" }}>
                {a.kind === "assigned" ? `${t("assigned by")} ${a.by}` : t("claimed")} · <When at={a.at} />
              </span>
            </span>
            {(canAssign || a.login.toLowerCase() === me.toLowerCase()) && (
              <button
                className="ghost sm icon"
                aria-label={`${t("Take off")} ${a.login}`}
                title={t("Take them off this PR here; a review requested on GitHub stays requested")}
                disabled={remove.isPending}
                onClick={() => remove.mutate(a.login)}
              >
                <Icon name="x" size={13} />
              </button>
            )}
          </div>
        ))}
        {onGitHub.length > 0 && (
          <div className="small muted row" style={{ gap: 6, flexWrap: "wrap" }}>
            {t("Review requested on GitHub:")}
            {onGitHub.map((l) => (
              <span key={l} className="row" style={{ gap: 4 }}>
                <Avatar login={l} size={18} />
                {l}
              </span>
            ))}
          </div>
        )}
        {canAssign && (
          <form
            className="stack"
            style={{ gap: 6 }}
            onSubmit={(e) => {
              e.preventDefault();
              if (logins.length) submit();
            }}
          >
            <div className="row" style={{ gap: 6 }}>
              <input
                list={`assignable-${repo}`}
                value={text}
                onChange={(e) => setText(e.target.value)}
                placeholder={t("GitHub login, or several")}
                aria-label={t("GitHub logins to assign")}
                style={{ flex: 1, minWidth: 0 }}
              />
              <button className="sm" type="submit" disabled={!logins.length || assign.isPending}>
                <Icon name="plus" size={13} />
                {assign.isPending ? t("Assigning…") : t("Assign")}
              </button>
            </div>
            <datalist id={`assignable-${repo}`}>
              {suggestions.map((p) => (
                <option key={p.login} value={p.login}>
                  {[p.role, p.areas.join(", ")].filter(Boolean).join(" · ")}
                </option>
              ))}
            </datalist>
            {suggestions.length > 0 && !text && (
              <div className="row" style={{ gap: 4, flexWrap: "wrap" }}>
                {suggestions.slice(0, 8).map((p) => (
                  <button key={p.login} type="button" className="chip-person" title={[p.role, p.areas.join(", ")].filter(Boolean).join(" · ") || undefined} onClick={() => setText(p.login)}>
                    <Avatar login={p.login} size={16} />
                    {p.login}
                  </button>
                ))}
              </div>
            )}
            <label className="small row" style={{ gap: 6 }}>
              <input type="checkbox" checked={ask} onChange={(e) => setAsk(e.target.checked)} />
              {t("Also request their review on GitHub")}
            </label>
          </form>
        )}
        <ErrorBox error={assign.error || remove.error} />
      </div>
    </Section>
  );
}
