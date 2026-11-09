// What agents spent on a repository, per person whose request they worked for. Its
// maintainers see everyone; other reviewers see the total and their own share.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type RepoUsage, type UsageRow } from "./api";
import { Avatar } from "./avatar";
import { Empty, ErrorBox, Section, When } from "./components";
import { useMe } from "./data";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { Skeleton } from "./ui";

const PERIODS = [7, 30, 90, 365];

function tokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
  if (n >= 10_000) return `${Math.round(n / 1000)}k`;
  return n.toLocaleString();
}

function cost(usd: number | null): string {
  return usd == null ? "—" : `$${usd.toFixed(usd >= 100 ? 0 : 2)}`;
}

/** The task kinds that took most of a row's tokens, for one line. */
function topKinds(row: UsageRow): string {
  return Object.entries(row.by_kind)
    .sort((a, b) => b[1].tokens - a[1].tokens)
    .slice(0, 3)
    .map(([k, v]) => `${k} ${tokens(v.tokens)}`)
    .join(" · ");
}

export function UsageTab({ repo }: { repo: string }) {
  const t = useT();
  const me = useMe().data?.login ?? "";
  const [days, setDays] = useState(30);
  const q = useQuery({ queryKey: ["usage", repo, days], queryFn: () => api.get<RepoUsage>(`/api/repos/${repo}/usage?days=${days}`) });
  const u = q.data;
  const all = u ? u.total.input_tokens + u.total.output_tokens : 0;

  return (
    <Section
      title={t("Agent usage")}
      hint={u?.everyone ? t("Tokens and reported cost of agent runs on this repository, by the person each run worked for.") : t("Everyone's total, and your own share. Its maintainers see each person's.")}
      aside={
        <select value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label={t("Period")}>
          {PERIODS.map((d) => (
            <option key={d} value={d}>
              {t("Last")} {d} {t("days")}
            </option>
          ))}
        </select>
      }
    >
      {q.isLoading ? (
        <Skeleton rows={4} />
      ) : q.error || !u ? (
        <ErrorBox error={q.error} />
      ) : u.total.runs === 0 ? (
        <Empty icon="activity">{t("No agent runs on this repository in this period.")}</Empty>
      ) : (
        <div className="stack" style={{ gap: 12 }}>
          <div className="tiles">
            <div className="tile">
              <span className="tile-label">
                <Icon name="activity" size={15} />
                {t("Runs")}
              </span>
              <span className="tile-value">{u.total.runs}</span>
              {u.total.failed > 0 && (
                <span className="tile-foot">
                  {u.total.failed} {t("failed")}
                </span>
              )}
            </div>
            <div className="tile">
              <span className="tile-label">
                <Icon name="cpu" size={15} />
                {t("Tokens")}
              </span>
              <span className="tile-value">{tokens(all)}</span>
              <span className="tile-foot">
                {tokens(u.total.input_tokens)} {t("in")} · {tokens(u.total.output_tokens)} {t("out")}
              </span>
            </div>
            <div className="tile">
              <span className="tile-label">
                <Icon name="server" size={15} />
                {t("Reported cost")}
              </span>
              <span className="tile-value">{cost(u.total.cost_usd)}</span>
              <span className="tile-foot">{t("only from agents that report it")}</span>
            </div>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t("Person")}</th>
                  <th className="num">{t("Runs")}</th>
                  <th className="num">{t("Tokens")}</th>
                  <th className="num">{t("Share")}</th>
                  <th className="num hide-narrow">{t("Cost")}</th>
                  <th className="hide-narrow">{t("Mostly")}</th>
                  <th className="hide-narrow">{t("Last run")}</th>
                </tr>
              </thead>
              <tbody>
                {u.people.map((r) => {
                  const used = r.input_tokens + r.output_tokens;
                  return (
                    <tr key={r.login ?? "-"}>
                      <td>
                        {r.login ? (
                          <span className="row" style={{ gap: 8 }}>
                            <Avatar login={r.login} size={22} />
                            {r.login}
                            {r.login.toLowerCase() === me.toLowerCase() && <span className="muted">({t("you")})</span>}
                          </span>
                        ) : (
                          <span className="row muted" style={{ gap: 8 }} title={t("Scans, scheduled checks, and other work nobody asked for")}>
                            <Icon name="cpu" size={18} />
                            {t("The service")}
                          </span>
                        )}
                      </td>
                      <td className="num">{r.runs}</td>
                      <td className="num" title={`${r.input_tokens.toLocaleString()} ${t("in")} · ${r.output_tokens.toLocaleString()} ${t("out")}`}>
                        {tokens(used)}
                      </td>
                      <td className="num">{all ? `${Math.round((used / all) * 100)}%` : "—"}</td>
                      <td className="num hide-narrow">{cost(r.cost_usd)}</td>
                      <td className="small muted hide-narrow">{topKinds(r)}</td>
                      <td className="small hide-narrow">{r.last_at ? <When at={r.last_at} /> : "—"}</td>
                    </tr>
                  );
                })}
                {u.people.length === 0 && (
                  <tr>
                    <td colSpan={7} className="muted small">
                      {t("No runs worked for you in this period.")}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          {!u.complete && <span className="small muted">{t("Only the most recent runs are counted; pick a shorter period for exact totals.")}</span>}
        </div>
      )}
    </Section>
  );
}
