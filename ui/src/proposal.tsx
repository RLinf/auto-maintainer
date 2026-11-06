// A proposal as the maintainer decides it: exactly what will be posted, editable before
// approval. Used by the inbox, the home feed and the agent panel.

import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type ProposalWithAction } from "./api";
import { AgentPicker } from "./agentPicker";
import { Diff, ErrorBox, Pre, prPath, RejectButton, Status, useAct, When } from "./components";
import { useMe } from "./data";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { Popover, useToast } from "./ui";

/** Kinds a maintainer may approve as themselves, with their own GitHub account. */
const AS_ME = new Set(["review.post", "pr.fix"]);

function readAsMe(): boolean {
  try {
    return localStorage.getItem("am.post-as-me") !== "0";
  } catch {
    return true;
  }
}

export const PROPOSAL_KEYS = [["proposals"], ["pr"], ["prs"], ["repos"], ["audit"]];

interface InlineComment {
  path: string;
  line: number;
  start_line?: number | null;
  side?: string;
  body: string;
}

const KIND_LABELS: Record<string, string> = {
  "label.apply": "Apply labels",
  "label.ci_gate": "Apply the CI gate label",
  "review.post": "Post a review",
  "review.update": "Update a review",
  "thread.reply": "Reply in a thread",
  "comment.upsert": "Post a comment",
  "ci.rerun_failed": "Rerun failed CI",
  "pr.request_reviewers": "Request reviewers",
  "pr.update_branch": "Update the branch",
  "pr.merge": "Merge",
  "pr.nudge": "Nudge the author",
  "pr.fix": "Open a fix PR",
  "pr.convert_to_draft": "Convert to draft",
  "pr.close": "Close the PR",
  "issue.label": "Label an issue",
  "issue.comment": "Comment on an issue",
  "lab.run": "Run a lab test",
  "memory.commit": "Commit to memory",
  "memory.change": "Change memory",
  "code.pr": "Open a service PR",
  "code.merge": "Merge a service PR",
  "service.deploy": "Deploy the service",
  notify: "Send a notification",
};

export function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}

