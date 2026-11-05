// CI health: diagnoses of failed jobs and counts per job and category over 14 days.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, qs, type CiObservation, type Me } from "../api";
import { Empty, ErrorBox, Loading, prPath, Section, Status, When } from "../components";
import { useT } from "../i18n";

export default function CiPage({ me }: { me: Me }) {
  const t = useT();
  const [repo, setRepo] = useState("");
  const ci = useQuery({
    queryKey: ["ci", repo],
    queryFn: () => api.get<{ observations: CiObservation[]; by_job: Record<string, Record<string, number>> }>(`/api/ci${qs({ repo })}`),
  });
  return (
    <>
      <div className="row">
        <h1 style={{ margin: 0 }}>{t("CI")}</h1>
        <select value={repo} onChange={(e) => setRepo(e.target.value)} aria-label={t("Repository")}>
          <option value="">{t("All")}</option>
          {me.repos.map((r) => (
            <option key={r}>{r}</option>
          ))}
        </select>
      </div>
      <ErrorBox error={ci.error} />
      {ci.isLoading ? <Loading /> : (
        <>
          <Section title="By job (14 days)">
            {Object.keys(ci.data?.by_job ?? {}).length ? (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Job</th>
                      <th className="num">flaky</th>
                      <th className="num">infra</th>
                      <th className="num">PR caused</th>
                      <th className="num">unknown</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(ci.data!.by_job).map(([job, c]) => (
                      <tr key={job}>
                        <td className="mono">{job}</td>
                        <td className="num">{c.flaky ?? 0}</td>
                        <td className="num">{c.infra ?? 0}</td>
                        <td className="num">{c.pr_caused ?? 0}</td>
                        <td className="num">{c.unknown ?? 0}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty>No failed jobs diagnosed.</Empty>
            )}
          </Section>
          <Section title="Observations">
            {ci.data?.observations.length ? (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>When</th>
                      <th>PR</th>
                      <th>Job</th>
                      <th>Category</th>
                      <th className="hide-narrow">Signature</th>
                      <th>Explanation</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[...ci.data.observations].reverse().map((o, i) => (
                      <tr key={i}>
                        <td className="small">
                          <When at={o.created_at} />
                        </td>
                        <td className="small">{o.number != null ? <Link to={prPath(o.repo, o.number)}>{o.repo}#{o.number}</Link> : o.repo}</td>
                        <td className="small">{o.workflow ? `${o.workflow} / ` : ""}{o.check_name}</td>
                        <td>
                          <Status value={o.category} />
                          {o.rerun && <span className="muted small"> rerun</span>}
                        </td>
                        <td className="mono hide-narrow">{o.signature ?? "—"}</td>
                        <td className="small">{o.explanation}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty>No observations.</Empty>
            )}
          </Section>
        </>
      )}
    </>
  );
}
