// The review queue: open PRs ranked by internal priority.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, qs, type Me, type PrRow, type ReviewActivity, type ReviewStart } from "../api";
import { Empty, ErrorBox, Labels, Loading, prPath, Tier, When } from "../components";
import { useT } from "../i18n";

const STATES = ["", "NEW_REVIEW", "FOLLOWUP_REVIEW", "APPROVED_NEW_COMMITS", "UP_TO_DATE", "SELF_PR", "DRAFT"];
const TIERS = ["", "P0", "P1", "P2", "P3"];

export default function QueuePage({ me }: { me: Me }) {
  const t = useT();
  const [repo, setRepo] = useState("");
  const [tier, setTier] = useState("");
  const [state, setState] = useState("");
  const [mine, setMine] = useState(false);
  const params = { repo, tier, state, mine };
  const prs = useQuery({ queryKey: ["prs", params], queryFn: () => api.get<PrRow[]>(`/api/prs${qs(params)}`) });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [started, setStarted] = useState<ReviewStart[]>([]);
  const client = useQueryClient();
  const review = useMutation({
    mutationFn: (rows: PrRow[]) =>
      api.post<ReviewStart[]>("/api/reviews", { prs: rows.map((r) => ({ repo: r.repo, number: r.number })) }),
    onSuccess: (out) => {
      setStarted(out);
      setSelected(new Set());
      void client.invalidateQueries({ queryKey: ["prs"] });
    },
  });
  const keyOf = (p: PrRow) => `${p.repo}#${p.number}`;
  const toggle = (p: PrRow) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(keyOf(p))) next.delete(keyOf(p));
      else next.add(keyOf(p));
      return next;
    });
  const chosen = (prs.data ?? []).filter((p) => selected.has(keyOf(p)));
  const reviewable = (prs.data ?? []).filter((p) => p.review_state !== "UP_TO_DATE");
  const allChosen = reviewable.length > 0 && reviewable.every((p) => selected.has(keyOf(p)));

  return (
    <>
      <div className="row">
        <h1>{t("Queue")}</h1>
        <span className="pill status-warn">{t("Internal")}</span>
        <span className="muted small">Tiers and scores are private to maintainers and never posted.</span>
      </div>
      <div className="filters">
        <label className="field">
          {t("Repository")}
          <select value={repo} onChange={(e) => setRepo(e.target.value)}>
            <option value="">{t("All")}</option>
            {me.repos.map((r) => (
              <option key={r}>{r}</option>
            ))}
          </select>
        </label>
        <label className="field">
          {t("Tier")}
          <select value={tier} onChange={(e) => setTier(e.target.value)}>
            {TIERS.map((x) => (
              <option key={x} value={x}>
                {x || t("All")}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          {t("Review state")}
          <select value={state} onChange={(e) => setState(e.target.value)}>
            {STATES.map((x) => (
              <option key={x} value={x}>
                {x || t("All")}
              </option>
            ))}
          </select>
        </label>
        <label className="row small">
          <input type="checkbox" checked={mine} onChange={(e) => setMine(e.target.checked)} /> {t("Mine")}
        </label>
        <button
          className="primary"
          disabled={!chosen.length || review.isPending}
          onClick={() => review.mutate(chosen)}
          title="Run a review, or a follow-up for PRs reviewed before. Drafts go to your inbox to edit and post."
        >
          {review.isPending ? t("Starting…") : `${t("Review selected")} (${chosen.length})`}
        </button>
      </div>
      <ErrorBox error={prs.error ?? review.error} />
      {started.length ? (
        <div className="card small">
          {started.map((s) => (
            <div key={`${s.repo}#${s.number}`}>
              {s.repo}#{s.number}:{" "}
              {s.run === "skip" ? <span className="muted">skipped ({s.reason})</span> : `${s.run} started; the draft will appear in your inbox`}
            </div>
          ))}
        </div>
      ) : null}
      {prs.isLoading ? (
        <Loading />
      ) : !prs.data?.length ? (
        <Empty>No open PRs match.</Empty>
      ) : (
        <div className="card table-wrap">
          <table>
            <thead>
              <tr>
                <th>
                  <input
                    type="checkbox"
                    aria-label="Select all"
                    checked={allChosen}
                    onChange={() => setSelected(allChosen ? new Set() : new Set(reviewable.map(keyOf)))}
                  />
                </th>
                <th>{t("Tier")}</th>
                <th className="num">{t("Total")}</th>
                <th>{t("Title")}</th>
                <th>{t("Author")}</th>
                <th>{t("Review state")}</th>
                <th>{t("Review")}</th>
                <th className="hide-narrow">{t("Labels")}</th>
                <th className="hide-narrow">{t("Claimed by")}</th>
                <th className="hide-narrow">{t("Summary")}</th>
              </tr>
            </thead>
            <tbody>
              {prs.data.map((p) => (
                <tr key={keyOf(p)}>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`Select ${keyOf(p)}`}
                      checked={selected.has(keyOf(p))}
                      disabled={p.review_state === "UP_TO_DATE"}
                      onChange={() => toggle(p)}
                    />
                  </td>
                  <td>
                    <Tier tier={p.tier} />
                  </td>
                  <td className="num">{p.total?.toFixed(1) ?? "—"}</td>
                  <td>
                    <Link to={prPath(p.repo, p.number)}>{p.title}</Link>
                    <div className="muted small">
                      {p.repo}#{p.number} · {p.head}
                      {p.draft ? " · draft" : ""} · <When at={p.updated_at} />
                    </div>
                  </td>
                  <td>{p.author}</td>
                  <td className="small">{p.review_state}</td>
                  <td className="small">
                    <Activity a={p.review_activity} me={me.login} />
                  </td>
                  <td className="hide-narrow">
                    <Labels labels={p.labels} />
                  </td>
                  <td className="hide-narrow">{p.claimed_by ?? <span className="muted">—</span>}</td>
                  <td className="hide-narrow small">{p.summary}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function Activity({ a, me }: { a: ReviewActivity; me: string }) {
  const job = a.job;
  if (job && (job.status === "queued" || job.status === "running")) {
    return <span className="pill status-info">{job.playbook === "followup" ? "following up…" : "reviewing…"}</span>;
  }
  if (a.draft) {
    return (
      <Link to="/inbox" className="pill status-warn">
        draft{a.draft.for ? ` for ${a.draft.for === me ? "you" : a.draft.for}` : ""}
      </Link>
    );
  }
  if (job && job.status === "failed") {
    return <span className="pill status-bad" title={job.error ?? ""}>failed</span>;
  }
  if (a.posted) {
    return <span className="pill status-ok">posted at {a.posted.sha}</span>;
  }
  return <span className="muted">—</span>;
}
