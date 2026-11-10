// The service's repository and its one owner (design 2.3): an admin of that
// repository, who hands ownership to another signed-in admin or moves the service to
// another fork. On a host set up before owners, any admin of the repository takes it.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, ApiError } from "./api";
import { ErrorBox, Section, Status, useAct } from "./components";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { Skeleton } from "./ui";

interface OwnerView {
  repo: string | null;
  repo_link: string | null;
  owner: string | null;
  is_owner: boolean;
  may_take: boolean;
  reason: string;
}

export function OwnerCard() {
  const t = useT();
  const q = useQuery({ queryKey: ["owner"], queryFn: () => api.get<OwnerView>("/api/settings/owner"), retry: false });
  const [to, setTo] = useState("");
  const [link, setLink] = useState("");
  const [newOwner, setNewOwner] = useState("");
  const [moving, setMoving] = useState(false);
  const take = useAct(() => api.post<{ owner: string }>("/api/settings/owner/take"), [["owner"], ["settings"]]);
  const transfer = useAct(() => api.post<{ owner: string }>("/api/settings/owner/transfer", { login: to.trim() }), [["owner"], ["settings"]]);
  const move = useAct(() => api.post<{ repo: string }>("/api/settings/owner/repo", { link: link.trim(), owner: newOwner.trim() || null }), [["owner"], ["settings"], ["service"]]);
  if (q.error instanceof ApiError && q.error.status === 404) return null;
  const v = q.data;
  return (
    <Section title={t("Owner")} hint={t("One person owns this service: an admin of its repository. Only they change its settings and its code; everyone else's changes wait for them.")}>
      {q.isLoading ? (
        <Skeleton rows={2} />
      ) : !v ? (
        <ErrorBox error={q.error} />
      ) : (
        <div className="stack" style={{ gap: 12 }}>
          <dl className="props">
            <dt>{t("Repository")}</dt>
            <dd>
              {v.repo_link ? (
                <a href={v.repo_link} target="_blank" rel="noreferrer noopener">
                  <code>{v.repo}</code> <Icon name="external" size={12} />
                </a>
              ) : (
                <span className="muted">{t("not set")}</span>
              )}
            </dd>
            <dt>{t("Owner")}</dt>
            <dd>{v.owner ? <strong>{v.owner}</strong> : <span className="muted">{t("nobody yet")}</span>}</dd>
          </dl>
          {v.may_take && (
            <div className="notice small row" style={{ gap: 8 }}>
              <span>
                {t("Nobody owns this service yet; until someone does, every admin of its repository acts as owner.")} {t("As one, you can take ownership.")}
              </span>
              <button className="primary sm" disabled={take.isPending} onClick={() => take.mutate(undefined)}>
                {t("Take ownership")}
              </button>
            </div>
          )}
          {!v.is_owner && !v.may_take && v.reason && <span className="small muted">{v.reason}</span>}
          {v.is_owner && (
            <>
              <form
                className="stack"
                style={{ gap: 6 }}
                onSubmit={(e) => {
                  e.preventDefault();
                  transfer.mutate(undefined, { onSuccess: () => setTo("") });
                }}
              >
                <span className="small muted">{t("Hand ownership to another admin of the repository who has signed in here:")}</span>
                <div className="row" style={{ gap: 8 }}>
                  <input value={to} onChange={(e) => setTo(e.target.value)} placeholder={t("their login")} spellCheck={false} style={{ flex: 1, minWidth: 180 }} />
                  <button className="sm" disabled={!to.trim() || transfer.isPending}>
                    {t("Hand over")}
                  </button>
                </div>
                <ErrorBox error={transfer.error} />
              </form>
              {!moving ? (
                <button className="link-button small" style={{ alignSelf: "flex-start", color: "var(--info)" }} onClick={() => setMoving(true)}>
                  <Icon name="chevronRight" size={13} />
                  {t("Move to another repository")}
                </button>
              ) : (
                <form
                  className="card stack"
                  style={{ gap: 8, padding: 12, background: "var(--surface-2)" }}
                  onSubmit={(e) => {
                    e.preventDefault();
                    move.mutate(undefined, { onSuccess: () => setMoving(false) });
                  }}
                >
                  <span className="small muted">
                    {t("Another fork or copy of auto-maintainer, on a forge this service serves. It must hold the code this host runs, and the service's account must be able to push to it. The service restarts with it.")}
                  </span>
                  <input value={link} onChange={(e) => setLink(e.target.value)} placeholder="https://github.com/you/auto-maintainer" spellCheck={false} />
                  <input value={newOwner} onChange={(e) => setNewOwner(e.target.value)} placeholder={t("Its owner, when you are not an admin of it or it is on another forge (corp:login)")} spellCheck={false} />
                  <ErrorBox error={move.error} />
                  <span className="row" style={{ gap: 6 }}>
                    <button className="primary sm" disabled={!link.trim() || move.isPending}>
                      {move.isPending ? t("Checking…") : t("Check and move")}
                    </button>
                    <button type="button" className="ghost sm" onClick={() => setMoving(false)}>
                      {t("Cancel")}
                    </button>
                  </span>
                </form>
              )}
              {move.data && <Status value="executed" label={`${t("Moved to")} ${move.data.repo}`} />}
            </>
          )}
          <ErrorBox error={take.error} />
        </div>
      )}
    </Section>
  );
}
