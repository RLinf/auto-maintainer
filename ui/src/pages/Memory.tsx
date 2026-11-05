// Memory: open memory changes waiting for the memory gate, and a browser over the memory repo.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, qs, type MemoryChange } from "../api";
import { Empty, ErrorBox, Loading, Pre, Section, Status, useAct, When } from "../components";
import { useT } from "../i18n";

const KEYS = [["memory"], ["profile"], ["repos"]];

export default function MemoryPage() {
  const t = useT();
  const changes = useQuery({ queryKey: ["memory", "changes"], queryFn: () => api.get<MemoryChange[]>("/api/memory/changes") });
  return (
    <>
      <h1>{t("Memory")}</h1>
      <Section title={t("Open changes")}>
        <ErrorBox error={changes.error} />
        {changes.isLoading ? <Loading /> : !changes.data?.length ? <Empty>No memory changes wait for approval.</Empty> : (
          <div className="stack">
            {changes.data.map((c) => (
              <ChangeCard key={c.id} change={c} />
            ))}
          </div>
        )}
      </Section>
      <FileBrowser />
    </>
  );
}

function ChangeCard({ change }: { change: MemoryChange }) {
  const t = useT();
  const [reason, setReason] = useState("");
  const [rejecting, setRejecting] = useState(false);
  const approve = useAct(() => api.post<{ state: string }>(`/api/memory/changes/${change.id}/approve`), KEYS);
  const reject = useAct(() => api.post(`/api/memory/changes/${change.id}/reject`, { reason }), KEYS);
  return (
    <div className="proposal">
      <div className="row">
        <span className="pill status-info">{change.tier}</span>
        <strong>{change.title}</strong>
        <Status value={change.state} />
        <span className="muted small right">
          by {change.author} · <When at={change.created_at} />
        </span>
      </div>
      {change.description && <div className="small muted" style={{ whiteSpace: "pre-wrap" }}>{change.description}</div>}
      <div className="small">
        Repositories: {change.repos.join(", ") || "—"} · Approvals:{" "}
        {Object.entries(change.approvals).map(([repo, logins]) => `${repo}: ${logins.join(", ")}`).join("; ") || "none yet"}
      </div>
      <details>
        <summary className="small">
          {t("Files")} ({Object.keys(change.files).length})
        </summary>
        {Object.entries(change.files).map(([path, text]) => (
          <div key={path} className="stack" style={{ marginTop: 6 }}>
            <span className="mono small">{path}{text === null ? " (deleted)" : ""}</span>
            {text !== null && <Pre text={text} />}
          </div>
        ))}
      </details>
      <div className="row">
        <button className="primary" disabled={approve.isPending} onClick={() => approve.mutate(undefined)}>
          {t("Approve")}
        </button>
        {rejecting ? (
          <>
            <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Reason (required)" />
            <button className="danger" disabled={!reason.trim()} onClick={() => reject.mutate(undefined)}>
              {t("Reject")}
            </button>
          </>
        ) : (
          <button className="danger" onClick={() => setRejecting(true)}>
            {t("Reject")}
          </button>
        )}
        {approve.data && <span className="small muted">Now {approve.data.state}.</span>}
      </div>
      <ErrorBox error={approve.error || reject.error} />
    </div>
  );
}

function FileBrowser() {
  const [path, setPath] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const tree = useQuery({ queryKey: ["memory", "tree"], queryFn: () => api.get<string[]>("/api/memory/tree") });
  const file = useQuery({
    queryKey: ["memory", "file", path],
    queryFn: () => api.get<{ path: string; text: string }>(`/api/memory/file${qs({ path })}`),
    enabled: !!path,
  });
  const files = (tree.data ?? []).filter((f) => f.includes(filter));
  return (
    <Section title="Files" aside={<input placeholder="Filter" value={filter} onChange={(e) => setFilter(e.target.value)} />}>
      <ErrorBox error={tree.error} />
      <div className="grid2">
        <ul className="small mono" style={{ margin: 0, paddingLeft: 0, listStyle: "none", maxHeight: 420, overflowY: "auto" }}>
          {files.map((f) => (
            <li key={f}>
              <a href="#" onClick={(e) => { e.preventDefault(); setPath(f); }} style={{ fontWeight: f === path ? 600 : 400 }}>
                {f}
              </a>
            </li>
          ))}
          {!files.length && <li className="muted">No files.</li>}
        </ul>
        <div>
          {file.isLoading && path ? <Loading /> : file.data ? <Pre text={file.data.text} /> : <Empty>Pick a file.</Empty>}
          <ErrorBox error={file.error} />
        </div>
      </div>
    </Section>
  );
}
