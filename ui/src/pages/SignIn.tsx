// Sign-in: authorizing the GitHub CLI with a one-time code on github.com, the only way to
// sign in. Its token reaches every repository the person can. When the host has no
// GitHub CLI, the page installs it first.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useT } from "../i18n";
import { Logo } from "../logo";
import { GhInstall, GhWebLogin } from "./Setup";

interface AuthConfig {
  gh?: boolean;
  gh_missing?: boolean;
  dev_login?: boolean;
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
        {t("Sign in with a GitHub account that has write access to a repository this service maintains.")}
      </p>
      {expired && <div className="error">{t("That sign-in link has expired or was used. Run `am login` again for a new one.")}</div>}
      {config.data?.gh && (
        <GhWebLogin
          start="/auth/gh/start"
          poll="/auth/gh/poll"
          body={t(
            "Enter a one-time code on github.com to authorize the GitHub CLI. What you approve here then runs as you on every repository your account can reach. The token stays on this host, under your login only, and is deleted when you sign out.",
          )}
          onDone={() => void client.invalidateQueries()}
        />
      )}
      {config.data?.gh_missing && <GhInstall path="/auth/gh/install" onDone={() => void client.invalidateQueries({ queryKey: ["auth-config"] })} />}
    </div>
  );
}
