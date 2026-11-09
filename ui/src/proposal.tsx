// A proposal as the maintainer decides it: exactly what will happen, shown the way it
// will look (a review as GitHub renders it, its comments under the lines they are
// about, a script to run, a diff to push), editable before approval. One main button
// says what approving does. Used by the inbox, the home feed, the agent panel, and the
// job view.

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type ProposalWithAction } from "./api";
import { AgentPicker, useAgentChoice } from "./agentPicker";
import { Diff, ErrorBox, prPath, RejectButton, Status, useAct, When } from "./components";
import { useMe } from "./data";
import { CommentOnDiff, parseDiff, usePrDiff } from "./diffview";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { Markdown } from "./markdown";
import { openJob } from "./openJob";
import { MenuItem, Popover, useToast } from "./ui";
import { TranslateReview } from "./translate";

export const PROPOSAL_KEYS = [["proposals"], ["pr"], ["prs"], ["repos"], ["audit"], ["work"], ["job"]];

/** Kinds a maintainer may approve as themselves, with their own GitHub account. */
const AS_ME = new Set(["review.post", "pr.fix"]);

/** Kinds whose ``body`` is Markdown posted on GitHub. */
const POSTED_BODY = new Set([
  "review.post",
  "review.update",
  "thread.reply",
  "comment.upsert",
  "pr.nudge",
  "pr.convert_to_draft",
  "pr.close",
  "issue.comment",
  "pr.merge",
  "pr.fix",
]);

