// GitHub users' avatars, by login: GitHub's avatar host serves each user's picture by
// login, so a login nobody signed in with still shows its face. It answers directly;
// github.com/<login>.png redirects there and can take many seconds from some networks.

import { useState } from "react";

export function avatarUrl(login: string, size: number): string {
  return `https://avatars.githubusercontent.com/${encodeURIComponent(login)}?s=${size * 2}`;
}

export function Avatar({ login, size = 22, title }: { login: string; size?: number; title?: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    // GitHub could not be reached, or the login has no picture: its first letter.
    return (
      <span className="avatar-img avatar-letter" style={{ width: size, height: size, fontSize: Math.round(size * 0.5) }} title={title} aria-hidden>
        {login.slice(0, 1).toUpperCase()}
      </span>
    );
  }
  return <img className="avatar-img" src={avatarUrl(login, size)} width={size} height={size} alt="" title={title} loading="lazy" onError={() => setFailed(true)} />;
}

/** A login with its avatar, inline in a line of text. */
export function Person({ login, size = 16 }: { login: string; size?: number }) {
  return (
    <span className="person-inline">
      <Avatar login={login} size={size} />
      {login}
    </span>
  );
}

/** Overlapping avatars for a tight cell, with a count for the rest. */
export function AvatarStack({ logins, max = 3, size = 20, me, title }: { logins: string[]; max?: number; size?: number; me?: string; title?: string }) {
  if (!logins.length) return null;
  const shown = logins.slice(0, max);
  return (
    <span className="avatar-stack" title={title ?? logins.join(", ")}>
      {shown.map((l) => (
        <span key={l} className={l === me ? "avatar-me" : undefined}>
          <Avatar login={l} size={size} />
        </span>
      ))}
      {logins.length > max && <span className="avatar-more">+{logins.length - max}</span>}
      {logins.length === 1 && <span className="small">{logins[0]}</span>}
    </span>
  );
}
