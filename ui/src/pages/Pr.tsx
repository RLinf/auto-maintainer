// One PR: snapshot, review state, internal evaluation with override, merge checklist,
// and the service's timeline of actions.

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, type Evaluation, type PrDetail } from "../api";
import { Empty, ErrorBox, Labels, Loading, Pre, Section, Status, Tier, useAct, When } from "../components";
import { useT } from "../i18n";

const TIERS = ["P0", "P1", "P2", "P3"];

export default function PrPage() {
  const t = useT();
  const { owner, repo, number } = useParams();
  const base = `/api/prs/${owner}/${repo}/${number}`;
  const detail = useQuery({ queryKey: ["pr", owner, repo, number], queryFn: () => api.get<PrDetail>(base) });
  const keys = [["pr"], ["prs"]];
  const claim = useAct(() => api.post(`${base}/claim`), keys);
  const unclaim = useAct(() => api.del(`${base}/claim`), keys);
  const review = useAct(() => api.post(`${base}/review`), keys);
  const arm = useAct(() => api.post(`${base}/merge`), keys);
  const disarm = useAct(() => api.del(`${base}/merge`), keys);

  if (detail.isLoading) return <Loading />;
  if (detail.error) return <ErrorBox error={detail.error} />;
  const d = detail.data!;
  const pr = d.pr;
  const mutationError = claim.error || unclaim.error || review.error || arm.error || disarm.error;

  return (
    <>
      <div className="stack">
        <div className="row">
          <h1 style={{ margin: 0 }}>{pr.title}</h1>
          <a href={pr.url} target="_blank" rel="noreferrer" className="small">
            {pr.repo}#{pr.number} on GitHub ↗
          </a>
        </div>
        <div className="row small muted">
          <span>by {pr.author}</span>
          <span>
            head <span className="mono">{pr.head_sha.slice(0, 7)}</span> into {pr.base_ref}
          </span>
          {pr.is_draft && <span className="pill status-muted">draft</span>}
          <Status value={pr.state.toLowerCase()} />
          <Labels labels={pr.labels} />
        </div>
        <div className="row">
          <button onClick={() => claim.mutate(undefined)}>{t("Claim")}</button>
          <button onClick={() => unclaim.mutate(undefined)}>{t("Unclaim")}</button>
          <button onClick={() => review.mutate(undefined)}>{t("Request review")}</button>
          {d.arm ? (
            <button className="danger" onClick={() => disarm.mutate(undefined)}>
              {t("Cancel merge")}
            </button>
          ) : (
            <button onClick={() => arm.mutate(undefined)}>{t("Arm merge")}</button>
          )}
          {review.isSuccess && <span className="muted small">Review requested.</span>}
        </div>
        <ErrorBox error={mutationError} />
      </div>

      <div className="grid2">
        <Section title={t("Review state")}>
          <div className="stack">
            <span>
              <strong>{d.review_state.state}</strong>
              {d.review_state.reviewed_sha && (
                <span className="muted"> · last reviewed at <span className="mono">{d.review_state.reviewed_sha.slice(0, 7)}</span></span>
              )}
            </span>
            <span className="small muted">{pr.files.length} files changed</span>
            <ul className="small mono" style={{ margin: 0, paddingLeft: 16 }}>
              {pr.files.slice(0, 30).map((f) => (
                <li key={f.path}>
                  {f.path} <span className="muted">+{f.additions}/−{f.deletions}</span>
                </li>
              ))}
            </ul>
          </div>
        </Section>
        <EvaluationCard base={base} evaluation={d.evaluation} />
      </div>

      <div className="grid2">
        <Section title={t("Merge checklist")} aside={d.arm ? <span className="small muted">armed by {d.arm.armed_by} at <span className="mono">{d.arm.sha.slice(0, 7)}</span></span> : null}>
          {d.gate ? (
            <>
              <div className="small">
                Outcome: <strong>{d.gate.outcome}</strong>
              </div>
              <ul className="checklist small">
                {d.gate.items.map((i) => (
                  <li key={i.name}>
                    <span>{i.ok ? "✅" : i.pending ? "⏳" : "❌"}</span>
                    <span>
                      {i.name.replace(/_/g, " ")}
                      {i.detail && <span className="muted">: {i.detail}</span>}
                    </span>
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <Empty>Not armed. Arm a merge to see the checklist.</Empty>
          )}
        </Section>
        <Section title={t("Hygiene")}>
          {d.hygiene?.results?.length ? (
            <ul className="checklist small">
              {d.hygiene.results
                .filter((r) => !r.skipped)
                .map((r) => (
                  <li key={r.id}>
                    <span>{r.passed ? "✅" : r.blocking ? "❌" : "⚠️"}</span>
                    <span>
                      {r.summary}
                      {r.details.length > 0 && <div className="muted">{r.details.join(", ")}</div>}
                    </span>
                  </li>
                ))}
            </ul>
          ) : (
            <Empty>No hygiene results yet.</Empty>
          )}
        </Section>
      </div>

      <Section title={t("Timeline")}>
        {d.actions.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>When</th>
                  <th>Action</th>
                  <th>Status</th>
                  <th className="hide-narrow">Decision</th>
                  <th>Proposal</th>
                  <th className="hide-narrow">Rationale / error</th>
                </tr>
              </thead>
              <tbody>
                {d.actions.map((a) => (
                  <tr key={a.action.id}>
                    <td className="small">
                      <When at={a.action.created_at} />
                    </td>
                    <td className="mono">{a.action.kind}</td>
                    <td>
                      <Status value={a.status} />
                    </td>
                    <td className="small hide-narrow">{a.decision ? `${a.decision.kind} · ${a.decision.rule}` : "—"}</td>
                    <td>
                      {a.proposal ? (
                        <Link to="/inbox">
                          <Status value={a.proposal.state} />
                        </Link>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                    <td className="small hide-narrow">
                      {a.error ? <span style={{ color: "var(--bad)" }}>{a.error}</span> : a.action.rationale}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>The service has not acted on this PR yet.</Empty>
        )}
      </Section>

      <div className="grid2">
        <Section title={t("Reviews")}>
          {d.reviews.length ? (
            d.reviews.map((r, i) => (
              <details key={i}>
                <summary className="small">
                  {r.kind} at <span className="mono">{r.sha.slice(0, 7)}</span> · <When at={r.created_at} />
                </summary>
                <Pre text={String(r.result.body ?? JSON.stringify(r.result, null, 2))} />
              </details>
            ))
          ) : (
            <Empty>No reviews posted by the service.</Empty>
          )}
        </Section>
        <Section title={t("CI")}>
          {d.ci.length ? (
            <table>
              <tbody>
                {d.ci.map((o, i) => (
                  <tr key={i}>
                    <td className="small">{o.check_name}</td>
                    <td>
                      <Status value={o.category} />
                    </td>
                    <td className="small">
                      {o.signature && <span className="mono">{o.signature} · </span>}
                      {o.explanation}
                      {o.rerun && <span className="muted"> · rerun</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>No CI diagnoses.</Empty>
          )}
        </Section>
      </div>
    </>
  );
}

function EvaluationCard({ base, evaluation }: { base: string; evaluation: Evaluation | null }) {
  const t = useT();
  const [scores, setScores] = useState<Record<string, string>>({});
  const [boost, setBoost] = useState("");
  const [tier, setTier] = useState("");
  const [reason, setReason] = useState("");
  useEffect(() => {
    setScores(Object.fromEntries(Object.entries(evaluation?.scores ?? {}).map(([k, v]) => [k, String(v)])));
    setBoost(evaluation ? String(evaluation.manual_boost) : "");
    setTier(evaluation?.tier_override ?? "");
  }, [evaluation]);
  const override = useAct(
    () => {
      const body: Record<string, unknown> = { reason };
      const parsed = Object.fromEntries(Object.entries(scores).map(([k, v]) => [k, Number(v)]));
      if (Object.keys(parsed).length) body.scores = parsed;
      if (boost !== "") body.manual_boost = Number(boost);
      body.tier = tier;
      return api.post<Evaluation>(`${base}/override`, body);
    },
    [["pr"], ["prs"]],
  );

  return (
    <Section title={`${t("Evaluation")} · ${t("Internal")}`} aside={evaluation ? <Tier tier={evaluation.tier} /> : null}>
      {evaluation ? (
        <div className="stack small">
          <div>
            Total <strong>{evaluation.total.toFixed(1)}</strong> = base {evaluation.base.toFixed(1)} + goals {evaluation.boost.toFixed(1)} + manual{" "}
            {evaluation.manual_boost.toFixed(1)}
          </div>
          {evaluation.summary && <div>{evaluation.summary}</div>}
          {evaluation.rationale && <div className="muted">{evaluation.rationale}</div>}
          {evaluation.matched_goals.length > 0 && <div>Stage goals: {evaluation.matched_goals.join(", ")}</div>}
          {evaluation.override_by && (
            <div className="muted">
              Overridden by {evaluation.override_by}: {evaluation.override_reason}
            </div>
          )}
        </div>
      ) : (
        <Empty>Not evaluated yet.</Empty>
      )}
      <form
        className="stack"
        style={{ marginTop: 10 }}
        onSubmit={(e) => {
          e.preventDefault();
          override.mutate(undefined);
        }}
      >
        <h3>{t("Override")}</h3>
        <div className="filters">
          {Object.keys(scores).map((dim) => (
            <label key={dim} className="field">
              {dim}
              <input
                type="number"
                min={0}
                max={10}
                step={0.5}
                value={scores[dim]}
                onChange={(e) => setScores({ ...scores, [dim]: e.target.value })}
                style={{ width: 80 }}
              />
            </label>
          ))}
          <label className="field">
            manual boost
            <input type="number" step={0.5} value={boost} onChange={(e) => setBoost(e.target.value)} style={{ width: 80 }} />
          </label>
          <label className="field">
            {t("Tier")}
            <select value={tier} onChange={(e) => setTier(e.target.value)}>
              <option value="">computed</option>
              {TIERS.map((x) => (
                <option key={x}>{x}</option>
              ))}
            </select>
          </label>
        </div>
        <label className="field">
          {t("Reason")} (required)
          <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Why this priority changes" />
        </label>
        <div className="row">
          <button className="primary" disabled={!reason.trim() || override.isPending}>
            {t("Override")}
          </button>
          {override.isSuccess && <span className="muted small">Saved. The calibration agent will reply in Chat.</span>}
        </div>
        <ErrorBox error={override.error} />
      </form>
    </Section>
  );
}
