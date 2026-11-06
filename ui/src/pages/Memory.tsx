// Memory: changes waiting for the memory gate, and a browser over the memory repository.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, qs, type MemoryChange } from "../api";
import { CaughtUp, Empty, ErrorBox, PageHead, Pre, RejectButton, Section, Status, timeOf, useAct, When } from "../components";
import { useMemoryChanges } from "../data";
import { ChangeProfiles } from "../profileView";
import { useT } from "../i18n";
import { Icon } from "../icons";
import { ListToolbar, useList } from "../list";
import { Skeleton, Tabs, useTab, useToast } from "../ui";

const KEYS = [["memory"], ["profile"], ["repos"], ["settings"]];

const TIER_LABEL: Record<string, string> = { T0: "Journal", T1: "Knowledge", T2: "Configuration" };

export default function MemoryPage() {
  const t = useT();
  const changes = useMemoryChanges();
  const tabs = [
    { id: "changes", label: t("Waiting for approval"), count: changes.data?.length },
    { id: "files", label: t("Browse memory") },
  ];
  const [tab, setTab] = useTab(tabs);
  const list = useList<MemoryChange>({
    rows: changes.data ?? [],
    key: (c) => c.id,
    search: (c) => `${c.title} ${c.description} ${c.author} ${c.repos.join(" ")} ${Object.keys(c.files).join(" ")}`,
    facets: [
      { id: "tier", label: t("Kind"), values: (c) => t(TIER_LABEL[c.tier] ?? c.tier) },
      { id: "repo", label: t("Repository"), values: (c) => c.repos },
      { id: "author", label: t("Author"), values: (c) => c.author },
    ],
    sorts: [
      { id: "new", label: t("Created"), value: (c) => timeOf(c.created_at), descFirst: true },
      { id: "title", label: t("Title"), value: (c) => c.title },
    ],
    defaultSort: "-new",
  });
  return (
    <>
      <PageHead
        title={t("Memory")}
        sub={t("What the service has learned and how each repository is configured, kept in the memory repository. Changes merge once the maintainers they affect approve.")}
      />
      <Tabs tabs={tabs} active={tab} onChange={setTab} />
      {tab === "changes" ? (
        <>
          <ListToolbar list={list} placeholder={t("Search memory changes")} />
          <ErrorBox error={changes.error} />
          {changes.isLoading ? (
            <div className="card">
              <Skeleton rows={5} />
            </div>
          ) : !list.count ? (
            <div className="card">
              <CaughtUp>{list.filtering ? t("Nothing matches these filters.") : t("No memory changes wait for approval.")}</CaughtUp>
            </div>
          ) : (
            <div className="stack">
              {list.visible.map((c) => (
                <ChangeCard key={c.id} change={c} />
              ))}
            </div>
          )}
        </>
      ) : (
        <FileBrowser />
      )}
    </>
  );
}

export function ChangeCard({ change, compact = false }: { change: MemoryChange; compact?: boolean }) {
  const t = useT();
  const toast = useToast();
  const approve = useAct(() => api.post<{ state: string }>(`/api/memory/changes/${change.id}/approve`), KEYS);
  const reject = useAct((reason: string) => api.post(`/api/memory/changes/${change.id}/reject`, { reason }), KEYS);
  const approvals = Object.entries(change.approvals);
  const done = approve.isSuccess || reject.isSuccess;
  return (
    <div className="proposal">
      <div className="row">
        <span className="pill status-info">{t(TIER_LABEL[change.tier] ?? change.tier)}</span>
        <strong>{change.title}</strong>
        <Status value={approve.data?.state ?? (reject.isSuccess ? "rejected" : change.state)} />
        <span className="muted small right">
          {change.author}
          {change.requested_by ? ` ${t("for")} ${change.requested_by}` : ""} · <When at={change.created_at} />
        </span>
      </div>
      {change.description && !compact && (
        <div className="small muted" style={{ whiteSpace: "pre-wrap" }}>
          {change.description}
        </div>
      )}
      <div className="small">
        {change.repos.length > 0 && (
          <>
            {t("Affects")} <strong>{change.repos.join(", ")}</strong> ·{" "}
          </>
        )}
        {t("Approved by")}: {approvals.length ? approvals.map(([repo, logins]) => `${logins.join(", ")} (${repo})`).join("; ") : t("none yet")}
      </div>
      {!compact && <ChangeProfiles change={change} />}
      <details>
        <summary className="small">
          {Object.keys(change.files).length} {t("files")}
        </summary>
        <div className="stack" style={{ marginTop: 8 }}>
          {Object.entries(change.files).map(([path, text]) => (
            <div key={path} className="stack" style={{ gap: 4 }}>
              <span className="mono">
                {path}
                {text === null ? ` (${t("deleted")})` : ""}
              </span>
              {text !== null && <Pre text={text} />}
            </div>
          ))}
        </div>
      </details>
      {!done && (
        <div className="row">
          <button
            className="primary"
            disabled={approve.isPending}
            onClick={() => approve.mutate(undefined, { onSuccess: (r) => toast({ title: `${t("Memory change")}: ${r.state}`, icon: "check" }) })}
          >
            <Icon name="check" size={14} />
            {t("Approve")}
          </button>
          <RejectButton busy={reject.isPending} onReject={(reason) => reject.mutate(reason)} />
        </div>
      )}
      <ErrorBox error={approve.error || reject.error} />
    </div>
  );
}

function FileBrowser() {
  const t = useT();
  const [path, setPath] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const tree = useQuery({ queryKey: ["memory", "tree"], queryFn: () => api.get<string[]>("/api/memory/tree") });
  const file = useQuery({
    queryKey: ["memory", "file", path],
    queryFn: () => api.get<{ path: string; text: string }>(`/api/memory/file${qs({ path })}`),
    enabled: !!path,
  });
  const files = (tree.data ?? []).filter((f) => f.toLowerCase().includes(filter.toLowerCase()));
  return (
    <div className="split" style={{ gridTemplateColumns: "320px minmax(0, 1fr)" }}>
      <Section title={t("Files")} hint={`${files.length}`}>
        <label className="search" style={{ minWidth: 0, width: "100%", marginBottom: 8 }}>
          <Icon name="search" />
          <input placeholder={t("Filter files")} value={filter} onChange={(e) => setFilter(e.target.value)} />
        </label>
        <ErrorBox error={tree.error} />
        <div className="list" style={{ maxHeight: "60vh", overflowY: "auto" }}>
          {files.map((f) => (
            <button
              key={f}
              className="menu-item"
              style={{ height: 30, fontFamily: "var(--mono)", fontSize: 12, background: f === path ? "var(--accent-soft)" : undefined }}
              onClick={() => setPath(f)}
            >
              <span className="truncate">{f}</span>
            </button>
          ))}
          {!files.length && <span className="small muted">{t("No files.")}</span>}
        </div>
      </Section>
      <Section title={path ?? t("Pick a file")}>
        {file.isLoading && path ? <Skeleton rows={8} /> : file.data ? <Pre text={file.data.text} /> : <Empty icon="book">{t("Pick a file on the left.")}</Empty>}
        <ErrorBox error={file.error} />
      </Section>
    </div>
  );
}
