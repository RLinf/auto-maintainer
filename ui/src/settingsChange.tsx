// A drafted configuration change, as the maintainer applies it: what it does, the
// diff against what runs now, whether it still checks, and Apply or Discard. Used on the
// Settings page and inline in the agent's answers.

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type MemoryChange, type SettingsCheck } from "./api";
import { Diff, ErrorBox, Pre, RejectButton, Status, useAct, When } from "./components";
import { useMemoryChanges, useSettings } from "./data";
import { ChangeProfiles } from "./profileView";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { useToast } from "./ui";

const KEYS = [["settings"], ["memory"], ["profile"], ["repos"], ["chat"], ["audit"]];
const SETTINGS_PATH = "config/service.yaml";

export function ChangeById({ id }: { id: string }) {
  const t = useT();
  const settings = useSettings();
  const memory = useMemoryChanges();
  const change = settings.data?.pending.find((c) => c.id === id) ?? memory.data?.find((c) => c.id === id);
  if (settings.isLoading || memory.isLoading || (!change && (settings.isFetching || memory.isFetching))) return null;
  if (!change) {
    return (
      <span className="small muted">
        <Icon name="check" size={12} /> {t("This configuration change is no longer open.")} <Link to="/settings">{t("Settings")}</Link>
      </span>
    );
  }
  return <SettingsChangeCard change={change} compact />;
}

export function SettingsChangeCard({ change, compact = false }: { change: MemoryChange; compact?: boolean }) {
  const t = useT();
  const toast = useToast();
  const settings = useSettings();
  const doc = change.service ? change.files[SETTINGS_PATH] : null;
  const check = useQuery({
    queryKey: ["settings", "check", change.id],
    queryFn: () => api.post<SettingsCheck>("/api/settings/check", { yaml: doc ?? "" }),
    enabled: doc != null,
  });
  const approve = useAct(() => api.post<{ state: string }>(`/api/memory/changes/${change.id}/approve`), KEYS);
  const reject = useAct((reason: string) => api.post(`/api/memory/changes/${change.id}/reject`, { reason }), KEYS);
  const canApply = change.service ? settings.data?.access.allowed : undefined;
  const done = approve.isSuccess || reject.isSuccess;
  const state = approve.data?.state ?? (reject.isSuccess ? "rejected" : change.state);

  return (
    <div className="proposal" style={{ borderColor: "color-mix(in srgb, var(--accent) 35%, var(--border))" }}>
      <div className="row">
        <Icon name="settings" />
        <strong>{change.title}</strong>
        <Status value={state} />
        <span className="small muted right">
          {change.service ? t("service settings") : change.repos.join(", ")} · {change.requested_by ?? change.author} · <When at={change.created_at} />
        </span>
      </div>
      {change.service ? (
        check.isLoading ? null : check.data ? (
          <>
            {!check.data.ok && (
              <div className="error">
                {t("This draft no longer checks")}: {check.data.problems.join("; ")}
              </div>
            )}
            {compact ? (
              <details open>
                <summary className="small">{t("What changes")}</summary>
                <div style={{ marginTop: 6 }}>
                  <Diff text={check.data.diff} />
                </div>
              </details>
            ) : (
              <Diff text={check.data.diff} />
            )}
          </>
        ) : (
          <ErrorBox error={check.error} />
        )
      ) : (
        <>
          <ChangeProfiles change={change} />
          <details open={!compact && !change.profiles?.length}>
            <summary className="small">
              {Object.keys(change.files).length} {t("files")}
            </summary>
            <div className="stack" style={{ marginTop: 6 }}>
              {Object.entries(change.files)
                .filter(([path]) => !path.endsWith("profile.lock.yaml"))
                .map(([path, text]) => (
                  <div key={path} className="stack" style={{ gap: 4 }}>
                    <span className="mono">{path}</span>
                    {text !== null && <Pre text={text} />}
                  </div>
                ))}
            </div>
          </details>
        </>
      )}
      {!done && change.state === "open" && (
        <div className="row">
          <button
            className="primary"
            disabled={approve.isPending || canApply === false || (check.data ? !check.data.ok : false)}
            title={canApply === false ? settings.data?.access.reason : undefined}
            onClick={() =>
              approve.mutate(undefined, {
                onSuccess: (r) => toast({ title: r.state === "merged" ? t("Applied") : `${t("Approved")}: ${r.state}`, icon: "check" }),
              })
            }
          >
            <Icon name="check" size={14} />
            {t("Apply")}
          </button>
          <RejectButton label="Discard" size="sm" busy={reject.isPending} onReject={(reason) => reject.mutate(reason)} />
          {canApply === false && <span className="small muted">{settings.data?.access.reason}</span>}
        </div>
      )}
      <ErrorBox error={approve.error || reject.error} />
    </div>
  );
}
