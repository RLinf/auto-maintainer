// Proposals: every action that waits for a maintainer, showing exactly what will be
// posted, editable before approval, with filters, sorting, grouping, and bulk approval.

import { Fragment } from "react";
import { api, type Me, type ProposalWithAction } from "../api";
import { useAgent } from "../agent";
import { CaughtUp, ErrorBox, PageHead, timeOf, useAct } from "../components";
import { useProposals } from "../data";
import { useT } from "../i18n";
import { Icon } from "../icons";
import { BulkBar, ListToolbar, useList } from "../list";
import { kindLabel, ProposalCard, PROPOSAL_KEYS } from "../proposal";
import { Skeleton, Tabs, useTab, useToast } from "../ui";

export { useProposals } from "../data";

export default function InboxPage({ me }: { me: Me }) {
  const t = useT();
  const toast = useToast();
  const agent = useAgent();
  const proposals = useProposals();
  const rows = proposals.data ?? [];
  const mine = rows.filter((p) => p.requested_by === me.login);
  const tabs = [
    { id: "all", label: t("All"), count: rows.length },
    { id: "mine", label: t("Your review drafts"), count: mine.length },
    { id: "service", label: t("From the service"), count: rows.length - mine.length },
  ];
  const [tab, setTab] = useTab(tabs);
  const source = tab === "mine" ? mine : tab === "service" ? rows.filter((p) => p.requested_by !== me.login) : rows;
  const list = useList<ProposalWithAction>({
    rows: source,
    key: (p) => p.id,
    search: (p) =>
      `${p.repo} ${p.action?.action.kind ?? ""} ${kindLabel(p.action?.action.kind ?? "")} ${p.action?.action.rationale ?? ""} #${p.action?.action.number ?? ""} ${String(p.action?.action.payload.body ?? "")}`,
    facets: [
      { id: "repo", label: t("Repository"), values: (p) => p.repo },
      { id: "kind", label: t("Action"), values: (p) => t(kindLabel(p.action?.action.kind ?? "unknown")) },
      { id: "warn", label: t("Warnings"), values: (p) => (p.warnings.length ? t("Has warnings") : t("No warnings")) },
    ],
    sorts: [
      { id: "new", label: t("Created"), value: (p) => timeOf(p.created_at), descFirst: true },
      { id: "repo", label: t("Repository"), value: (p) => p.repo },
      { id: "kind", label: t("Action"), value: (p) => p.action?.action.kind ?? "" },
    ],
    defaultSort: "-new",
    groups: [
      { id: "repo", label: t("Repository"), value: (p) => p.repo },
      { id: "kind", label: t("Action"), value: (p) => t(kindLabel(p.action?.action.kind ?? "unknown")) },
      { id: "pr", label: t("Pull request"), value: (p) => (p.action?.action.number != null ? `${p.repo}#${p.action.action.number}` : p.repo) },
    ],
    pageSize: 25,
  });
  const bulk = useAct(async (ids: string[]) => {
    for (const id of ids) await api.post(`/api/proposals/${id}/approve`, {});
    return ids.length;
  }, PROPOSAL_KEYS);

  const groups = list.grouped ?? [["", list.visible] as [string, ProposalWithAction[]]];
  return (
    <>
      <PageHead
        title={t("Proposals")}
        sub={t("What the service wants to do and needs a person to approve. What you see is exactly what will be posted.")}
        actions={
          <button className="agent" onClick={() => agent.open()}>
            <Icon name="sparkles" />
            {t("Ask about these")}
          </button>
        }
      />
      <Tabs tabs={tabs} active={tab} onChange={setTab} />
      <ListToolbar list={list} placeholder={t("Search proposals")} />
      <ErrorBox error={proposals.error || bulk.error} />
      {proposals.isLoading ? (
        <div className="card">
          <Skeleton rows={6} />
        </div>
      ) : !list.count ? (
        <div className="card">
          <CaughtUp>{list.filtering ? t("Nothing matches these filters.") : t("No proposals wait for you.")}</CaughtUp>
        </div>
      ) : (
        <div className="stack">
          {groups.map(([group, items]) => (
            <Fragment key={group}>
              {group && (
                <div className="row" style={{ marginTop: 6 }}>
                  <h2>{group}</h2>
                  <span className="muted small">{items.length}</span>
                  <button className="ghost sm right" onClick={() => list.selectAll(items)}>
                    {t("Select all")}
                  </button>
                </div>
              )}
              {items.map((p) => (
                <ProposalCard key={p.id} proposal={p} selected={list.isSelected(p)} onSelect={() => list.toggle(p)} />
              ))}
            </Fragment>
          ))}
          {!list.grouped && list.pages > 1 && (
            <div className="row" style={{ justifyContent: "center" }}>
              <button className="sm" disabled={list.page === 0} onClick={() => list.setPage(list.page - 1)}>
                {t("Previous")}
              </button>
              <span className="small muted">
                {list.page + 1} / {list.pages}
              </span>
              <button className="sm" disabled={list.page >= list.pages - 1} onClick={() => list.setPage(list.page + 1)}>
                {t("Next")}
              </button>
            </div>
          )}
        </div>
      )}
      <BulkBar count={list.selected.length} onClear={list.clearSelection}>
        <button
          className="primary sm"
          disabled={bulk.isPending}
          onClick={() =>
            bulk.mutate(
              list.selected.map((p) => p.id),
              {
                onSuccess: (n) => {
                  list.clearSelection();
                  toast({ title: `${n} ${t("proposals approved")}`, icon: "check" });
                },
              },
            )
          }
        >
          <Icon name="check" size={14} />
          {bulk.isPending ? t("Approving…") : `${t("Approve")} ${list.selected.length}`}
        </button>
      </BulkBar>
    </>
  );
}
