// GitHub users' avatars, by login: GitHub serves each user's picture at a stable URL,
// so a login nobody signed in with still shows its face.

export function avatarUrl(login: string, size: number): string {
  return `https://github.com/${encodeURIComponent(login)}.png?size=${size * 2}`;
}

export function Avatar({ login, size = 22, title }: { login: string; size?: number; title?: string }) {
  return <img className="avatar-img" src={avatarUrl(login, size)} width={size} height={size} alt="" title={title} loading="lazy" />;
}

/** Overlapping avatars for a tight cell, with a count for the rest. */
export function AvatarStack({ logins, max = 3, size = 20, me }: { logins: string[]; max?: number; size?: number; me?: string }) {
  if (!logins.length) return null;
  const shown = logins.slice(0, max);
  return (
    <span className="avatar-stack" title={logins.join(", ")}>
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
