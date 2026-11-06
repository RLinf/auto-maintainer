// The command palette (Cmd/Ctrl+K): jump to any page, repository, or PR, run a command,
// or hand the typed text to the agent.

import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { Me } from "./api";
import { useAgent } from "./agent";
import { prPath } from "./components";
import { usePrs, useRepos } from "./data";
import { useT } from "./i18n";
import { Icon, type IconName } from "./icons";

export interface Command {
  id: string;
  group: string;
  label: string;
  sub?: string;
  icon: IconName;
  keywords?: string;
  run: () => void;
}

export const NAV: { to: string; label: string; icon: IconName; keys?: string; hint: string }[] = [
  { to: "/", label: "Home", icon: "home", keys: "g h", hint: "Decisions waiting for you and the state of the service" },
  { to: "/prs", label: "Pull requests", icon: "pr", keys: "g p", hint: "The review queue, ranked by priority" },
  { to: "/repos", label: "Repositories", icon: "repo", keys: "g r", hint: "Profiles, settings, and onboarding" },
  { to: "/inbox", label: "Proposals", icon: "inbox", keys: "g i", hint: "Actions waiting for approval" },
  { to: "/questions", label: "Agent questions", icon: "question", keys: "g q", hint: "What running agents are asking" },
  { to: "/memory", label: "Memory", icon: "book", keys: "g m", hint: "Memory changes and the memory repository" },
  { to: "/ci", label: "CI health", icon: "activity", hint: "Diagnosed failures, flaky jobs, reruns" },
  { to: "/agents", label: "Agents", icon: "cpu", hint: "Runtime health, runs, and cost" },
  { to: "/chat", label: "Conversations", icon: "message", hint: "Your conversations with the agent" },
  { to: "/settings", label: "Settings", icon: "settings", keys: "g s", hint: "Agents, model providers, routing, and system" },
  { to: "/service", label: "Service", icon: "server", hint: "Running version, updates, self-improvement" },
  { to: "/audit", label: "Audit log", icon: "shield", hint: "Every decision and change" },
];

function matches(haystack: string, query: string): number {
  const h = haystack.toLowerCase();
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return 1;
  let score = 0;
  for (const w of words) {
    const at = h.indexOf(w);
    if (at === -1) return 0;
    score += at === 0 ? 3 : h[at - 1] === " " || h[at - 1] === "/" || h[at - 1] === "#" ? 2 : 1;
  }
  return score;
}

export function CommandPalette({ open, onClose, commands, me }: { open: boolean; onClose: () => void; commands: Command[]; me: Me }) {
  const t = useT();
  const navigate = useNavigate();
  const agent = useAgent();
  const prs = usePrs(open);
  const repos = useRepos(open);
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (open) {
      setQuery("");
      setIndex(0);
      window.setTimeout(() => input.current?.focus(), 0);
    }
  }, [open]);

  const items = useMemo(() => {
    const go = (to: string) => () => navigate(to);
    const all: Command[] = [
      ...commands,
      ...NAV.map((n) => ({ id: `nav:${n.to}`, group: "Go to", label: t(n.label), sub: n.keys, icon: n.icon, keywords: n.hint, run: go(n.to) })),
      ...(repos.data?.map((r) => r.repo) ?? me.repos).map((repo) => ({
        id: `repo:${repo}`,
        group: "Repositories",
        label: repo,
        icon: "repo" as IconName,
        run: go(`/repos/${repo}`),
      })),
      ...(prs.data ?? []).map((p) => ({
        id: `pr:${p.repo}#${p.number}`,
        group: "Pull requests",
        label: p.title,
        sub: `${p.repo}#${p.number}${p.tier ? ` · ${p.tier}` : ""}`,
        icon: "pr" as IconName,
        keywords: `${p.repo}#${p.number} #${p.number} ${p.author} ${p.labels.join(" ")}`,
        run: go(prPath(p.repo, p.number)),
      })),
    ];
    const scored = all
      .map((c) => ({ c, s: matches(`${c.label} ${c.sub ?? ""} ${c.keywords ?? ""}`, query) }))
      .filter((x) => x.s > 0);
    const limits: Record<string, number> = { "Pull requests": query ? 8 : 5, Repositories: query ? 6 : 4 };
    const counts: Record<string, number> = {};
    const out: Command[] = [];
    for (const { c } of query ? scored.sort((a, b) => b.s - a.s) : scored) {
      counts[c.group] = (counts[c.group] ?? 0) + 1;
      if (counts[c.group] <= (limits[c.group] ?? 50)) out.push(c);
    }
    if (query.trim()) {
      out.push({
        id: "ask",
        group: "Agent",
        label: `${t("Ask the agent")}: “${query.trim()}”`,
        icon: "sparkles",
        run: () => agent.open({ prompt: query.trim(), send: true }),
      });
    }
    const order = ["Agent", "Commands", "Go to", "Pull requests", "Repositories"];
    if (query.trim()) order.push(order.shift()!);
    return out.sort((a, b) => order.indexOf(a.group) - order.indexOf(b.group));
  }, [commands, prs.data, repos.data, me.repos, query, navigate, agent, t]);

  useEffect(() => setIndex(0), [query]);
  useEffect(() => {
    list.current?.querySelector(`[data-index="${index}"]`)?.scrollIntoView({ block: "nearest" });
  }, [index]);

  if (!open) return null;
  const run = (c: Command | undefined) => {
    if (!c) return;
    onClose();
    c.run();
  };
  let lastGroup = "";
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="dialog palette" role="dialog" aria-modal="true" aria-label={t("Command palette")}>
        <input
          ref={input}
          autoFocus
          className="palette-input"
          value={query}
          placeholder={t("Search pages, PRs, repositories, or ask the agent…")}
          aria-label={t("Search")}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setIndex((i) => Math.min(items.length - 1, i + 1));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setIndex((i) => Math.max(0, i - 1));
            } else if (e.key === "Enter" && !e.nativeEvent.isComposing) {
              e.preventDefault();
              run(items[index]);
            } else if (e.key === "Escape") {
              onClose();
            }
          }}
        />
        <div className="palette-list" ref={list} role="listbox">
          {items.map((c, i) => {
            const header = c.group !== lastGroup ? <div className="palette-group">{t(c.group)}</div> : null;
            lastGroup = c.group;
            return (
              <div key={c.id}>
                {header}
                <div
                  className={`palette-item ${i === index ? "active" : ""}`}
                  role="option"
                  aria-selected={i === index}
                  data-index={i}
                  onMouseMove={() => setIndex(i)}
                  onClick={() => run(c)}
                >
                  <Icon name={c.icon} />
                  <span className="truncate">{c.label}</span>
                  {c.sub && <span className="sub">{c.sub}</span>}
                </div>
              </div>
            );
          })}
          {!items.length && <div className="empty">{t("No matches.")}</div>}
        </div>
        <div className="palette-foot">
          <span>
            <span className="kbd">↑</span> <span className="kbd">↓</span> {t("move")}
          </span>
          <span>
            <span className="kbd">↵</span> {t("open")}
          </span>
          <span>
            <span className="kbd">esc</span> {t("close")}
          </span>
        </div>
      </div>
    </div>
  );
}
