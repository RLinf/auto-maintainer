// What agents spent on a repository, per person whose request they worked for. Its
// maintainers see everyone; other reviewers see the total and their own share.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type AgentQuota, type AgentsUsage, type RepoUsage, type UsageRow } from "./api";
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

/** Each agent's tokens in a period, and what its CLI's own /usage says is left of its
 * account's limits. */
export function AgentsUsageSection({ mine = false }: { mine?: boolean }) {
  const t = useT();
  const [days, setDays] = useState(7);
  const [fresh, setFresh] = useState(false);
  const q = useQuery({
    queryKey: ["agents-usage", days, fresh, mine],
    queryFn: () => api.get<AgentsUsage>(`/api/agents/usage?days=${days}${fresh ? "&fresh=true" : ""}${mine ? "&mine=true" : ""}`),
  });
  const u = q.data;
  return (
    <Section
      title={mine ? t("My usage and limits") : t("Usage and limits")}
      hint={
        mine
          ? t("Tokens of the runs on your own accounts, and what each CLI says is left of them.")
          : u?.everything
            ? t("Tokens of every agent run, and what each CLI says is left.")
            : t("Tokens of runs in the repositories you see, and what each CLI says is left.")
      }
      aside={
        <span className="row" style={{ gap: 6 }}>
          <select value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label={t("Period")}>
            {PERIODS.map((d) => (
              <option key={d} value={d}>
                {t("Last")} {d} {t("days")}
              </option>
            ))}
          </select>
          <button className="ghost sm" onClick={() => (fresh ? q.refetch() : setFresh(true))} disabled={q.isFetching} title={t("Ask each CLI again")}>
            <Icon name="refresh" size={14} />
          </button>
        </span>
      }
    >
      {q.isLoading ? (
        <Skeleton rows={3} />
      ) : q.error || !u ? (
        <ErrorBox error={q.error} />
      ) : u.agents.length === 0 ? (
        <Empty icon="cpu">{t("No agents on this host yet.")}</Empty>
      ) : (
        <div className="list">
          {u.agents.map((a) => (
            <div key={a.name} className="list-row" style={{ alignItems: "flex-start", flexWrap: "wrap", gap: 16 }}>
              <span className="main" style={{ minWidth: 180 }}>
                <span className="t mono">{a.name}</span>
                <span className="meta">
                  {tokens(a.input_tokens + a.output_tokens)} {t("tokens")} · {a.runs} {t("runs")}
                  {a.cost_usd != null && ` · ${cost(a.cost_usd)}`}
                </span>
                <span className="meta">
                  {tokens(a.input_tokens)} {t("in")} · {tokens(a.output_tokens)} {t("out")}
                </span>
              </span>
              <QuotaView quota={a.quota} />
            </div>
          ))}
        </div>
      )}
    </Section>
  );
}

function QuotaView({ quota }: { quota: AgentQuota | null }) {
  const t = useT();
  if (!quota) return <span className="small muted">{t("This CLI does not report its limits.")}</span>;
  if (!quota.buckets.length) return <span className="small muted">{quota.note}</span>;
  return (
    <div className="stack" style={{ gap: 6, flex: "1 1 280px", minWidth: 0 }}>
      {quota.buckets.map((b) => {
        const left = b.remaining;
        const color = left == null ? "var(--muted)" : left > 0.5 ? "var(--ok)" : left > 0.2 ? "var(--warn)" : "var(--bad)";
        const resets = b.resets_at ? new Date(b.resets_at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : b.resets;
        return (
          <div key={`${b.group}/${b.name}`} className="stack" style={{ gap: 3 }}>
            <span className="row small" style={{ gap: 6, justifyContent: "space-between" }}>
              <span className="truncate">
                {b.group && <span className="muted">{b.group} · </span>}
                {b.name}
              </span>
              <span>
                <strong>{left == null ? "—" : `${Math.round(left * 100)}%`}</strong> {t("left")}
              </span>
            </span>
            <span className="quality-meter" style={{ ["--qc" as string]: color }}>
              <span style={{ width: `${Math.round((left ?? 0) * 100)}%` }} />
            </span>
            {resets && (
              <span className="small muted">
                {t("resets")} {resets}
              </span>
            )}
          </div>
        );
      })}
      <span className="small muted">
        {t("Read")} <When at={quota.read_at} />
      </span>
    </div>
  );
}
