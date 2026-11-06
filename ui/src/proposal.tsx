// A proposal as the maintainer decides it: exactly what will be posted, editable before
// approval. Used by the inbox, the home feed and the agent panel.

import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type ProposalWithAction } from "./api";
import { ErrorBox, Pre, prPath, RejectButton, Status, useAct, When } from "./components";
import { useT } from "./i18n";
import { Icon } from "./icons";

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
  const record = proposal.action;
  const action = record?.action;
  const payload = (action?.payload ?? {}) as Record<string, unknown>;
  const [editing, setEditing] = useState(false);
  const [expanded, setExpanded] = useState(!compact);
  const [body, setBody] = useState(typeof payload.body === "string" ? payload.body : "");
  const [comments, setComments] = useState<InlineComment[]>(
    Array.isArray(payload.comments) ? (payload.comments as InlineComment[]) : [],
  );

  const approve = useAct(() => {
    const edited: Record<string, unknown> | undefined = editing
      ? { ...payload, body, ...(comments.length ? { comments } : {}) }
      : undefined;
    return api.post(`/api/proposals/${proposal.id}/approve`, edited ? { payload: edited } : {});
  }, PROPOSAL_KEYS);
  const reject = useAct((reason: string) => api.post(`/api/proposals/${proposal.id}/reject`, { reason }), PROPOSAL_KEYS);

  if (!action) return <div className="proposal muted">Action {proposal.action_id} is missing.</div>;
  const hasBody = typeof payload.body === "string";
  const done = proposal.state !== "pending" || approve.isSuccess || reject.isSuccess;
  const labels = Array.isArray(payload.labels) ? (payload.labels as string[]) : null;

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
          {proposal.requested_by ? `for ${proposal.requested_by} · ` : ""}
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
      {compact && (hasBody || comments.length > 0) && (
        <button className="ghost sm" style={{ alignSelf: "flex-start" }} onClick={() => setExpanded(!expanded)}>
          <Icon name={expanded ? "chevronDown" : "chevronRight"} size={14} />
          {expanded ? t("Hide what will be posted") : t("Show what will be posted")}
        </button>
      )}
      {expanded && (
        <>
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
          {!hasBody && !comments.length && !labels && <Pre text={JSON.stringify(payload, null, 2)} />}
        </>
      )}
      {!done && (
        <div className="row">
          <button className="primary" disabled={approve.isPending} onClick={() => approve.mutate(undefined)}>
            <Icon name="check" size={14} />
            {editing ? t("Approve edited") : t("Approve")}
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
        </div>
      )}
      <ErrorBox error={approve.error || reject.error} />
    </div>
  );
}
