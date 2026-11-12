// A repository profile in plain words. Every setting gets a readable label and value;
// between two profiles only what changes is listed (was → now), section by section, and
// the YAML diff stays one click away. Profiles arrive as the JSON the API sends (the
// lock's ``profile`` and the candidate), with unset fields left out.

import { useState } from "react";
import { agentName } from "./agentPicker";
import type { MemoryChange } from "./api";
import { Diff } from "./components";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { kindLabel } from "./proposal";

export type Profile = Record<string, unknown>;

type T = (s: string) => string;

/** Sections in the order a maintainer reads them, with a short note on each. */
const SECTIONS: { key: string; title: string; hint: string }[] = [
  { key: "policy", title: "What the service may do", hint: "How far each action goes without a person" },
  { key: "review", title: "Reviews", hint: "When reviews run and what they contain" },
  { key: "merge", title: "Merging", hint: "When and how PRs are merged" },
  { key: "ci", title: "CI", hint: "Required workflows, reruns, and flakes" },
  { key: "labels", title: "Labels", hint: "Which labels mean what" },
  { key: "checks", title: "Checks on each PR", hint: "What every PR is checked for" },
  { key: "contribution", title: "Contribution rules", hint: "Titles, commits, and descriptions" },
  { key: "scope", title: "Which PRs it looks at", hint: "" },
  { key: "people", title: "People", hint: "Who counts as the team" },
  { key: "access", title: "Who may configure", hint: "" },
  { key: "priority", title: "Priority", hint: "" },
  { key: "timing", title: "Timing", hint: "Response times and the calendar" },
  { key: "labs", title: "Test machines", hint: "" },
  { key: "routing", title: "Agents per task", hint: "" },
  { key: "languages", title: "Language and style", hint: "" },
  { key: "confidentiality", title: "Kept private", hint: "Never written in public" },
  { key: "comments", title: "Comments", hint: "" },
  { key: "ceilings", title: "Upper limits", hint: "The most any action may reach" },
  { key: "playbooks", title: "Jobs", hint: "" },
  { key: "notify", title: "Notifications", hint: "" },
  { key: "memory", title: "Memory", hint: "" },
  { key: "extends", title: "Packs", hint: "Shared settings it builds on" },
];
const SECTION_KEYS = new Set(SECTIONS.map((s) => s.key));
const SKIP = new Set(["schema", "repo"]);

/** Paths whose children are names chosen by people (``*`` in a pattern). */
const MAPS = new Set(["policy", "ceilings", "routing", "notify", "playbooks", "labs", "ci.runner_groups", "labs.*.templates"]);
/** Patterns shown as one value instead of one row per field. */
const WHOLE = new Set(["policy.*", "ceilings.*", "routing.*", "notify.*", "playbooks.*", "ci.runner_groups.*", "labs.*.templates.*"]);

