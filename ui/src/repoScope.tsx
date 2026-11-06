// Pages that list PRs show one repository at a time: each repository ranks its PRs by its
// own rubric, so a combined list would compare scores that do not compare. The choice is
// in the URL (?repo=) and remembered in this browser, so Home and Pull requests agree.

import { useSearchParams } from "react-router-dom";
import { useT } from "./i18n";
import { Icon } from "./icons";

const KEY = "am.repo";

function remembered(): string {
  try {
    return localStorage.getItem(KEY) ?? "";
  } catch {
    return "";
  }
}

/** The repository a page shows, among ``names``: the one in the URL, else the one chosen
 * last in this browser, else the first. Empty only when there is no repository. */
export function useRepoScope(names: string[]): [string, (repo: string) => void] {
  const [params, setParams] = useSearchParams();
  const fromUrl = params.get("repo") ?? "";
  const last = remembered();
  const scope = names.includes(fromUrl) ? fromUrl : names.includes(last) ? last : (names[0] ?? "");
  const choose = (repo: string) => {
    try {
      localStorage.setItem(KEY, repo);
    } catch {
      // the choice still holds in the URL
    }
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set("repo", repo);
        return next;
      },
      { replace: true },
    );
  };
  return [scope, choose];
}

/** The switch between repositories; nothing when there is only one. ``count`` puts a
 * number beside each name (for example, PRs waiting for review). */
export function RepoTabs({ names, scope, onPick, count }: { names: string[]; scope: string; onPick: (repo: string) => void; count?: (repo: string) => number }) {
  const t = useT();
  if (names.length < 2) return null;
  return (
    <div className="row repo-switch" role="tablist" aria-label={t("Repository")}>
      {names.map((repo) => {
        const n = count?.(repo) ?? 0;
        return (
          <button key={repo} role="tab" aria-selected={scope === repo} className={`chip-btn ${scope === repo ? "on" : ""}`} onClick={() => onPick(repo)}>
            <Icon name="repo" size={13} />
            {repo}
            {n ? <span className="muted">{n}</span> : null}
          </button>
        );
      })}
    </div>
  );
}