export function ProposalCard({
  proposal,
  compact = false,
  selected,
  onSelect,
}: {
  proposal: ProposalWithAction;
  compact?: boolean;
  selected?: boolean;
  onSelect?: () => void;
}) {
  const t = useT();
  const me = useMe().data;
  const record = proposal.action;
  const action = record?.action;
  const payload = (action?.payload ?? {}) as Record<string, unknown>;
  const [editing, setEditing] = useState(false);
  const [expanded, setExpanded] = useState(!compact);
  const [body, setBody] = useState(typeof payload.body === "string" ? payload.body : "");
  const [comments, setComments] = useState<InlineComment[]>(
    Array.isArray(payload.comments) ? (payload.comments as InlineComment[]) : [],
  );
  const [title, setTitle] = useState(typeof payload.title === "string" ? payload.title : "");
  const [asMe, setAsMe] = useState(readAsMe);
  const kind = action?.kind ?? "";
  const isFix = kind === "pr.fix";
  const canAsMe = !!me?.can_post_as_me && AS_ME.has(kind);
  // A fix is pushed only by a person; the service's own account never pushes to a repository.
  const postAsMe = isFix || (canAsMe && asMe);

  const approve = useAct(() => {
    const edited: Record<string, unknown> | undefined = editing
      ? { ...payload, body, ...(comments.length ? { comments } : {}), ...(isFix ? { title } : {}) }
      : undefined;
    return api.post(`/api/proposals/${proposal.id}/approve`, { ...(edited ? { payload: edited } : {}), ...(postAsMe ? { as_me: true } : {}) });
  }, PROPOSAL_KEYS);
  const reject = useAct((reason: string) => api.post(`/api/proposals/${proposal.id}/reject`, { reason }), PROPOSAL_KEYS);

  if (!action) return <div className="proposal muted">Action {proposal.action_id} is missing.</div>;
  const hasBody = typeof payload.body === "string";
  const done = proposal.state !== "pending" || approve.isSuccess || reject.isSuccess;
  const labels = Array.isArray(payload.labels) ? (payload.labels as string[]) : null;
  const files = isFix && Array.isArray(payload.files) ? (payload.files as string[]) : [];
  const approveLabel = isFix
    ? t("Open the PR as me")
    : kind === "review.post" && postAsMe
      ? editing
        ? t("Post edited as me")
        : t("Post as me")
      : editing
        ? t("Approve edited")
        : t("Approve");

  return (
    <div className="proposal">
      <div className="row">
        {onSelect && !done && (
          <input type="checkbox" aria-label={`Select ${kindLabel(action.kind)}`} checked={!!selected} onChange={onSelect} />
        )}
        <strong>{t(kindLabel(action.kind))}</strong>
        {action.number != null && (
          <Link to={prPath(action.repo, action.number)} className="small">
            {action.repo}#{action.number}
          </Link>
        )}
        {action.number == null && <span className="small muted">{action.repo}</span>}
        {action.head_sha && <span className="mono muted">at {action.head_sha.slice(0, 7)}</span>}
        <Status value={approve.isSuccess ? "approved" : reject.isSuccess ? "rejected" : proposal.state} />
        <span className="muted small right">
          {proposal.requested_by ? `${t("draft for")} ${proposal.requested_by} · ` : ""}
          <When at={proposal.created_at} />
        </span>
      </div>
      {action.rationale && <div className="small muted">{action.rationale}</div>}
      {labels && (
        <div className="labels">
          {labels.map((l) => (
            <span key={l} className="label">
              {l}
            </span>
          ))}
        </div>
      )}
      {proposal.warnings.length > 0 && (
        <ul className="small" style={{ margin: 0, paddingLeft: 18, color: "var(--warn)" }}>
          {proposal.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
      {isFix && (
        <div className="small muted">
          {files.length} {t("files changed")}
          {typeof payload.runtime === "string" && payload.runtime ? ` · ${payload.runtime}` : ""} · {t("into the PR's branch")}
        </div>
      )}
      {compact && (hasBody || comments.length > 0) && (
        <button className="ghost sm" style={{ alignSelf: "flex-start" }} onClick={() => setExpanded(!expanded)}>
          <Icon name={expanded ? "chevronDown" : "chevronRight"} size={14} />
          {expanded ? t("Hide what will be posted") : t("Show what will be posted")}
        </button>
      )}
      {expanded && (
        <>
          {isFix &&
            (editing ? (
              <input value={title} onChange={(e) => setTitle(e.target.value)} aria-label={t("PR title")} />
            ) : (
              <strong>{String(payload.title ?? "")}</strong>
            ))}
          {hasBody &&
            (editing ? (
              <textarea className="prose" value={body} onChange={(e) => setBody(e.target.value)} rows={Math.min(24, body.split("\n").length + 2)} />
            ) : (
              <Pre text={String(payload.body)} />
            ))}
          {comments.length > 0 && (
            <div className="stack">
              <span className="small muted">
                {comments.length} {t("inline comments")}
              </span>
              {comments.map((c, i) => (
                <div key={i} className="inline-comment stack">
                  <span className="mono">
                    {c.path}:{c.start_line ? `${c.start_line}-` : ""}
                    {c.line}
                  </span>
                  {editing ? (
                    <textarea
                      className="prose"
                      value={c.body}
                      onChange={(e) => setComments(comments.map((x, j) => (j === i ? { ...x, body: e.target.value } : x)))}
                    />
                  ) : (
                    <Pre text={c.body} />
                  )}
                </div>
              ))}
            </div>
          )}
          {isFix && typeof payload.patch === "string" && <Diff text={payload.patch} />}
          {!hasBody && !comments.length && !labels && <Pre text={JSON.stringify(payload, null, 2)} />}
        </>
      )}
      {!done && (
        <div className="row">
          <button
            className="primary"
            disabled={approve.isPending || (isFix && !me?.can_post_as_me)}
            title={isFix && !me?.can_post_as_me ? t("Sign in with GitHub to open it as yourself.") : undefined}
            onClick={() => approve.mutate(undefined)}
          >
            <Icon name={isFix ? "pr" : "check"} size={14} />
            {approveLabel}
          </button>
          {hasBody && (
            <button
              onClick={() => {
                setExpanded(true);
                setEditing(!editing);
              }}
            >
              {editing ? t("Discard edits") : t("Edit")}
            </button>
          )}
          <RejectButton busy={reject.isPending} onReject={(reason) => reject.mutate(reason)} />
          {canAsMe && kind === "review.post" && (
            <label className="row small muted" style={{ gap: 6 }}>
              <input
                type="checkbox"
                checked={asMe}
                onChange={(e) => {
                  setAsMe(e.target.checked);
                  try {
                    localStorage.setItem("am.post-as-me", e.target.checked ? "1" : "0");
                  } catch {
                    // the choice then lasts for this card only
                  }
                }}
              />
              {t("Post as")} @{me?.login}
            </label>
          )}
          {AS_ME.has(kind) && me && !me.can_post_as_me && (
            <span className="small muted">{t("Sign in with GitHub to post as yourself.")}</span>
          )}
          {kind === "review.post" && action.number != null && Array.isArray((payload.result as { findings?: unknown[] } | undefined)?.findings) && ((payload.result as { findings: unknown[] }).findings.length > 0) && (
            <FixButton repo={action.repo} number={action.number} review={action.id} />
          )}
        </div>
      )}
      <ErrorBox error={approve.error || reject.error} />
    </div>
  );
}

/** Have an agent change the PR's code for a review's findings; the diff comes back as
 * a ``pr.fix`` draft for the person who asked. */
export function FixButton({
  repo,
  number,
  review,
  align = "left",
}: {
  repo: string;
  number: number;
  review?: string;
  align?: "left" | "right";
}) {
  const t = useT();
  const toast = useToast();
  const [agent, setAgent] = useState("");
  const [note, setNote] = useState("");
  const fix = useAct(
    () => api.post(`/api/prs/${repo}/${number}/fix`, { review: review ?? null, agent: agent || null, note }),
    [["pr"], ["prs"], ["proposals"]],
  );
  return (
    <Popover
      align={align}
      label={t("Fix with agent")}
      trigger={(open, toggle) => (
        <button
          className={open ? "on" : ""}
          onClick={toggle}
          aria-expanded={open}
          title={t("An agent changes the code for the review's findings; you see the diff before a PR is opened.")}
        >
          <Icon name="sparkles" size={14} />
          {t("Fix with agent")}
        </button>
      )}
    >
      {(close) => (
        <div className="stack" style={{ gap: 10, padding: 6, width: 340, maxWidth: "calc(100vw - 48px)" }}>
          <span className="small muted">{t("An agent changes the code for the review's findings; you see the diff before a PR is opened.")}</span>
          <textarea
            className="prose"
            rows={3}
            placeholder={t("Anything the agent should know (optional)")}
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
          <div className="row">
            <AgentPicker value={agent} onChange={setAgent} />
            <button
              className="primary right"
              disabled={fix.isPending}
              onClick={() =>
                fix.mutate(undefined, {
                  onSuccess: () => {
                    close();
                    setNote("");
                    toast({
                      title: t("Fix started"),
                      body: t("The diff comes back to you as a proposal; you open the PR as yourself."),
                      icon: "sparkles",
                    });
                  },
                })
              }
            >
              {fix.isPending ? t("Starting…") : t("Start the fix")}
            </button>
          </div>
          <ErrorBox error={fix.error} />
        </div>
      )}
    </Popover>
  );
}
