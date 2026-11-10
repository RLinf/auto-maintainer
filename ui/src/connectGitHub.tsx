// Signing in with GitHub after signing in another way (a one-time link from the host):
// what a person approves as themselves (an approval, a resolved comment, a fix) needs
// their own GitHub sign-in, which only GitHub's one-time code gives the service. The
// token stays on the host, under their login only, and goes when they sign out.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";
import { useT } from "./i18n";
import { GhWebLogin } from "./pages/Setup";

/** A compact GitHub sign-in, shown where something needs to be done as the person. */
export function ConnectGitHub({ why }: { why?: string }) {
  const t = useT();
  const client = useQueryClient();
  const config = useQuery({ queryKey: ["auth-config"], queryFn: () => api.get<{ gh?: boolean }>("/auth/config"), staleTime: 60_000 });
  if (!config.data?.gh) return <span className="small muted">{why ?? t("Sign in with GitHub, so this is done as you.")}</span>;
  return (
    <GhWebLogin
      start="/auth/gh/start"
      poll="/auth/gh/poll"
      title={t("Sign in with GitHub")}
      body={why ?? t("Enter a one-time code on github.com, so what you approve here is done as you. The token stays on this host, under your login only.")}
      onDone={() => void client.invalidateQueries()}
    />
  );
}
