// Display names (design 5.13): the name each login goes by, such as a person's real
// name. A person sets their own from the account menu; the service's owner may name
// someone who has set none, from Settings. Everyone sees them, always beside the login,
// so a name cannot pass for someone else.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "./api";
import { Avatar } from "./avatar";
import { ErrorBox, Section, useAct } from "./components";
import { useT } from "./i18n";
import { Icon } from "./icons";

export interface AliasRow {
  login: string;
  alias: string;
  /** The person chose it themselves. */
  own: boolean;
}

export interface Aliases {
  aliases: AliasRow[];
  can_name_others: boolean;
}

export function useAliases() {
  return useQuery({ queryKey: ["aliases"], queryFn: () => api.get<Aliases>("/api/aliases"), staleTime: 5 * 60_000 });
}

/** A function from a login to the name it goes by, or ``undefined``. */
export function useAliasOf(): (login: string) => string | undefined {
  const q = useAliases();
  const map = new Map((q.data?.aliases ?? []).map((a) => [a.login.toLowerCase(), a.alias]));
  return (login: string) => map.get(login.toLowerCase());
}

/** ``login`` as people read it: "name (login)" when it has a name. */
export function useLabelOf(): (login: string) => string {
  const aliasOf = useAliasOf();
  return (login: string) => {
    const alias = aliasOf(login);
    return alias ? `${alias} (${login})` : login;
  };
}

/** Set or clear the user's own display name. */
export function OwnAliasForm({ login, onDone }: { login: string; onDone?: () => void }) {
  const t = useT();
  const aliasOf = useAliasOf();
  const [text, setText] = useState(aliasOf(login) ?? "");
  const save = useAct((alias: string | null) => api.put<Aliases>("/api/me/alias", { alias }), [["aliases"]]);
  return (
    <form
      className="stack"
      style={{ gap: 6, padding: "6px 10px" }}
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate(text.trim() || null, { onSuccess: () => onDone?.() });
      }}
    >
      <span className="small muted">{t("The name people see beside your login")}</span>
      <span className="row" style={{ gap: 6, flexWrap: "nowrap" }}>
        <input value={text} onChange={(e) => setText(e.target.value)} maxLength={40} placeholder={t("Your name")} aria-label={t("Your name")} style={{ flex: 1, minWidth: 0 }} />
        <button className="primary sm" disabled={save.isPending}>
          {t("Save")}
        </button>
      </span>
      <ErrorBox error={save.error} />
    </form>
  );
}

/** Everyone's display names; the service's owner names a login whose person has set
 * none (someone who never signs in), and a person's own name is theirs alone. */
export function NamesCard() {
  const t = useT();
  const q = useAliases();
  const [login, setLogin] = useState("");
  const [alias, setAlias] = useState("");
  const save = useAct(({ who, name }: { who: string; name: string | null }) => api.put<Aliases>(`/api/aliases/${encodeURIComponent(who)}`, { alias: name }), [["aliases"]]);
  const rows = q.data?.aliases ?? [];
  const can = !!q.data?.can_name_others;
  return (
    <Section title={t("Display names")} hint={t("The names people go by here, shown beside their logins")}>
      <div className="list">
        {rows.length === 0 && <span className="small muted">{t("Nobody has a name yet.")}</span>}
        {rows.map((a) => (
          <div key={a.login} className="list-row">
            <Avatar login={a.login} size={20} />
            <span className="main">
              <strong>{a.alias}</strong> <span className="muted mono">{a.login}</span>
            </span>
            <span className="small muted">{a.own ? t("chosen by them") : t("given by the owner")}</span>
            {can && !a.own && (
              <button className="ghost sm icon" aria-label={`${t("Remove")} ${a.alias}`} disabled={save.isPending} onClick={() => save.mutate({ who: a.login, name: null })}>
                <Icon name="x" size={13} />
              </button>
            )}
          </div>
        ))}
      </div>
      {can && (
        <form
          className="row"
          style={{ gap: 6, marginTop: 10 }}
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate({ who: login.trim().replace(/^@/, ""), name: alias.trim() }, { onSuccess: () => (setLogin(""), setAlias("")) });
          }}
        >
          <input value={login} onChange={(e) => setLogin(e.target.value)} placeholder={t("GitHub login")} spellCheck={false} className="mono" style={{ flex: 1, minWidth: 120 }} />
          <input value={alias} onChange={(e) => setAlias(e.target.value)} placeholder={t("Name")} maxLength={40} style={{ flex: 1, minWidth: 120 }} />
          <button className="primary sm" disabled={!login.trim() || !alias.trim() || save.isPending}>
            {t("Give this name")}
          </button>
        </form>
      )}
      <p className="small muted" style={{ marginBottom: 0 }}>
        {t("Everyone sets their own name from the account menu; it replaces one the owner gave.")}
      </p>
      <ErrorBox error={save.error || q.error} />
    </Section>
  );
}
