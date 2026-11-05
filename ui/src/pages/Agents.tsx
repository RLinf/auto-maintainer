// Agent runtimes: health from the router and recent runs with usage.

import { useQuery } from "@tanstack/react-query";
import { api, type AgentHealth, type AgentRun } from "../api";
import { Empty, ErrorBox, Loading, Section, Status, When } from "../components";
import { useT } from "../i18n";

export default function AgentsPage() {
  const t = useT();
  const agents = useQuery({ queryKey: ["agents"], queryFn: () => api.get<{ health: AgentHealth[]; runs: AgentRun[] }>("/api/agents") });
  if (agents.isLoading) return <Loading />;
  const data = agents.data;
  const cost = (data?.runs ?? []).reduce((sum, r) => sum + (r.cost_usd ?? 0), 0);
  return (
    <>
      <h1>{t("Agents")}</h1>
      <ErrorBox error={agents.error} />
      <Section title={t("Runtime health")}>
        {data?.health.length ? (
          <table>
            <thead>
              <tr>
                <th>Runtime</th>
                <th>Available</th>
                <th>Version</th>
                <th className="hide-narrow">Structured output</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {data.health.map((h) => (
                <tr key={h.name}>
                  <td className="mono">{h.name}</td>
                  <td>
                    <Status value={h.available ? "executed" : "failed"} />
                  </td>
                  <td className="small">{h.version ?? "—"}</td>
                  <td className="small hide-narrow">{h.structured_output === null ? "—" : h.structured_output ? "yes" : "no"}</td>
                  <td className="small">{h.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>No runtimes.</Empty>
        )}
      </Section>
      <Section title={t("Recent runs")} aside={<span className="small muted">${cost.toFixed(2)} reported cost in this list</span>}>
        {data?.runs.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>When</th>
                  <th>Runtime</th>
                  <th>Task</th>
                  <th className="hide-narrow">Repository</th>
                  <th>Result</th>
                  <th className="num">Cost</th>
                  <th className="num">Seconds</th>
                </tr>
              </thead>
              <tbody>
                {data.runs.map((r) => (
                  <tr key={r.id}>
                    <td className="small">
                      <When at={r.created_at} />
                    </td>
                    <td className="mono">{r.runtime}</td>
                    <td className="small">
                      {r.task_kind}
                      {r.permissions === "bypass" ? (
                        <>
                          {" "}
                          <span className="pill status-warn" title="Ran with permission checks skipped">
                            bypass
                          </span>
                        </>
                      ) : null}
                    </td>
                    <td className="small hide-narrow">{r.repo ?? "—"}</td>
                    <td className="small">
                      <Status value={r.ok ? "executed" : "failed"} /> {r.error}
                    </td>
                    <td className="num">{r.cost_usd == null ? "—" : `$${r.cost_usd.toFixed(3)}`}</td>
                    <td className="num">{r.duration_seconds.toFixed(1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>No agent runs yet.</Empty>
        )}
      </Section>
    </>
  );
}