const LABELS: Record<string, string> = {
  extends: "Packs",
  "scope.min_pr": "Ignore PRs numbered below",
  "scope.base_branches": "Only PRs into",
  "scope.include_drafts": "Include draft PRs",
  "scope.backfill_reviews": "Review PRs opened before onboarding",
  "scope.ignore_authors": "Ignore PRs from",
  "scope.issues": "Look at issues too",
  "people.codeowners": "CODEOWNERS file",
  "people.team": "Team",
  "people.team_min_permission": "Counts as team from",
  "people.review_identities": "Accounts that review for the team",
  "access.configure": "Who may change the configuration",
  "contribution.commit_convention": "Commit messages",
  "contribution.conventional_types": "Allowed commit types",
  "contribution.title_rule.pattern": "PR title must match",
  "contribution.title_rule.max_description": "Longest title description",
  "contribution.title_rule.lowercase_start": "Title starts in lowercase",
  "contribution.title_rule.ascii_only": "Title is plain ASCII",
  "contribution.title_rule.no_trailing_period": "No period at the end of the title",
  "contribution.signoff": "Commits need Signed-off-by",
  "contribution.template_sections": "PR description sections",
  "contribution.license_header.pattern": "License header",
  "contribution.license_header.paths": "License header in",
  "checks[]": "Check",
  "review.trigger": "When reviews run",
  "review.scan.schedule": "Review schedule",
  "review.scan.max_prs": "PRs per scheduled run",
  "review.scan.followups": "Follow up on earlier reviews",
  "review.skill": "Review instructions",
  "review.template": "Review sections",
  "review.recommendations.ready": "Verdict when ready",
  "review.recommendations.changes": "Verdict when changes are needed",
  "review.recommendations.blocked": "Verdict when blocked",
  "review.verify": "A second agent checks the findings",
  "review.cross_agent": "Two agents review each PR",
  "review.max_inline_comments": "Most inline comments",
  "review.suggest_labels": "Suggest labels",
  "review.max_suggested_labels": "Most suggested labels",
  "labels.roles.needs_review": "Label for PRs that need review",
  "labels.roles.changes_requested": "Label when changes are requested",
  "labels.roles.ci_gate": "Label that lets CI run",
  "labels.roles.community": "Label for outside contributors",
  "labels.rules": "Label rules file",
  "labels.rules[]": "Label rule",
  "labels.never_remove_human_labels": "Never remove labels people added",
  "ci.provider": "CI system",
  "ci.required_workflows": "Required workflows",
  "ci.cancelled_is_failure": "A cancelled run counts as failed",
  "ci.rerun.max_per_sha": "Reruns per commit",
  "ci.rerun.backoff_seconds": "Wait before a rerun",
  "ci.runner_groups.*": "Runner group",
  "ci.flake_signatures[]": "Known flake",
  "ci.gate_daily_budget": "CI gate runs per day",
  "ci.root_cause": "Find the root cause of real failures",
  "merge.enabled": "Merge scheduling",
  "merge.method": "Merge method",
  "merge.arming.who": "Who may mark a PR ready to merge",
  "merge.approvals": "Approvals needed",
  "merge.blocking_roles": "Labels that block merging",
  "merge.message.subject": "Merge commit subject",
  "merge.message.preserve_trailers": "Trailers kept in the merge commit",
  "merge.update_branch": "Update the branch before merging",
  "merge.freezes[]": "Merge freeze",
  "priority.enabled": "Rank PRs by priority",
  "priority.rubric": "Priority rubric",
  "priority.stage_goals": "Stage goals file",
  "timing.calendar": "Holiday calendar",
  "timing.holidays": "Holidays",
  "timing.workdays": "Extra workdays",
  "timing.weekend": "Weekend",
  "timing.first_response_days": "First response within",
  "timing.rereview_days": "Review again within",
  "timing.stale_days": "Stale after",
  "labs.*.use": "Use it",
  "labs.*.timeout_seconds": "Time limit",
  "labs.*.base_clone": "Base clone",
  "labs.*.worktree_root": "Worktrees in",
  "labs.*.env_roots": "Environments in",
  "labs.*.throwaway_env_root": "Throwaway environments in",
  "labs.*.protected_paths": "Never touched",
  "labs.*.setup": "Setup command",
  "labs.*.guard.forbid": "Forbidden commands",
  "labs.*.guard.ray_private_instance": "Private Ray instance",
  "labs.*.templates.*": "Template",
  "languages.public": "Language of public text",
  "languages.internal": "Language of internal notes",
  "languages.style_guide": "Style guide",
  "languages.style_phrases": "Phrases to avoid",
  "confidentiality.internal_entities": "Names kept private",
  "confidentiality.internal_terms": "Terms kept private",
  "comments.command_prefix": "Command prefix",
  "comments.footer": "Comment footer",
  "comments.legacy_markers": "Older markers it recognizes",
  "memory.knowledge_auto_merge": "Merge what it learns on its own",
  "memory.use_global": "Use knowledge shared across repositories",
  "memory.notice_hours": "Notice before merging",
};

