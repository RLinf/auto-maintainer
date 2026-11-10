// Sign-in: authorizing the GitHub CLI with a one-time code on github.com. Its token
// reaches every repository the person can. When the host has no GitHub CLI, the page
// installs it first. A service that also serves other forges (GitLab) offers each:
// its CLI's one-time code where the forge has one, or a personal access token.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useT } from "../i18n";
import { Logo } from "../logo";
import { GhInstall, GhWebLogin } from "./Setup";

interface AuthConfig {
  gh?: boolean;
  gh_missing?: boolean;
  dev_login?: boolean;
  forges?: { id: string; name: string; device: boolean }[];
}

/** Sign in on another forge with a personal access token. */
function TokenSignIn({ forge, onDone }: { forge: string; onDone: () => void }) {
  const t = useT();
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <form
      className="row"
      style={{ gap: 6, flexWrap: "wrap" }}
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        setError("");
        const r = await fetch(`/auth/forges/${forge}/token`, {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token }),
        });
        setBusy(false);
        if (r.ok) onDone();
        else setError(((await r.json().catch(() => ({}))) as { detail?: string }).detail ?? t("Sign-in failed"));
      }}
    >
      <input type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder={t("Personal access token (scopes api, read_user)")} autoComplete="off" style={{ flex: 1, minWidth: 220 }} />
      <button className="primary sm" disabled={!token.trim() || busy}>
        {t("Sign in")}
      </button>
      {error && <span className="small danger-text">{error}</span>}
    </form>
  );
}

export default function SignIn() {
  const t = useT();
  const client = useQueryClient();
  const config = useQuery({
    queryKey: ["auth-config"],
    queryFn: async () => (await fetch("/auth/config", { credentials: "same-origin" })).json() as Promise<AuthConfig>,
  });
  const expired = new URLSearchParams(window.location.search).get("signin") === "expired";

  return (
    <div className="signin card" style={{ padding: 28 }}>
      <div className="row" style={{ gap: 10 }}>
        <Logo size={40} />
        <h1>auto-maintainer</h1>
      </div>
      <p className="muted" style={{ margin: 0 }}>
        {config.data?.forges?.length
          ? t("Sign in with an account that has write access to a repository this service maintains, on its forge.")
          : t("Sign in with a GitHub account that has write access to a repository this service maintains.")}
      </p>
      {expired && <div className="error">{t("That sign-in link has expired or was used. Run `am login` again for a new one.")}</div>}
      {config.data?.gh && (
        <GhWebLogin
          start="/auth/gh/start"
          poll="/auth/gh/poll"
          startBody={undefined}
          body={t(
            "Enter a one-time code on github.com to authorize the GitHub CLI. What you approve here then runs as you on every repository your account can reach. The token stays on this host, under your login only, and is deleted when you sign out.",
          )}
          onDone={() => void client.invalidateQueries()}
        />
      )}
      {config.data?.gh_missing && <GhInstall path="/auth/gh/install" onDone={() => void client.invalidateQueries({ queryKey: ["auth-config"] })} />}
      {(config.data?.forges ?? []).map((f) => (
        <section key={f.id} className="stack" style={{ gap: 8, borderTop: "1px solid var(--border)", paddingTop: 12 }}>
          <strong>
            {t("Sign in with")} {f.name}
          </strong>
          {f.device && (
            <GhWebLogin
              start={`/auth/forges/${f.id}/start`}
              poll={`/auth/forges/${f.id}/poll`}
              site={f.name}
              title={`${t("Sign in with")} ${f.name}`}
              button={`${t("Sign in with")} ${f.name}`}
              body={t("Enter a one-time code on the forge's site to authorize its CLI. What you approve here then runs as you there. The token stays on this host, under your login only, and is deleted when you sign out.")}
              onDone={() => void client.invalidateQueries()}
            />
          )}
          <span className="small muted">{f.device ? t("Or paste a personal access token:") : t("Paste a personal access token:")}</span>
          <TokenSignIn forge={f.id} onDone={() => void client.invalidateQueries()} />
        </section>
      ))}
    </div>
  );
}
