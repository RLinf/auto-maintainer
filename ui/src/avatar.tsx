// People's avatars, by login, from their forge (forges.ts): GitHub's avatar host serves
// each user's picture by login, so a login nobody signed in with still shows its face.
// A forge without such an address shows the login's first letter.

import { useState } from "react";
import { useAliasOf } from "./aliases";
import { links } from "./forges";

export function avatarUrl(login: string, size: number): string {
  return links.avatar(login, size * 2);
}

export function Avatar({ login, size = 22, title }: { login: string; size?: number; title?: string }) {
  const [failed, setFailed] = useState(false);
  const src = avatarUrl(login, size);
  if (failed || !src) {
    // GitHub could not be reached, or the login has no picture: its first letter.
    return (
      <span className="avatar-img avatar-letter" style={{ width: size, height: size, fontSize: Math.round(size * 0.5) }} title={title} aria-hidden>
        {login.slice(0, 1).toUpperCase()}
      </span>
    );
  }
  return <img className="avatar-img" src={src} width={size} height={size} alt="" title={title} loading="lazy" onError={() => setFailed(true)} />;
}

/** A login with its avatar and the name it goes by, inline in a line of text. */
export function Person({ login, size = 16 }: { login: string; size?: number }) {
  const alias = useAliasOf()(login);
  return (
    <span className="person-inline" title={alias ? `${alias} (${login})` : undefined}>
      <Avatar login={login} size={size} />
      <Named login={login} alias={alias} />
    </span>
  );
}

/** A name with its login beside it, or the login alone. */
export function Named({ login, alias }: { login: string; alias?: string }) {
  return alias ? (
    <>
      {alias} <span className="muted login-beside">{login}</span>
    </>
  ) : (
    <>{login}</>
  );
}

/** A PR's owners (who claimed it or was assigned to it here), as avatars; each one's
 * tooltip says how. */
export function OwnerAvatars({ owners, me, size = 20, compact, t }: { owners: { login: string; kind: string; by: string }[]; me?: string; size?: number; compact?: boolean; t: (s: string) => string }) {
  const aliasOf = useAliasOf();
  const how = (o: { kind: string; by: string }) => (o.kind === "assigned" ? `${t("assigned by")} ${aliasOf(o.by) ?? o.by}` : t("claimed"));
  const label = (l: string) => (aliasOf(l) ? `${aliasOf(l)} (${l})` : l);
  return <AvatarStack logins={owners.map((o) => o.login)} me={me} size={size} max={4} compact={compact} title={owners.map((o) => `${label(o.login)}: ${how(o)}`).join("\n")} />;
}

/** Whether ``login`` holds ``role`` among a PR's owners and reviewers. */
export function holds(people: { login: string; role: string }[], login: string, role: "owner" | "reviewer"): boolean {
  const me = login.toLowerCase();
  return people.some((p) => p.role === role && p.login.toLowerCase() === me);
}

/** Overlapping avatars; one alone carries its name too, unless ``compact``. */
export function AvatarStack({ logins, max = 3, size = 20, me, title, compact }: { logins: string[]; max?: number; size?: number; me?: string; title?: string; compact?: boolean }) {
  const aliasOf = useAliasOf();
  if (!logins.length) return null;
  const shown = logins.slice(0, max);
  const label = title ?? logins.map((l) => (aliasOf(l) ? `${aliasOf(l)} (${l})` : l)).join(", ");
  return (
    <span className="avatar-stack-wrap" title={label}>
      <span className="avatar-stack">
        {shown.map((l) => (
          <span key={l} className={l === me ? "avatar-me" : undefined}>
            <Avatar login={l} size={size} />
          </span>
        ))}
        {logins.length > max && <span className="avatar-more">+{logins.length - max}</span>}
      </span>
      {logins.length === 1 && !compact && (
        <span className="small">
          <Named login={logins[0]} alias={aliasOf(logins[0])} />
        </span>
      )}
    </span>
  );
}
