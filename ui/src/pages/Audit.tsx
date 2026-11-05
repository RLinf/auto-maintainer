// The audit log: every decision, execution, approval, pause, and configuration change.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, qs, type AuditEntry, type Me } from "../api";
import { Empty, ErrorBox, Loading, prPath, When } from "../components";
import { useT } from "../i18n";

export default function AuditPage({ me }: { me: Me }) {
  const t = useT();
  const [repo, setRepo] = useState("");
  const [kind, setKind] = useState("");
  const audit = useQuery({ queryKey: ["audit", repo], queryFn: () => api.get<AuditEntry[]>(`/api/audit${qs({ repo })}`) });
  const rows = (audit.data ?? []).filter((a) => !kind || a.kind === kind);
  const kinds = [...new Set((audit.data ?? []).map((a) => a.kind))].sort();
  return (
    <>
      <div className="row">
        <h1 style={{ margin: 0 }}>{t("Audit")}</h1>
        <select value={repo} onChange={(e) => setRepo(e.target.value)} aria-label={t("Repository")}>
          <option value="">{t("All")}</option>
          {me.repos.map((r) => (
            <option key={r}>{r}</option>
          ))}
        </select>
        <select value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Kind">
          <option value="">all kinds</option>
          {kinds.map((k) => (
            <option key={k}>{k}</option>
          ))}
        </select>
      </div>
      <ErrorBox error={audit.error} />
      {audit.isLoading ? <Loading /> : !rows.length ? <Empty /> : (
        <div className="card table-wrap">
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Kind</th>
                <th>Actor</th>
                <th>Target</th>
                <th>Summary</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((a) => (
                <tr key={a.id}>
                  <td className="small">
                    <When at={a.created_at} />
                  </td>
                  <td className="mono">{a.kind}</td>
                  <td className="small">{a.actor}</td>
                  <td className="small">
                    {a.repo && a.number != null ? <Link to={prPath(a.repo, a.number)}>{a.repo}#{a.number}</Link> : a.repo ?? "service"}
                  </td>
                  <td className="small">{a.summary}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
