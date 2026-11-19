// How a deploy of the service's own code checks the commit first
// (``SystemSettings.deploy_checks``): wait for its CI on GitHub (passed deploys at
// once; failed stops; none, cancelled, or too slow tests here instead), test it on
// this host (the default), or deploy it untested. Only the owner changes it.

import { api } from "./api";
import { useAct } from "./components";
import { useSettings } from "./data";
import { useT } from "./i18n";
import { useToast } from "./ui";

export type DeployChecks = "ci" | "local" | "none";

export const DEPLOY_CHECKS: {
  id: DeployChecks;
  label: string;
  hint: string;
}[] = [
  {
    id: "ci",
    label: "Wait for GitHub CI",
    hint: "Passed deploys without testing here; failed stops the deploy; no CI, a cancelled run, or none within 45 minutes tests here instead.",
  },
  {
    id: "local",
    label: "Test here first (default)",
    hint: "Runs the tests in a staging worktree on this host, up to half an hour.",
  },
  {
    id: "none",
    label: "Deploy without testing",
    hint: "am supervise still rolls back a version that fails to start.",
  },
];

export function DeployChecksSelect({ compact = false }: { compact?: boolean }) {
  const t = useT();
  const toast = useToast();
  const settings = useSettings();
  const set = useAct(
    (mode: DeployChecks | null) =>
      api.put<{ approved: boolean }>("/api/settings/deploy-checks", { mode }),
    [["settings"], ["memory"]],
  );
  const v = settings.data;
  if (!v) return null;
  const mode = (v.system.deploy_checks ?? "local") as DeployChecks;
  const chosen = DEPLOY_CHECKS.find((c) => c.id === mode)!;
  return (
    <span
      className="row"
      style={{ gap: 6, flexWrap: "wrap" }}
      title={t(chosen.hint)}
    >
      {compact && <span className="small muted">{t("Before deploying")}:</span>}
      <select
        value={mode}
        disabled={!v.access.allowed || set.isPending}
        aria-label={t("Before deploying")}
        onChange={(e) =>
          set.mutate(
            (e.target.value === "local"
              ? null
              : e.target.value) as DeployChecks | null,
            {
              onSuccess: (r) =>
                toast({
                  title: r.approved ? t("Saved") : t("Change proposed"),
                  icon: r.approved ? "check" : "inbox",
                }),
            },
          )
        }
      >
        {DEPLOY_CHECKS.map((c) => (
          <option key={c.id} value={c.id}>
            {t(c.label)}
          </option>
        ))}
      </select>
    </span>
  );
}
