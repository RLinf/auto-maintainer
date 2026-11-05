// The proposal inbox: exactly what each proposal will post, with edit-before-approve.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type ProposalWithAction } from "../api";
import { Empty, ErrorBox, Loading, Pre, Status, useAct, When, prPath } from "../components";
import { useT } from "../i18n";

const LABEL_KINDS = new Set(["label.apply", "label.ci_gate", "issue.label"]);
const KEYS = [["proposals"], ["pr"], ["prs"], ["repos"]];

interface InlineComment {
  path: string;
  line: number;
  start_line?: number | null;
  side?: string;
  body: string;
}

export function useProposals() {
  return useQuery({ queryKey: ["proposals", "pending"], queryFn: () => api.get<ProposalWithAction[]>("/api/proposals") });
}

export default function InboxPage({ me }: { me?: string }) {
  const t = useT();
  const proposals = useProposals();
  const bulk = useAct(
    async (ids: string[]) => {
      for (const id of ids) await api.post(`/api/proposals/${id}/approve`, {});
    },
    KEYS,
  );

  if (proposals.isLoading) return <Loading />;
  const mine = (proposals.data ?? []).filter((p) => me && p.requested_by === me);
  const groups = new Map<string, ProposalWithAction[]>();
  for (const p of proposals.data ?? []) {
    if (me && p.requested_by === me) continue;
    groups.set(p.repo, [...(groups.get(p.repo) ?? []), p]);
  }

  return (
    <>
      <h1>{t("Inbox")}</h1>
      <ErrorBox error={proposals.error || bulk.error} />
      {mine.length > 0 && (
        <section className="card stack">
          <header className="card-head">
            <h2>
              {t("Your review drafts")} <span className="muted small">{mine.length} to edit and post</span>
            </h2>
          </header>
          {mine.map((p) => (
            <ProposalCard key={p.id} proposal={p} />
          ))}
        </section>
      )}
      {groups.size === 0 && mine.length === 0 && <Empty>No proposals wait for you.</Empty>}
      {[...groups.entries()].map(([repo, items]) => {
        const labelIds = items.filter((p) => p.action && LABEL_KINDS.has(p.action.action.kind)).map((p) => p.id);
        return (
          <section key={repo} className="card stack">
            <header className="card-head">
              <h2>
                {repo} <span className="muted small">{items.length} pending</span>
              </h2>
              {labelIds.length > 0 && (
                <button disabled={bulk.isPending} onClick={() => bulk.mutate(labelIds)}>
                  {t("Approve all labels")} ({labelIds.length})
                </button>
              )}
            </header>
            {items.map((p) => (
              <ProposalCard key={p.id} proposal={p} />
            ))}
          </section>
        );
      })}
    </>
  );
}

export function ProposalCard({ proposal, compact = false }: { proposal: ProposalWithAction; compact?: boolean }) {
  const t = useT();
  const record = proposal.action;
  const action = record?.action;
  const payload = (action?.payload ?? {}) as Record<string, unknown>;
  const [editing, setEditing] = useState(false);
  const [body, setBody] = useState(typeof payload.body === "string" ? payload.body : "");
  const [comments, setComments] = useState<InlineComment[]>(Array.isArray(payload.comments) ? (payload.comments as InlineComment[]) : []);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");

  const approve = useAct(() => {
    const edited: Record<string, unknown> | undefined = editing ? { ...payload, body, ...(comments.length ? { comments } : {}) } : undefined;
    return api.post(`/api/proposals/${proposal.id}/approve`, edited ? { payload: edited } : {});
  }, KEYS);
  const reject = useAct(() => api.post(`/api/proposals/${proposal.id}/reject`, { reason }), KEYS);

  if (!action) return <div className="proposal muted">Action {proposal.action_id} is missing.</div>;
  const hasBody = typeof payload.body === "string";
  const done = proposal.state !== "pending" || approve.isSuccess || reject.isSuccess;

  return (
    <div className="proposal">
      <div className="row">
        <span className="mono">{action.kind}</span>
        {action.number != null && (
          <Link to={prPath(action.repo, action.number)}>
            {action.repo}#{action.number}
          </Link>
        )}
        {action.head_sha && <span className="mono muted small">at {action.head_sha.slice(0, 7)}</span>}
        <Status value={approve.isSuccess ? "approved" : reject.isSuccess ? "rejected" : proposal.state} />
        <span className="muted small right">
          <When at={proposal.created_at} />
        </span>
      </div>
      {action.rationale && <div className="small muted">{action.rationale}</div>}
      {proposal.warnings.length > 0 && (
        <ul className="small" style={{ margin: 0, color: "var(--warn)" }}>
          {proposal.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
      {!compact && (
        <>
          {hasBody &&
            (editing ? (
              <textarea value={body} onChange={(e) => setBody(e.target.value)} rows={Math.min(24, body.split("\n").length + 2)} />
            ) : (
              <Pre text={String(payload.body)} />
            ))}
          {comments.length > 0 && (
            <div className="stack">
              <span className="small muted">{comments.length} inline comments</span>
              {comments.map((c, i) => (
                <div key={i} className="inline-comment stack">
                  <span className="mono small">
                    {c.path}:{c.start_line ? `${c.start_line}-` : ""}
                    {c.line}
                  </span>
                  {editing ? (
                    <textarea
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
          {!hasBody && !comments.length && <Pre text={JSON.stringify(payload, null, 2)} />}
        </>
      )}
      {!done && (
        <div className="row">
          <button className="primary" disabled={approve.isPending} onClick={() => approve.mutate(undefined)}>
            {editing ? "Approve edited" : t("Approve")}
          </button>
          {!compact && hasBody && <button onClick={() => setEditing(!editing)}>{editing ? "Discard edits" : "Edit"}</button>}
          {rejecting ? (
            <>
              <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Reason (required)" />
              <button className="danger" disabled={!reason.trim() || reject.isPending} onClick={() => reject.mutate(undefined)}>
                {t("Reject")}
              </button>
            </>
          ) : (
            <button className="danger" onClick={() => setRejecting(true)}>
              {t("Reject")}
            </button>
          )}
        </div>
      )}
      <ErrorBox error={approve.error || reject.error} />
    </div>
  );
}