const LEVEL: Record<string, string> = { off: "Off", shadow: "Record only", propose: "Ask first", auto: "Automatic" };
const PERMISSION: Record<string, string> = {
  none: "Nobody",
  read: "Read access",
  triage: "Triage access",
  write: "Write access",
  maintain: "Maintain access",
  admin: "Admin access",
};
const VALUES: Record<string, Record<string, string>> = {
  "review.trigger": { manual: "When someone asks", scan: "On a schedule", events: "On every update" },
  "contribution.commit_convention": { none: "Any style", conventional: "Conventional Commits" },
  "merge.method": { squash: "Squash", merge: "Merge commit", rebase: "Rebase" },
  "merge.arming.who": { write: "Anyone with write access", maintain: "Maintainers", admin: "Admins", codeowners: "Code owners" },
  "access.configure": PERMISSION,
  "people.team_min_permission": PERMISSION,
};
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
/** Values shown as code: patterns, commands, and templates. */
const MONO = /pattern|subject|footer|setup|schedule|forbid|command_prefix|skill|stage_goals|codeowners|paths|base_clone|_root$|env_roots|flake_signatures|templates/;

interface Leaf {
  section: string;
  /** Dotted path with names chosen by people kept, e.g. ``policy.review.post``. */
  id: string;
  /** The same path with those names as ``*`` and list items as ``[]``. */
  pattern: string;
  /** The names in the path that ``*`` and ``[]`` stand for, in order. */
  names: string[];
  value: unknown;
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function isEmpty(v: unknown): boolean {
  return v === null || v === undefined || (Array.isArray(v) && v.length === 0) || (isObject(v) && Object.keys(v).length === 0);
}

function itemName(item: Record<string, unknown>, index: number): string {
  for (const key of ["id", "label", "name", "start"]) if (typeof item[key] === "string" && item[key]) return item[key] as string;
  return String(index + 1);
}

function flatten(profile: Profile | null): Map<string, Leaf> {
  const out = new Map<string, Leaf>();
  if (!profile) return out;
  const walk = (v: unknown, path: string[], pattern: string[], names: string[]) => {
    if (isEmpty(v)) return;
    const pat = pattern.join(".");
    const add = () => out.set(path.join("."), { section: path[0], id: path.join("."), pattern: pat, names, value: v });
    if (WHOLE.has(pat)) return add();
    if (Array.isArray(v)) {
      if (v.every(isObject)) {
        v.forEach((item, i) => {
          const name = itemName(item, i);
          out.set([...path, name].join("."), { section: path[0], id: [...path, name].join("."), pattern: `${pat}[]`, names: [...names, name], value: item });
        });
        return;
      }
      return add();
    }
    if (isObject(v) && path.length > 0) {
      const map = MAPS.has(pat);
      for (const [k, x] of Object.entries(v)) walk(x, [...path, k], [...pattern, map ? "*" : k], map ? [...names, k] : names);
      return;
    }
    add();
  };
  for (const [k, v] of Object.entries(profile)) if (!SKIP.has(k)) walk(v, [k], [k], []);
  return out;
}

function humanize(key: string): string {
  const s = key.replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function labelOf(leaf: Leaf, t: T): string {
  const [section] = leaf.pattern.split(".");
  if (section === "policy" || section === "ceilings") return t(kindLabel(leaf.names[0]));
  if (section === "routing") return leaf.names[0];
  if (section === "playbooks" || section === "notify") return leaf.names[0];
  const known = LABELS[leaf.pattern];
  const base = known ? t(known) : humanize(leaf.pattern.split(".").slice(1).join(" › ") || leaf.pattern);
  if (section === "labs") {
    const rest = leaf.names.slice(1);
    return `${leaf.names[0]} · ${base}${rest.length ? ` ${rest.join(" ")}` : ""}`;
  }
  return leaf.pattern.endsWith("[]") || leaf.pattern.endsWith(".*") ? `${base} ${leaf.names[leaf.names.length - 1]}` : base;
}

function plain(v: unknown, t: T): string {
  if (v === undefined || v === null || v === "") return t("not set");
  if (typeof v === "boolean") return v ? t("On") : t("Off");
  if (Array.isArray(v)) return v.length ? v.map((x) => plain(x, t)).join(", ") : t("none");
  if (isObject(v)) {
    const parts = Object.entries(v)
      .filter(([, x]) => !isEmpty(x) && x !== false)
      .map(([k, x]) => (x === true ? humanize(k).toLowerCase() : `${humanize(k).toLowerCase()}: ${plain(x, t)}`));
    return parts.length ? parts.join(" · ") : t("none");
  }
  return String(v);
}

function seconds(n: number, t: T): string {
  return n >= 60 && n % 60 === 0 ? `${n / 60} ${t("min")}` : `${n} ${t("s")}`;
}

function policyRule(v: unknown, t: T): string {
  if (!isObject(v)) return t(LEVEL[String(v)] ?? String(v));
  const level = t(LEVEL[String(v.level ?? "shadow")] ?? String(v.level));
  const c = isObject(v.conditions) ? v.conditions : {};
  const when: string[] = [];
  if (c.author === "team") when.push(t("team PRs"));
  if (c.author === "external") when.push(t("outside PRs"));
  if (typeof c.recommendation === "string") when.push(`${t("verdict")} ${c.recommendation}`);
  if (Array.isArray(c.paths) && c.paths.length) when.push(`${t("paths")} ${c.paths.join(", ")}`);
  if (!when.length) return level;
  return `${level} (${t("applies to")} ${when.join(", ")}; ${t("otherwise")} ${t(LEVEL[String(v.otherwise ?? "shadow")] ?? String(v.otherwise)).toLowerCase()})`;
}

function valueOf(leaf: Leaf, v: unknown, t: T): string {
  if (v === undefined) return t("not set");
  const p = leaf.pattern;
  if (p === "policy.*") return policyRule(v, t);
  if (p === "ceilings.*") return t(LEVEL[String(v)] ?? String(v));
  if (p === "routing.*" && isObject(v)) {
    const order = [v.primary, ...(Array.isArray(v.fallback) ? v.fallback : [])].map((n) => agentName(String(n)));
    return order.join(" → ") + (v.model ? ` (${v.model})` : "");
  }
  if (p === "playbooks.*" && isObject(v)) return v.enabled === false ? t("Off") : t("On");
  if (p === "checks[]" && isObject(v)) {
    const bits = [`${v.name ?? v.id} (${v.type ?? "builtin"})`];
    if (v.blocking) bits.push(t("blocks merging"));
    if (v.enabled === false) bits.push(t("Off").toLowerCase());
    return bits.join(" · ");
  }
  if (p === "ci.flake_signatures[]" && isObject(v)) return String(v.description || v.pattern || "");
  if (p === "timing.weekend" && Array.isArray(v)) return v.map((d) => t(DAYS[Number(d)] ?? String(d))).join(", ");
  if (VALUES[p] && typeof v === "string") return t(VALUES[p][v] ?? v);
  if (typeof v === "number") {
    if (p.endsWith("_days")) return `${v} ${t(v === 1 ? "day" : "days")}`;
    if (p.endsWith("_hours")) return `${v} ${t("h")}`;
    if (p.endsWith("seconds")) return seconds(v, t);
  }
  return plain(v, t);
}

function Value({ leaf, v }: { leaf: Leaf; v: unknown }) {
  const t = useT();
  const text = valueOf(leaf, v, t);
  return v !== undefined && typeof v === "string" && MONO.test(leaf.pattern) ? <code>{text}</code> : <span>{text}</span>;
}

interface Row {
  leaf: Leaf;
  before: unknown;
  after: unknown;
}

function sectionOrder(key: string): number {
  const i = SECTIONS.findIndex((s) => s.key === key);
  return i < 0 ? SECTIONS.length : i;
}

function sectionTitle(key: string, t: T): string {
  const s = SECTIONS.find((x) => x.key === key);
  return s ? t(s.title) : humanize(key);
}

function grouped(rows: Row[]): [string, Row[]][] {
  const by = new Map<string, Row[]>();
  for (const r of rows) by.set(r.leaf.section, [...(by.get(r.leaf.section) ?? []), r]);
  return [...by.entries()].sort((a, b) => sectionOrder(a[0]) - sectionOrder(b[0]));
}

function levelCounts(rows: Row[], t: T): string {
  const counts = new Map<string, number>();
  for (const r of rows) {
    const v = r.after;
    const level = String(isObject(v) ? v.level ?? "shadow" : v);
    counts.set(level, (counts.get(level) ?? 0) + 1);
  }
  return ["auto", "propose", "shadow", "off"]
    .filter((l) => counts.get(l))
    .map((l) => `${counts.get(l)} ${t(LEVEL[l]).toLowerCase()}`)
    .join(" · ");
}

function Rows({ rows, changes }: { rows: Row[]; changes: boolean }) {
  const t = useT();
  return (
    <div className="pv-rows">
      {rows.map(({ leaf, before, after }) => (
        <div key={leaf.id} className="pv-row">
          <span className="pv-label">{labelOf(leaf, t)}</span>
          <span className="pv-value">
            {changes && (
              <>
                <span className="pv-was">
                  <Value leaf={leaf} v={before} />
                </span>
                <span className="pv-arrow" aria-hidden>
                  →
                </span>
              </>
            )}
            <span className={changes ? "pv-now" : ""}>
              <Value leaf={leaf} v={after} />
            </span>
          </span>
        </div>
      ))}
    </div>
  );
}

/** Rules that hold for every PR, grouped by level ("Record only: …"); rules with
 * conditions keep a row of their own. */
function PolicyByLevel({ rows }: { rows: Row[] }) {
  const t = useT();
  const groups = new Map<string, string[]>();
  const own: Row[] = [];
  for (const r of rows) {
    const rule = r.after;
    const c = isObject(rule) && isObject(rule.conditions) ? rule.conditions : {};
    const plainRule = !isObject(rule) || ((c.author ?? "any") === "any" && !c.recommendation && !(Array.isArray(c.paths) && c.paths.length));
    if (!plainRule) {
      own.push(r);
      continue;
    }
    const level = String(isObject(rule) ? rule.level ?? "shadow" : rule);
    groups.set(level, [...(groups.get(level) ?? []), labelOf(r.leaf, t)]);
  }
  return (
    <>
      <div className="pv-rows">
        {["auto", "propose", "shadow", "off"]
          .filter((l) => groups.get(l)?.length)
          .map((l) => (
            <div key={l} className="pv-row">
              <span className="pv-label">{t(LEVEL[l])}</span>
              <span className="pv-value">{(groups.get(l) ?? []).join(", ")}</span>
            </div>
          ))}
      </div>
      {own.length > 0 && <Rows rows={own} changes={false} />}
    </>
  );
}

function FoldedSection({ section, rows, open }: { section: string; rows: Row[]; open?: boolean }) {
  const t = useT();
  const hint = SECTIONS.find((s) => s.key === section)?.hint;
  const summary = section === "policy" ? levelCounts(rows, t) : `${rows.length} ${t(rows.length === 1 ? "setting" : "settings")}`;
  return (
    <details className="pv-section" open={open}>
      <summary>
        <strong>{sectionTitle(section, t)}</strong>
        <span className="small muted">
          {summary}
          {hint ? ` · ${t(hint)}` : ""}
        </span>
      </summary>
      {section === "policy" ? <PolicyByLevel rows={rows} /> : <Rows rows={rows} changes={false} />}
    </details>
  );
}

/** What changes between ``before`` (the approved profile, or ``null`` before the first
 * approval) and ``after``, in labeled rows grouped by section, with the YAML diff
 * behind a toggle when ``yaml`` is given. */
export function ProfileChanges({ before, after, yaml }: { before: Profile | null; after: Profile; yaml?: string | null }) {
  const t = useT();
  const [showYaml, setShowYaml] = useState(false);
  const [showSame, setShowSame] = useState(false);
  const old = flatten(before);
  const now = flatten(after);
  const all: Row[] = [...new Set([...old.keys(), ...now.keys()])].map((id) => ({
    leaf: (now.get(id) ?? old.get(id)) as Leaf,
    before: old.get(id)?.value,
    after: now.get(id)?.value,
  }));
  const changed = before ? all.filter((r) => JSON.stringify(r.before) !== JSON.stringify(r.after)) : [];
  const changedSections = new Set(changed.map((r) => r.leaf.section));
  const same = grouped(all.filter((r) => r.after !== undefined && !changedSections.has(r.leaf.section)));
  const unknown = [...new Set(all.map((r) => r.leaf.section))].filter((s) => !SECTION_KEYS.has(s));

  return (
    <div className="pv">
      {before ? (
        changed.length ? (
          grouped(changed).map(([section, rows]) => (
            <section key={section} className="pv-section">
              <div className="pv-head">
                <strong>{sectionTitle(section, t)}</strong>
              </div>
              <Rows rows={rows} changes />
            </section>
          ))
        ) : (
          <span className="muted small">{t("No setting changes; only how the profile is put together does.")}</span>
        )
      ) : (
        <>
          <span className="muted small">{t("Nothing is approved here yet. This is everything the service will follow in this repository.")}</span>
          {same.map(([section, rows]) => (
            <FoldedSection key={section} section={section} rows={rows} open={section === "policy"} />
          ))}
        </>
      )}
      {before && same.length > 0 && (
        <div className="stack" style={{ gap: 8 }}>
          <button className="ghost sm pv-toggle" onClick={() => setShowSame((v) => !v)}>
            <Icon name={showSame ? "chevronDown" : "chevronRight"} size={13} />
            {t("Unchanged")}: {same.slice(0, 4).map(([s]) => sectionTitle(s, t)).join(", ")}
            {same.length > 4 ? ` +${same.length - 4} ${t("more sections")}` : ""}
          </button>
          {showSame && same.map(([section, rows]) => <FoldedSection key={section} section={section} rows={rows} />)}
        </div>
      )}
      {unknown.length > 0 && <span className="small muted">{unknown.join(", ")}</span>}
      {yaml !== undefined && (
        <div className="stack" style={{ gap: 8 }}>
          <button className="ghost sm pv-toggle" onClick={() => setShowYaml((v) => !v)}>
            <Icon name={showYaml ? "chevronDown" : "chevronRight"} size={13} />
            {showYaml ? t("Hide YAML") : t("Show YAML")}
          </button>
          {showYaml && <Diff text={yaml} />}
        </div>
      )}
    </div>
  );
}

/** The profile changes a memory change makes, one block per repository it locks. */
export function ChangeProfiles({ change }: { change: MemoryChange }) {
  const profiles = change.profiles ?? [];
  if (!profiles.length) return null;
  return (
    <div className="stack" style={{ gap: 10 }}>
      {profiles.map((p) => (
        <div key={p.repo} className="stack" style={{ gap: 6 }}>
          {profiles.length > 1 && <strong className="small">{p.repo}</strong>}
          <ProfileChanges before={p.before} after={p.after} />
        </div>
      ))}
    </div>
  );
}
