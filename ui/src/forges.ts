// The forges this service serves (design 5.16): how a repository's or a person's name
// says which forge it is on, and links to them there. Names on the default forge carry
// no prefix; elsewhere they begin with the forge's id and a colon
// (`corp:group/project`, `corp:alice`).

import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { api } from "./api";

export interface ForgeTerms {
  change: string;
  change_short: string;
  change_number_prefix: string;
  forge_name: string;
}

export interface ForgeInfo {
  id: string;
  kind: string;
  web_url: string;
  terms: ForgeTerms;
  change_url: string;
  file_url: string;
  tree_url: string;
  contributors_url: string;
  user_url: string;
  /** Empty when the forge has no avatar address by login. */
  avatar_url: string;
}

const GITHUB: ForgeInfo = {
  id: "github",
  kind: "github",
  web_url: "https://github.com",
  terms: { change: "pull request", change_short: "PR", change_number_prefix: "#", forge_name: "GitHub" },
  change_url: "{web}/{path}/pull/{number}",
  file_url: "{web}/{path}/blob/{ref}/{file}",
  tree_url: "{web}/{path}/tree/{ref}/{file}",
  contributors_url: "{web}/{path}/graphs/contributors",
  user_url: "{web}/{login}",
  avatar_url: "https://avatars.githubusercontent.com/{login}?s={size}",
};

let known: { default: string; forges: ForgeInfo[] } = { default: "github", forges: [GITHUB] };

/** Load the forges once, when the dashboard starts. */
export function useForgesLoader(): void {
  const q = useQuery({ queryKey: ["forges"], queryFn: () => api.get<{ default: string; forges: ForgeInfo[] }>("/api/forges"), staleTime: Infinity });
  useEffect(() => {
    if (q.data?.forges.length) known = q.data;
  }, [q.data]);
}

function split(name: string): [string, string] {
  const colon = name.indexOf(":");
  return colon < 0 ? [known.default, name] : [name.slice(0, colon), name.slice(colon + 1)];
}

/** The forge a repository or person name is on. */
export function forgeOf(name: string): ForgeInfo {
  const [id] = split(name);
  return known.forges.find((f) => f.id === id) ?? { ...GITHUB, id };
}

/** The name as the forge itself spells it: the path or login without the prefix. */
export function bare(name: string): string {
  return split(name)[1];
}

function fill(template: string, forge: ForgeInfo, fields: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (_, key: string) => (key === "web" ? forge.web_url.replace(/\/+$/, "") : String(fields[key] ?? "")));
}

export const links = {
  change: (repo: string, number: number | string) => fill(forgeOf(repo).change_url, forgeOf(repo), { path: bare(repo), number }),
  file: (repo: string, file: string, ref = "HEAD") => fill(forgeOf(repo).file_url, forgeOf(repo), { path: bare(repo), file: file.replace(/^\/+/, ""), ref }),
  tree: (repo: string, file: string, ref = "HEAD") => fill(forgeOf(repo).tree_url, forgeOf(repo), { path: bare(repo), file: file.replace(/^\/+/, ""), ref }),
  contributors: (repo: string) => fill(forgeOf(repo).contributors_url, forgeOf(repo), { path: bare(repo) }),
  user: (login: string) => fill(forgeOf(login).user_url, forgeOf(login), { login: encodeURIComponent(bare(login)) }),
  /** Empty when the person's forge has no avatar address by login. */
  avatar: (login: string, size: number) => {
    const forge = forgeOf(login);
    return forge.avatar_url ? fill(forge.avatar_url, forge, { login: encodeURIComponent(bare(login)), size }) : "";
  },
};