export interface InlineComment {
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
  "pr.approve": "Approve the PR",
  "ci.approve_run": "Approve and run CI",
  "pr.merge": "Merge",
  "pr.nudge": "Nudge the author",
  "pr.fix": "Open a fix PR",
  "pr.convert_to_draft": "Convert to draft",
  "pr.close": "Close the PR",
  "issue.label": "Label an issue",
  "issue.comment": "Comment on an issue",
  "lab.run": "Run a lab test",
  "lab.reproduce": "Reproduce on a test machine",
  "lab.review": "Test during reviews on a test machine",
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

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

function list(v: unknown): string[] {
  return Array.isArray(v) ? v.map(String) : [];
}

function readAsMe(): boolean {
  try {
    return localStorage.getItem("am.post-as-me") !== "0";
  } catch {
    return true;
  }
}

function saveAsMe(v: boolean) {
  try {
    localStorage.setItem("am.post-as-me", v ? "1" : "0");
  } catch {
    // the choice then lasts for this page only
  }
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
  const kind = action?.kind ?? "";
  const [editing, setEditing] = useState(false);
  // One-line actions (labels, reviewers, reruns) show in full even in compact lists.
  const short = !POSTED_BODY.has(kind) && kind !== "lab.reproduce" && !Array.isArray(payload.comments);
  const [expanded, setExpanded] = useState(!compact || short);
  const [raw, setRaw] = useState(false);
  const [body, setBody] = useState(str(payload.body));
  const [title, setTitle] = useState(str(payload.title));
  const [script, setScript] = useState(str(payload.script));
  const [setup, setSetup] = useState(str(payload.setup));
  const [steps, setSteps] = useState(list(payload.steps).join("\n"));
  const [comments, setComments] = useState<InlineComment[]>(Array.isArray(payload.comments) ? (payload.comments as InlineComment[]) : []);
  const [asMe, setAsMe] = useState(readAsMe);
  // Labels the reviewer leaves out of a label proposal before approving the rest.
  const [dropped, setDropped] = useState<string[]>([]);
  // The review's live view, where its conversation is, when it is the one open.
  const openedJob = useSearchParams()[0].get("job");
  // A revision from the conversation about the review replaces the draft; show it.
  useEffect(() => {
    if (editing) return;
    setBody(str(payload.body));
    setComments(Array.isArray(payload.comments) ? (payload.comments as InlineComment[]) : []);
  }, [payload.body, payload.comments, editing]);
  const isFix = kind === "pr.fix";
  // A merge of the base branch always goes onto the PR's branch; a fix may do either.
  const isMerge = isFix && !!str(payload.merge_ref);
  const [target, setTarget] = useState(str(payload.target) || "pr");
  const changedTarget = isFix && !isMerge && target !== (str(payload.target) || "pr");
  const canAsMe = !!me?.can_post_as_me && AS_ME.has(kind);
  // An edit of a posted review is done by whoever posted it: GitHub lets only the author edit it.
  const reviewAuthor = kind === "review.update" ? str(payload.author) : "";
  const notTheAuthor = !!reviewAuthor && me?.login !== reviewAuthor;
  // A fix is pushed only by a person; the service's own account never pushes to a repository.
  const postAsMe = isFix || !!reviewAuthor || (canAsMe && asMe);
  const hasComments = Array.isArray(payload.comments) && (payload.comments as unknown[]).length > 0;
  const diff = usePrDiff(action?.repo ?? "", action?.number, expanded && hasComments);
  const files = useMemo(() => (diff.data ? parseDiff(diff.data.diff) : null), [diff.data]);

  const editedPayload = (): Record<string, unknown> => ({
    ...payload,
    ...(typeof payload.body === "string" ? { body } : {}),
    ...(Array.isArray(payload.comments) ? { comments } : {}),
    ...(isFix ? { title, ...(isMerge ? {} : { target }) } : {}),
    ...(dropped.length ? { labels: list(payload.labels).filter((l) => !dropped.includes(l)) } : {}),
    ...(kind === "lab.reproduce"
      ? str(payload.script)
        ? { script, setup }
        : {
            steps: steps
              .split("\n")
              .map((x) => x.trim())
              .filter(Boolean),
          }
      : {}),
  });
  const approve = useAct((as?: boolean) => {
    const edited = editing || changedTarget || dropped.length ? editedPayload() : undefined;
    const mine = as ?? postAsMe;
    return api.post(`/api/proposals/${proposal.id}/approve`, { ...(edited ? { payload: edited } : {}), ...(mine ? { as_me: true } : {}) });
  }, PROPOSAL_KEYS);
  // Keeps the edits on the draft without posting it; posting later sends what was saved.
  const save = useAct(() => api.post(`/api/proposals/${proposal.id}/edit`, { payload: editedPayload() }), PROPOSAL_KEYS);
  const reject = useAct((reason: string) => api.post(`/api/proposals/${proposal.id}/reject`, { reason }), PROPOSAL_KEYS);

  if (!action) return <div className="proposal muted">Action {proposal.action_id} is missing.</div>;
  const done = proposal.state !== "pending" || approve.isSuccess || reject.isSuccess;
  // The repository's maintainers decide every proposal; a reviewer, the ones they asked for.
  const decides = proposal.can_decide !== false;
  const requester = proposal.requester ?? proposal.requested_by;
  const editable = typeof payload.body === "string" || kind === "lab.reproduce" || (Array.isArray(payload.comments) && comments.length > 0);
  const findings = ((payload.result as { findings?: unknown[] } | undefined)?.findings ?? []).length;
  const approveLabel = isFix
    ? isMerge
      ? t("Push the merge as me")
      : target === "branch"
        ? t("Push to the PR as me")
        : t("Open the PR as me")
    : kind === "lab.reproduce"
      ? `${t("Run on")} ${str(payload.lab)}`
      : kind === "review.post"
        ? postAsMe
          ? t("Post as me")
          : t("Post")
        : kind === "review.update" && payload.revises
          ? reviewAuthor
            ? t("Update the review as me")
            : t("Update the posted review")
          : POSTED_BODY.has(kind)
          ? t("Post")
          : t("Approve");

  const toggleDrop =
    kind === "label.apply" && !done && decides
      ? (l: string) => setDropped((d) => (d.includes(l) ? d.filter((x) => x !== l) : [...d, l]))
      : undefined;
  const details = (
    <KindBody
      kind={kind}
      payload={payload}
      dropped={dropped}
      toggleDrop={toggleDrop}
      editing={editing}
      raw={raw}
      body={body}
      setBody={setBody}
      title={title}
      setTitle={setTitle}
      script={script}
      setScript={setScript}
      setup={setup}
      setSetup={setSetup}
      steps={steps}
      setSteps={setSteps}
      comments={comments}
      setComments={setComments}
      files={files}
      diffLoading={diff.isLoading}
    />
  );

  return (
    <div className="proposal">
      <div className="row">
        {onSelect && !done && decides && <input type="checkbox" aria-label={`Select ${kindLabel(kind)}`} checked={!!selected} onChange={onSelect} />}
        <strong>{t(kindLabel(kind))}</strong>
        {action.number != null && (
          <Link to={prPath(action.repo, action.number)} className="small">
            {action.repo}#{action.number}
          </Link>
        )}
        {action.number == null && <span className="small muted">{action.repo}</span>}
        {action.head_sha && <span className="mono muted">{action.head_sha.slice(0, 7)}</span>}
        <Status value={approve.isSuccess ? "approved" : reject.isSuccess ? "rejected" : proposal.state} />
        <span className="muted small right">
          {requester ? `${t("requested by")} ${requester === me?.login ? t("you") : requester} · ` : ""}
          <When at={proposal.created_at} />
        </span>
      </div>
      {action.rationale && <div className="small muted">{action.rationale}</div>}
      {proposal.warnings.length > 0 && (
        <ul className="small" style={{ margin: 0, paddingLeft: 18, color: "var(--warn)" }}>
          {proposal.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
      {compact && !expanded ? (
        <button className="ghost sm" style={{ alignSelf: "flex-start" }} onClick={() => setExpanded(true)}>
          <Icon name="chevronRight" size={14} />
          {t("Show what will happen")}
        </button>
      ) : (
        <>
          {details}
          {POSTED_BODY.has(kind) && typeof payload.body === "string" && !editing && (
            <button className="ghost sm" style={{ alignSelf: "flex-start" }} onClick={() => setRaw(!raw)}>
              {raw ? t("Show as it will look") : t("Show the Markdown")}
            </button>
          )}
          {(kind === "review.post" || kind === "review.update") && typeof payload.body === "string" && !editing && (
            <TranslateReview
              actionId={action.id}
              places={Array.isArray(payload.comments) ? (payload.comments as InlineComment[]).map((c) => `${c.path}:${c.line}`) : []}
            />
          )}
        </>
      )}
      {!done && !decides && (
        <span className="small muted">
          {requester
            ? `${t("A maintainer of the repository, or the person who asked for it, decides this:")} @${requester}.`
            : t("A maintainer of the repository decides this.")}
        </span>
      )}
      {!done && decides && (
        <div className="row">
          <div className="split-button">
            <button
              className="primary"
              disabled={approve.isPending || ((isFix || !!reviewAuthor) && !me?.can_post_as_me) || notTheAuthor}
              title={
                notTheAuthor
                  ? `${t("Only the reviewer who posted it can change it:")} @${reviewAuthor}`
                  : isFix && !me?.can_post_as_me
                    ? t("Sign in with GitHub to open it as yourself.")
                    : reviewAuthor && !me?.can_post_as_me
                      ? t("Sign in with GitHub to do it as yourself.")
                      : undefined
              }
              onClick={() => approve.mutate(undefined)}
            >
              <Icon name={isFix ? "pr" : kind === "lab.reproduce" ? "server" : "check"} size={14} />
              {editing ? `${approveLabel} (${t("edited")})` : approveLabel}
            </button>
            {canAsMe && kind === "review.post" && (
              <Popover
                align="left"
                label={t("Post as")}
                trigger={(open, toggle) => (
                  <button className="primary caret" aria-label={t("Post as")} aria-expanded={open} onClick={toggle}>
                    <Icon name="chevronDown" size={14} />
                  </button>
                )}
              >
                {(close) => (
                  <>
                    <MenuItem
                      checked={asMe}
                      onClick={() => {
                        setAsMe(true);
                        saveAsMe(true);
                        close();
                      }}
                    >
                      {t("Post as me")} (@{me?.login})
                    </MenuItem>
                    <MenuItem
                      checked={!asMe}
                      onClick={() => {
                        setAsMe(false);
                        saveAsMe(false);
                        close();
                      }}
                    >
                      {t("Post as the service account")}
                    </MenuItem>
                  </>
                )}
              </Popover>
            )}
          </div>
          {isFix && !isMerge && (
            <select value={target} onChange={(e) => setTarget(e.target.value)} aria-label={t("Where the fix goes")}>
              <option value="pr">{t("As a PR into this PR")}</option>
              <option value="branch">{t("As a commit on the PR's branch")}</option>
            </select>
          )}
          {editing && (
            <button disabled={save.isPending} onClick={() => save.mutate(undefined, { onSuccess: () => setEditing(false) })}>
              <Icon name="check" size={14} />
              {save.isPending ? t("Saving…") : t("Save")}
            </button>
          )}
          {editable && (
            <button
              className={editing ? "ghost" : ""}
              onClick={() => {
                if (editing) {
                  // Discarding returns to what the draft holds now (saved edits included).
                  setBody(str(payload.body));
                  setTitle(str(payload.title));
                  setScript(str(payload.script));
                  setSetup(str(payload.setup));
                  setComments(Array.isArray(payload.comments) ? (payload.comments as InlineComment[]) : []);
                }
                setExpanded(true);
                setRaw(false);
                setEditing(!editing);
              }}
            >
              {editing ? t("Discard edits") : t("Edit")}
            </button>
          )}
          {kind === "review.post" && action.provenance?.job_id && openedJob !== action.provenance.job_id && (
            <button className="ghost" title={t("Talk with the agent about this review: ask why, push back, or have it change the draft")} onClick={() => openJob(action.provenance.job_id as string)}>
              <Icon name="message" size={14} />
              {t("Discuss")}
            </button>
          )}
          <RejectButton quiet busy={reject.isPending} onReject={(reason) => reject.mutate(reason)} />
          {kind === "review.post" && action.number != null && findings > 0 && (
            <span className="right">
              <FixButton repo={action.repo} number={action.number} review={action.id} align="right" quiet />
            </span>
          )}
        </div>
      )}
      {isFix && !done && decides && me && !me.can_post_as_me && <span className="small muted">{t("Sign in with GitHub to open it as yourself.")}</span>}
      {kind === "review.update" && !!payload.revises && !done && (
        <span className="small muted">
          {reviewAuthor
            ? notTheAuthor
              ? `${t("Only the reviewer who posted it can change it:")} @${reviewAuthor}.`
              : t("This edits the review you posted, as you.")
            : t("This edits the review the service posted.")}{" "}
          {t("Comments already posted on lines of the diff stay as they are.")}
        </span>
      )}
      {proposal.edited && !editing && !done && <span className="small muted">{t("Edited; posting sends the saved version.")}</span>}
      <ErrorBox error={approve.error || reject.error || save.error} />
    </div>
  );
}

function KindBody({
  kind,
  payload,
  dropped = [],
  toggleDrop,
  editing,
  raw,
  body,
  setBody,
  title,
  setTitle,
  script,
  setScript,
  setup,
  setSetup,
  steps,
  setSteps,
  comments,
  setComments,
  files,
  diffLoading,
}: {
  kind: string;
  payload: Record<string, unknown>;
  /** Labels left out of a label proposal; ``toggleDrop`` leaves one out or keeps it. */
  dropped?: string[];
  toggleDrop?: (label: string) => void;
  editing: boolean;
  raw: boolean;
  body: string;
  setBody: (v: string) => void;
  title: string;
  setTitle: (v: string) => void;
  script: string;
  setScript: (v: string) => void;
  setup: string;
  setSetup: (v: string) => void;
  steps: string;
  setSteps: (v: string) => void;
  comments: InlineComment[];
  setComments: (v: InlineComment[]) => void;
  files: ReturnType<typeof parseDiff> | null;
  diffLoading: boolean;
}) {
  const t = useT();
  const parts: ReactNode[] = [];
  const posted = (text: string) =>
    raw ? (
      <pre key="raw" className="pre">
        {text}
      </pre>
    ) : (
      <Markdown key="md" text={text} className="posted" />
    );

  if (kind === "pr.fix") {
    const changed = list(payload.files);
    const merge = str(payload.merge_ref);
    parts.push(
      <div key="fix" className="small muted">
        {merge
          ? `${t("Merges")} ${merge} (${str(payload.merge_sha).slice(0, 7)}) ${t("into the PR's branch")} · ${changed.length} ${t("conflicted files resolved")}`
          : `${changed.length} ${t("files changed")}`}
        {str(payload.runtime) ? ` · ${str(payload.runtime)}` : ""}
      </div>,
      editing ? <input key="title" value={title} onChange={(e) => setTitle(e.target.value)} aria-label={t("PR title")} /> : <strong key="title">{str(payload.title)}</strong>,
    );
  }
  if (kind === "pr.merge") {
    parts.push(
      <div key="merge" className="small">
        {t("Merge method")}: <code>{str(payload.method) || "merge"}</code>
        {str(payload.subject) ? (
          <>
            {" "}
            · <strong>{str(payload.subject)}</strong>
          </>
        ) : null}
      </div>,
    );
  }
  if (typeof payload.body === "string") {
    parts.push(
      editing ? (
        <textarea key="body" className="prose editor" value={body} onChange={(e) => setBody(e.target.value)} rows={Math.max(16, Math.min(60, body.split("\n").length + 4))} />
      ) : (
        posted(str(payload.body))
      ),
    );
  }
  if (comments.length > 0) {
    parts.push(
      <div key="comments" className="stack" style={{ gap: 10 }}>
        <span className="small muted">
          {comments.length} {t("inline comments")}
        </span>
        {comments.map((c, i) => (
          <CommentOnDiff
            key={`${c.path}:${c.line}:${i}`}
            files={files}
            loading={diffLoading}
            path={c.path}
            line={c.line}
            start={c.start_line}
            side={c.side}
            outdated={t("This line is not in the PR's current diff; GitHub may refuse the comment.")}
          >
            {editing ? (
              <div className="stack" style={{ gap: 6 }}>
                <textarea
                  className="prose editor"
                  rows={Math.max(5, Math.min(24, c.body.split("\n").length + 2))}
                  value={c.body}
                  onChange={(e) => setComments(comments.map((x, j) => (j === i ? { ...x, body: e.target.value } : x)))}
                />
                <button className="ghost sm danger-text" style={{ alignSelf: "flex-start" }} onClick={() => setComments(comments.filter((_, j) => j !== i))}>
                  <Icon name="x" size={13} />
                  {t("Remove this comment")}
                </button>
              </div>
            ) : raw ? (
              <pre className="pre">{c.body}</pre>
            ) : (
              <Markdown text={c.body} className="posted" />
            )}
          </CommentOnDiff>
        ))}
      </div>,
    );
  }
  if (kind === "pr.fix" && typeof payload.patch === "string") parts.push(<Diff key="patch" text={payload.patch} />);
  if (kind === "lab.reproduce") {
    const planned = list(payload.steps);
    const oneScript = !!str(payload.script);
    parts.push(
      <div key="repro" className="stack" style={{ gap: 8 }}>
        <div>{str(payload.summary)}</div>
        <div className="small muted">
          {oneScript
            ? t("Runs on")
            : t("An agent works on")}{" "}
          <strong>{str(payload.lab)}</strong>,{" "}
          {list(payload.sides).length === 1 ? t("at the PR's head") : t("before this PR and at its head")}, {t("in a fresh folder that is removed afterwards.")}
          {!oneScript && ` ${t("Each command it runs is checked against the machine's rules, and you can watch it work.")}`}
          {payload.needs_gpu ? ` ${t("Needs one idle GPU.")}` : ""} {t("At most")} {Math.round(Number(payload.timeout_seconds ?? 1800) / 60)} {t("minutes")}.
        </div>
        {oneScript ? (
          <>
            {(editing || str(payload.setup)) && (
              <>
                <span className="small muted">{t("Setup")}</span>
                {editing ? <textarea className="mono-input" rows={3} value={setup} onChange={(e) => setSetup(e.target.value)} /> : <pre className="md-code">{str(payload.setup)}</pre>}
              </>
            )}
            <span className="small muted">{t("Script")}</span>
            {editing ? (
              <textarea className="mono-input" rows={Math.min(24, script.split("\n").length + 2)} value={script} onChange={(e) => setScript(e.target.value)} />
            ) : (
              <pre className="md-code">{str(payload.script)}</pre>
            )}
          </>
        ) : (
          <>
            <span className="small muted">{t("The plan")}</span>
            {editing ? (
              <textarea rows={Math.min(16, steps.split("\n").length + 2)} value={steps} onChange={(e) => setSteps(e.target.value)} aria-label={t("One step per line")} />
            ) : (
              <ol className="plan-steps">
                {planned.map((x) => (
                  <li key={x}>{x}</li>
                ))}
              </ol>
            )}
          </>
        )}
        {str(payload.expect) && (
          <div className="small">
            <span className="muted">{t("What would confirm it")}:</span> {str(payload.expect)}
          </div>
        )}
      </div>,
    );
  }
  if (kind === "pr.request_reviewers") {
    const logins = list(payload.logins ?? payload.reviewers);
    parts.push(
      <div key="rr">
        {t("Ask for a review from")} {logins.map((l) => `@${l}`).join(", ") || "—"}
      </div>,
    );
  }
  if (kind === "label.apply" || kind === "issue.label") {
    const labels = [...list(payload.labels), ...list(payload.roles)];
    const removed = list(payload.remove_roles);
    parts.push(
      <div key="labels" className="labels">
        {labels.map((l) =>
          toggleDrop && list(payload.labels).includes(l) ? (
            <button
              key={l}
              type="button"
              className={`label${dropped.includes(l) ? " muted" : ""}`}
              style={{ cursor: "pointer", textDecoration: dropped.includes(l) ? "line-through" : undefined }}
              aria-pressed={!dropped.includes(l)}
              title={dropped.includes(l) ? t("Keep this label") : t("Leave this label out")}
              onClick={() => toggleDrop(l)}
            >
              {dropped.includes(l) ? "−" : "+"} {l}
            </button>
          ) : (
            <span key={l} className="label">
              + {l}
            </span>
          ),
        )}
        {removed.map((l) => (
          <span key={l} className="label muted">
            − {l}
          </span>
        ))}
      </div>,
    );
  }
  if (kind === "label.ci_gate") parts.push(<div key="gate">{t("Adds the label that lets the full CI run on this PR.")}</div>);
  if (kind === "pr.update_branch") parts.push(<div key="ub">{t("Merges the base branch into the PR's branch.")}</div>);
  if (kind === "ci.rerun_failed")
    parts.push(
      <div key="rerun">
        {t("Reruns the failed jobs of run")} #{String(payload.run_id ?? "")}
        {str(payload.workflow) ? ` (${str(payload.workflow)})` : ""}
      </div>,
    );
  if (!parts.length)
    parts.push(
      <details key="json">
        <summary className="small muted">{t("Details")}</summary>
        <pre className="pre">{JSON.stringify(payload, null, 2)}</pre>
      </details>,
    );
  return <>{parts}</>;
}

/** Have an agent change the PR's code for a review's findings; the diff comes back as
 * a ``pr.fix`` draft for the person who asked. */
/** Bring the PR's base branch into it, as the person who presses: GitHub updates the
 *  branch at once when they merge cleanly; otherwise an agent resolves the conflicts
 *  and the merge comes back as a draft to read and push. */
export function MergeBaseButton({
  repo,
  number,
  base,
  conflicting,
  canPush,
}: {
  repo: string;
  number: number;
  base: string;
  conflicting: boolean;
  canPush: boolean;
}) {
  const t = useT();
  const toast = useToast();
  const [agent, setAgent] = useAgentChoice("fix");
  const merge = useAct(
    () =>
      api.post<{ run: "update" | "resolve"; status?: string; error?: string | null; event?: string }>(`/api/prs/${repo}/${number}/merge-base`, {
        agent: agent || null,
      }),
    [["pr"], ["prs"], ["proposals"], ["work"]],
  );
  const label = conflicting ? `${t("Resolve conflicts with")} ${base}` : `${t("Merge")} ${base}`;
  return (
    <Popover
      align="right"
      label={label}
      trigger={(open, toggle) => (
        <button className={open ? "on" : ""} onClick={toggle} aria-expanded={open}>
          <Icon name="merge" size={14} />
          {label}
        </button>
      )}
    >
      {(close) => (
        <div className="stack" style={{ gap: 10, padding: 6, width: 340, maxWidth: "calc(100vw - 48px)" }}>
          <span className="small muted">
            {conflicting
              ? t("An agent merges the base branch into the PR and resolves the conflicts. You read the resolution, then push the merge onto the PR's branch as yourself.")
              : t("GitHub merges the base branch into the PR's branch now, as you.")}
          </span>
          <div className="row">
            {conflicting && <AgentPicker value={agent} onChange={setAgent} task="fix" />}
            <button
              className="primary right"
              disabled={merge.isPending || !canPush}
              title={canPush ? undefined : t("Sign in with GitHub, so the merge is pushed as you.")}
              onClick={() =>
                merge.mutate(undefined, {
                  onSuccess: (out) => {
                    close();
                    if (out.run === "resolve") {
                      toast({
                        title: t("Resolving the conflicts"),
                        body: t("The merge comes back to you as a draft; you push it as yourself."),
                        icon: "sparkles",
                        action: out.event ? { label: t("Watch"), onClick: () => openJob(out.event as string) } : undefined,
                      });
                    } else if (out.status === "executed") {
                      toast({ title: `${t("Merged")} ${base}`, body: t("GitHub updated the PR's branch as you."), icon: "check" });
                    } else {
                      toast({ title: t("The branch was not updated"), body: out.error ?? out.status, icon: "x" });
                    }
                  },
                })
              }
            >
              {merge.isPending ? t("Starting…") : conflicting ? t("Start resolving") : t("Merge it as me")}
            </button>
          </div>
          {!canPush && <span className="small muted">{t("Sign in with GitHub, so the merge is pushed as you.")}</span>}
          <ErrorBox error={merge.error} />
        </div>
      )}
    </Popover>
  );
}

/** Approve the PR on GitHub as yourself, at the head you are looking at. The service's
 *  own account never approves; whether the approval counts is GitHub's rule for you. */
export function ApproveButton({
  repo,
  number,
  head,
  approved,
  own,
  canPost,
}: {
  repo: string;
  number: number;
  head: string;
  /** Your newest review on GitHub approves this head. */
  approved: boolean;
  /** You opened the PR, which GitHub does not let you approve. */
  own: boolean;
  canPost: boolean;
}) {
  const t = useT();
  const toast = useToast();
  const [note, setNote] = useState("");
  const approve = useAct(
    () =>
      api.post<{ status: string; error: string | null; reason: string | null; rule: string | null }>(`/api/prs/${repo}/${number}/approve`, {
        body: note,
        head,
      }),
    [["pr"], ["prs"], ["work"]],
  );
  if (approved) {
    return (
      <span className="button on" title={t("Your newest review on GitHub approves this head")}>
        <Icon name="check" size={14} />
        {t("Approved by you")}
      </span>
    );
  }
  if (own) return null;
  return (
    <Popover
      align="right"
      label={t("Approve")}
      trigger={(open, toggle) => (
        <button className={open ? "on" : ""} onClick={toggle} aria-expanded={open}>
          <Icon name="check" size={14} />
          {t("Approve")}
        </button>
      )}
    >
      {(close) => (
        <div className="stack" style={{ gap: 10, padding: 6, width: 340, maxWidth: "calc(100vw - 48px)" }}>
          <span className="small muted">
            {t("Approves the PR on GitHub as you, at")} <code>{head.slice(0, 7)}</code>.
          </span>
          <textarea rows={3} value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("A comment with the approval (optional)")} aria-label={t("A comment with the approval")} />
          <div className="row">
            <button
              className="primary right"
              disabled={approve.isPending || !canPost}
              title={canPost ? undefined : t("Sign in with GitHub, so the approval comes from you.")}
              onClick={() =>
                approve.mutate(undefined, {
                  onSuccess: (out) => {
                    close();
                    if (out.status === "executed") {
                      setNote("");
                      toast({ title: t("Approved"), body: t("GitHub has your approval of this PR."), icon: "check" });
                    } else if (out.status === "queued" || out.status === "intent") {
                      toast({ title: t("Approving…"), body: t("Your approval goes to GitHub in a moment."), icon: "clock" });
                    } else if (out.status === "shadowed") {
                      toast({
                        title: t("Not approved"),
                        body: t("This instance only records what it would do, so GitHub was not asked."),
                        icon: "eye",
                      });
                    } else {
                      toast({ title: t("Not approved"), body: out.error || out.reason || out.status, icon: "x" });
                    }
                  },
                })
              }
            >
              {approve.isPending ? t("Approving…") : t("Approve as me")}
            </button>
          </div>
          {!canPost && <span className="small muted">{t("Sign in with GitHub, so the approval comes from you.")}</span>}
          <ErrorBox error={approve.error} />
        </div>
      )}
    </Popover>
  );
}

export function FixButton({
  repo,
  number,
  review,
  align = "left",
  quiet,
}: {
  repo: string;
  number: number;
  review?: string;
  align?: "left" | "right";
  quiet?: boolean;
}) {
  const t = useT();
  const toast = useToast();
  const [agent, setAgent] = useAgentChoice("fix");
  const [note, setNote] = useState("");
  const fix = useAct(
    () => api.post<{ event: string }>(`/api/prs/${repo}/${number}/fix`, { review: review ?? null, agent: agent || null, note }),
    [["pr"], ["prs"], ["proposals"], ["work"]],
  );
  return (
    <Popover
      align={align}
      label={t("Fix with agent")}
      trigger={(open, toggle) => (
        <button
          className={`${quiet ? "ghost" : ""} ${open ? "on" : ""}`}
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
          <textarea className="prose" rows={3} placeholder={t("Anything the agent should know (optional)")} value={note} onChange={(e) => setNote(e.target.value)} />
          <div className="row">
            <AgentPicker value={agent} onChange={setAgent} task="fix" />
            <button
              className="primary right"
              disabled={fix.isPending}
              onClick={() =>
                fix.mutate(undefined, {
                  onSuccess: (out) => {
                    close();
                    setNote("");
                    toast({
                      title: t("Fix started"),
                      body: t("The diff comes back to you as a proposal; you open the PR as yourself."),
                      icon: "sparkles",
                      action: { label: t("Watch"), onClick: () => openJob(out.event) },
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
