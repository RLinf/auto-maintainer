// The service's own code: what it runs, what is waiting on its default branch, and the
// self-improvement PRs it opened. Only maintainers of its repository merge and deploy.

import { useQuery } from "@tanstack/react-query";
import { api, type ServiceView } from "../api";
import { Empty, ErrorBox, Loading, Status, useAct, When } from "../components";
import { useT } from "../i18n";

const KEYS = [["service"], ["audit"]];

export default function ServicePage() {
  const t = useT();
  const view = useQuery({ queryKey: ["service"], queryFn: () => api.get<ServiceView>("/api/service") });
  const merge = useAct((n: number) => api.post(`/api/service/prs/${n}/merge`), KEYS);
  const deploy = useAct((sha: string) => api.post("/api/service/deploy", { sha }), KEYS);

  if (view.isLoading) return <Loading />;
  if (view.error) return <ErrorBox error={view.error} />;
  const v = view.data!;
  if (!v.code_repo) return <Empty>No code repository is configured (code_repo in local.yaml).</Empty>;
  const s = v.status;
  const prs = v.prs ?? [];
  const mine = prs.filter((p) => p.self_improvement);

  return (
    <>
      <h1>{t("Service")}</h1>
      <p className="muted">
        Changes to the service come from reviewers' requests in chat or from people. Merging and deploying
        need {v.approve_permission} on <code>{v.code_repo}</code>.
        {!v.can_approve && <> {v.reason}</>}
      </p>
      <ErrorBox error={merge.error || deploy.error} />
      <section className="card stack">
        <h2>{t("Running")}</h2>
        {s ? (
          <>
            <div className="row">
              <code>{s.running_sha.slice(0, 12)}</code>
              <span className="muted small">{s.branch}</span>
              {s.dirty && <span className="badge">{t("local changes")}</span>}
            </div>
            {s.error && <div className="muted small">{s.error}</div>}
            {s.pending.length === 0 ? (
              <div className="muted">{t("Up to date with")} {v.branch}.</div>
            ) : (
              <>
                <div>
                  {s.pending.length} {t("commits on")} {v.branch} {t("are not running yet")}:
                </div>
                <ul className="small">
                  {s.pending.map((c) => (
                    <li key={c.sha}>
                      <code>{c.sha.slice(0, 8)}</code> {c.subject} <span className="muted">({c.author})</span>
                    </li>
                  ))}
                </ul>
                {v.can_approve && s.upstream_sha && (
                  <button className="primary" disabled={deploy.isPending || s.dirty} onClick={() => deploy.mutate(s.upstream_sha!)}>
                    {t("Deploy")} {s.upstream_sha.slice(0, 8)}
                  </button>
                )}
              </>
            )}
          </>
        ) : (
          <div className="muted">The service does not run from a git checkout it can update.</div>
        )}
      </section>
      <section className="card stack">
        <h2>{t("Self-improvement PRs")}</h2>
        {mine.length === 0 && <Empty>{t("No open self-improvement PRs.")}</Empty>}
        {mine.map((p) => (
          <div key={p.number} className="row">
            <a href={p.url} target="_blank" rel="noreferrer">
              #{p.number} {p.title}
            </a>
            <span className="muted small">{p.branch}</span>
            {v.can_approve && (
              <button disabled={merge.isPending} onClick={() => merge.mutate(p.number)}>
                {t("Approve and merge")}
              </button>
            )}
          </div>
        ))}
      </section>
      <section className="card stack">
        <h2>{t("History")}</h2>
        {(v.history ?? []).map((h, i) => (
          <div key={i} className="row small">
            <Status value={h.status} />
            <span>{h.kind}</span>
            <span>{h.summary}</span>
            {h.error && <span className="muted">{h.error.slice(0, 200)}</span>}
            <When at={h.at} />
          </div>
        ))}
      </section>
    </>
  );
}
