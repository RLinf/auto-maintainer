// HTTP client for the auto-maintainer API (src/auto_maintainer/web/api.py).
// Every non-GET /api request carries the X-AM-CSRF header from the am_csrf cookie.

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function cookie(name: string): string {
  const match = document.cookie.split("; ").find((c) => c.startsWith(name + "="));
  return match ? decodeURIComponent(match.slice(name.length + 1)) : "";
}

const CSRF_REFUSED = "missing or wrong CSRF token";

async function send(method: string, path: string, body?: unknown): Promise<Response> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && path.startsWith("/api/")) headers["X-AM-CSRF"] = cookie("am_csrf");
  return fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let res = await send(method, path, body);
  if (res.status === 403 && method !== "GET" && (await res.clone().text()).includes(CSRF_REFUSED)) {
    // The browser lost its token (a phone that closed its browser keeps the session
    // but may drop it): any read gets a new one; try once more with it.
    await fetch("/api/me", { credentials: "same-origin" });
    res = await send(method, path, body);
  }
  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!res.ok) {
    const raw = data && typeof data === "object" && "detail" in data ? (data as { detail: unknown }).detail : data;
    const detail =
      raw && typeof raw === "object" && "message" in raw
        ? [String((raw as { message: unknown }).message), ...(((raw as { problems?: string[] }).problems ?? []) as string[])].join(": ")
        : typeof raw === "string"
          ? raw
          : raw
            ? JSON.stringify(raw)
            : res.statusText;
    throw new ApiError(res.status, detail);
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body: unknown = {}) => request<T>("POST", path, body),
  put: <T>(path: string, body: unknown = {}) => request<T>("PUT", path, body),
  del: <T>(path: string) => request<T>("DELETE", path),
};

export function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const parts = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== "" && v !== false)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  return parts.length ? `?${parts.join("&")}` : "";
}

// ---- Types mirroring the API responses (only the fields the UI reads). ----

export interface Me {
  login: string;
  name: string;
  avatar_url: string;
  operator: boolean;
  repos: string[];
  dev_mode: boolean;
  /** Another host serves the same repositories: this one writes to a forge only what a person approves. */
  standby?: boolean;
  paused: boolean;
  pending_questions: number;
  can_post_as_me?: boolean;
}

export interface ServiceView {
  code_repo: string | null;
  branch?: string;
  owner?: string | null;
  can_approve?: boolean;
  reason?: string;
  status?: {
    running_sha: string;
    branch: string;
    upstream_sha: string | null;
    pending: { sha: string; author: string; subject: string }[];
    dirty: boolean;
    error: string | null;
  } | null;
  prs?: {
    number: number;
    title: string;
    author: string;
    branch: string;
    base?: string;
    url: string;
    head_sha?: string;
    mergeable?: string;
    conflicting?: boolean;
    self_improvement: boolean;
  }[];
  history?: { kind: string; status: string; summary: string; error: string | null; at: string | null }[];
}

export interface QuestionOption {
  id: string;
  label: string;
  kind: "allow" | "allow_session" | "deny" | "cancel";
}

export interface InputQuestion {
  id: string;
  question: string;
  header: string;
  options: string[];
  free_text: boolean;
}

export interface AgentAnswer {
  allow: boolean;
  option_id: string | null;
  answers: Record<string, string[]>;
  message: string;
  by: string;
  automatic: boolean;
  at: string | null;
}

export interface AgentQuestion {
  id: string;
  kind: "permission" | "input";
  title: string;
  detail: string;
  tool: string;
  paths: string[];
  options: QuestionOption[];
  inputs: InputQuestion[];
  runtime: string;
  task_kind: string;
  repo: string | null;
  pr: number | null;
  asked_for?: string | null;
  job_id?: string | null;
  state: "pending" | "answered" | "expired" | "cancelled";
  created_at: string | null;
  expires_at: string | null;
  answer: AgentAnswer | null;
}

export interface Assignee {
  login: string;
  /** "owner": answers for the whole PR's progress; "reviewer": reviews it. */
  role: "owner" | "reviewer";
  /** "claimed": they took it themselves; "assigned": someone (``by``) gave it to them. */
  /** ``forge``: asked on the forge itself (a review request, an assignee). */
  kind: "claimed" | "assigned" | "forge";
  by: string;
  at: string;
}

export interface Assignable {
  can_assign: boolean;
  people: { login: string; role: string | null; areas: string[] }[];
}

export interface AssignResult {
  assignees: Assignee[];
  requested: { status: string; error: string | null; reason: string | null; action: string } | null;
}

export interface PrRow {
  repo: string;
  number: number;
  title: string;
  author: string;
  url: string;
  head: string;
  labels: string[];
  draft: boolean;
  /** When the PR was opened. */
  created_at: string | null;
  updated_at: string | null;
  review_state: string;
  reviewed_sha: string;
  /** The PR's owners and reviewers here (``role``), who claimed the role or were
   * assigned it, oldest first; "Owned by me" and "I review" filter by them. Reviews
   * GitHub requested (its code owners' automatic ones among them) give nobody a role. */
  assignees: Assignee[];
  /** How its review goes (``GET .../progress`` has the details). */
  progress: ProgressSummary;
  tier: string | null;
  total: number | null;
  summary: string;
  scores: Record<string, number>;
  review_trigger: "manual" | "scan" | "events";
  review_activity: ReviewActivity;
  quality: ReviewQuality | null;
  quick_quality: QuickQuality | null;
}

/** A PR's quality as the reviewing agent judged it, by the repository's quality guide
 *  (domain/review.py: review_quality). Internal. */
export interface ReviewQuality {
  score: number;
  band: QualityBand;
  reason: string;
  /** The reason by dashboard language; ``reason`` where one is missing. */
  reasons?: Record<string, string>;
  /** The basics the agent checked before scoring: met, not met, or null (not applicable). */
  checks?: Record<string, boolean | null>;
  /** The head the judged review looked at. */
  sha: string;
  kind: "review" | "followup";
  posted: boolean;
  action_id: string;
  /** The score comes from scoring that review again, not from the review itself. */
  rescored: boolean;
}

/** A quick score of a PR from its diff, before any review (playbooks/quality.py).
 *  The review's score is the accurate one. Internal. */
export interface QuickQuality {
  score: number;
  band: QualityBand;
  reason: string;
  reasons?: Record<string, string>;
  checks?: Record<string, boolean | null>;
  /** The head it looked at. */
  sha: string;
  at: string;
}

export interface QualityStart {
  repo: string;
  number: number;
  run: "score" | "joined";
  event: string;
}

/** How a repository's reviews are written and how their agent judges quality;
 *  ``null`` means the service's default. */
export interface SkillChoice {
  name: string;
  /** The repository's SKILL.md; empty for the service's own skills. */
  path: string;
  description: string;
  /** Its name or folder says review, so the default review follows it. */
  reviews?: boolean;
}

export interface ReviewGuides {
  repo: string;
  /** ``review.skill``: builtin:<name> (the repository's review skill, else the service's),
   *  service:<name> (the service's even so), or repo:<path>. */
  skill: string;
  default_skill: string;
  /** The maintainers' own words on how PRs are reviewed; null when they gave none. */
  instructions: string | null;
  /** What reviews follow now under ``skill``. "missing": a repository skill that is gone. */
  in_use: { kind: "repo" | "service" | "missing"; name: string; path: string };
  repo_skills: SkillChoice[];
  service_skills: SkillChoice[];
  /** The repository has no review skill and nobody chose how its PRs are reviewed. */
  needs_choice: boolean;
  writing_guide: string | null;
  quality_guide: string | null;
  /** Whether every open PR gets a quick score before its review. */
  quick_quality: boolean;
  /** Rules quick scores follow, drawn automatically from the quick scores reviews corrected. */
  quick_lessons: { rules?: string[]; at?: string; misses?: number };
  quick_misses: { number: number; quick: number; review: number; at: string }[];
  default_writing_guide: string;
  default_quality_guide: string;
  can_configure: boolean;
  reason: string;
}

export type QualityBand = "excellent" | "good" | "fair" | "poor" | "bad";

export interface ReviewActivity {
  job?: {
    id: string;
    playbook: string;
    status: string;
    updated_at: string;
    result: Record<string, unknown> | null;
    error: string | null;
    by?: string | null;
    agent?: string | null;
  };
  draft?: { proposal: string; for: string | null; sha: string };
  posted?: { sha: string; by: string | null };
}

export interface ReviewStart {
  repo: string;
  number: number;
  run: "review" | "followup" | "skip" | "joined" | "drafted";
  reason?: string;
  by?: string | null;
  agent?: string | null;
  state?: string;
  event?: string;
}

export interface Decision {
  kind: string;
  rule: string;
  reason: string;
}

export interface Action {
  kind: string;
  repo: string;
  number: number | null;
  head_sha: string | null;
  payload: Record<string, unknown>;
  rationale: string;
  discriminator: string;
  id: string;
  created_at: string;
  provenance: { requested_by?: string | null; playbook?: string | null; job_id?: string | null };
}

export interface Proposal {
  id: string;
  action_id: string;
  repo: string;
  state: string;
  created_at: string;
  decided_by: string | null;
  decided_at: string | null;
  reason: string | null;
  edited: boolean;
  warnings: string[];
  requested_by?: string | null;
}

export interface ActionRecord {
  action: Action;
  decision: Decision | null;
  status: string;
  external_ref: Record<string, unknown>;
  error: string | null;
  approver: string | null;
  updated_at: string;
}

export interface ProposalWithAction extends Proposal {
  action: ActionRecord | null;
  /** Who asked for the action: the reviewer a draft is for, or the person whose request made it. */
  requester?: string | null;
  /** Whether the viewer may approve, edit, or reject it: a maintainer, or the person who asked. */
  can_decide?: boolean;
  /** For a fix or merge: whether the viewer may push to the PR's branch (``null``: unknown). */
  can_push?: boolean | null;
}

export interface Evaluation {
  repo: string;
  number: number;
  title: string;
  rubric_version: number;
  scores: Record<string, number>;
  summary: string;
  rationale: string;
  manual_boost: number;
  tier_override: string | null;
  override_reason: string | null;
  override_by: string | null;
  base: number;
  boost: number;
  total: number;
  tier: string;
  matched_goals: string[];
  evaluated_at: string;
  evaluated_by: string;
}

export interface GateItem {
  name: string;
  ok: boolean;
  detail: string;
  pending: boolean;
}

/** Where a PR's approvals stand, the same on its row and its page. */
export interface ApprovalSummary {
  /** Approvals that count under the base branch's rules. */
  approved: number;
  /** Approvals the branch requires; null when its rules are not known. */
  required: number | null;
  /** Approvals still needed (a waiting code owner counts); null when unknown. */
  needed: number | null;
  owners_waiting: number;
  /** Reviewers assigned on the dashboard, and those of them who approved. */
  assigned: number;
  assigned_approved: number;
  assigned_waiting: string[];
  changes_requested: number;
}

export interface ProgressSummary {
  approval?: ApprovalSummary;
  threads: number;
  resolved: number;
  /** Open threads a comment check found addressed in the code. */
  fixed: number;
  /** Share of review threads resolved; ``null`` without threads. */
  addressed: number | null;
  approved: number;
  approved_at_head: number;
  changes_requested: number;
  reviewed: number;
  reviewers: number;
}

export interface ReviewerProgress {
  login: string;
  verdict: "APPROVED" | "CHANGES_REQUESTED" | "COMMENTED" | "DISMISSED" | null;
  at_head: boolean;
  reviews: number;
  threads: number;
  resolved: number;
  /** Open threads whose lines changed since. */
  outdated: number;
  /** Open threads a comment check found addressed in the code. */
  fixed: number;
  addressed: number | null;
  /** The review threads they opened, each with what a check found. */
  comments: CommentItem[];
  last_at: string | null;
  /** A reviewer of the PR here (listed first). */
  assigned: boolean;
  requested: boolean;
  /** CODEOWNERS names them for some of the changed files. */
  code_owner?: boolean;
}

/** What a comment check found of one review thread, at one head. */
export interface CommentCheck {
  thread_id: string;
  status: "addressed" | "partly" | "open" | "unclear";
  /** What the comment asks, in one line. */
  summary: string;
  reason: string;
  fixed_in: string | null;
  head_sha: string;
  checked_at: string;
}

export interface CommentItem {
  thread_id: string;
  path: string | null;
  line: number | null;
  url: string;
  /** The comment's first line, until a check sums it up. */
  excerpt: string;
  /** Resolved on GitHub. */
  resolved: boolean;
  outdated: boolean;
  check: CommentCheck | null;
  /** Resolved, or found addressed by a check. */
  done: boolean;
}

export interface ProgressEvent {
  at: string | null;
  kind: "opened" | "role" | "review" | "commits" | "check";
  login: string;
  state: string | null;
  threads: number;
  resolved: number;
  commits: number;
  detail: string;
}

/** ``GET /api/prs/{repo}/{n}/progress``: how the PR's review goes, for its owners. */
export interface PrProgress {
  threads: number;
  resolved: number;
  outdated: number;
  fixed: number;
  addressed: number | null;
  approved_by: string[];
  approved_at_head: string[];
  changes_requested_by: string[];
  required_approvals: number | null;
  reviewers: ReviewerProgress[];
  timeline: ProgressEvent[];
  /** The code owners' approvals: each owner's verdict, and how many parts of the
   * change an owner other than the author approved. */
  code_owners: { file: string | null; required: boolean | null; parts: number; approved_parts: number; people: CodeOwnerStanding[]; waiting: string[][] };
  approvals: ApprovalStatus;
  approval?: ApprovalSummary;
  head: string;
  /** The user resolves any comment (a maintainer or the PR's owner); a reviewer resolves their own. */
  can_resolve: boolean;
  /** The user signed in with GitHub, which resolving as them needs. */
  signed_in: boolean;
  /** "scan": checked on the repository's schedule too; "manual": only when asked. */
  comment_checks: "scan" | "manual" | null;
}

/** A code owner of a PR's changed files and where they stand on it. */
export interface CodeOwnerStanding {
  /** A login, an ``org/team``, or an email address, as CODEOWNERS names it. */
  login: string;
  team: boolean;
  /** The PR's author, whose own approval GitHub does not count. */
  author: boolean;
  /** Their latest review that is not a comment. */
  verdict: "APPROVED" | "CHANGES_REQUESTED" | "DISMISSED" | null;
  at_head: boolean;
  /** GitHub asks them (or the team) for a review now. */
  requested: boolean;
  files: number;
  /** What the merge waits on from them: they approved, they alone can approve some
   * files, one of them and others must, another owner's approval covers them, or
   * they wrote the PR. */
  need: "approved" | "needed" | "one_of" | "covered" | "author";
  /** For ``covered``: the owners whose approval covers all their files. */
  covered_by?: string[];
}

/** ``GET /api/prs/{repo}/{n}/code-owners``: whom to ask for the approvals a merge
 * waits on. Code owners are not the PR's owners here. */
/** A PR's approvals against its branch's rules, as GitHub counts them. */
export interface ApprovalStatus {
  /** GitHub's own verdict on the branch's review rules for this PR. */
  decision: "APPROVED" | "CHANGES_REQUESTED" | "REVIEW_REQUIRED" | null;
  /** Approving reviews the branch requires; null when it could not be read. */
  required: number | null;
  /** Approvals that count toward ``required``. */
  approved_by: string[];
  not_counted: { login: string; reason: string }[];
  /** Approvals still needed, from anyone with write access. */
  missing: number | null;
  changes_requested_by: string[];
  code_owners_required: boolean | null;
  code_owners_waiting: string[][];
  last_push_approval: boolean | null;
}

export interface CodeOwners {
  file: string | null;
  base: string;
  /** Whether the branch makes a merge wait for a code owner's approval of each part. */
  required: boolean | null;
  parts: { owners: string[]; patterns: string[]; files: string[]; approved_by: string[] }[];
  people: CodeOwnerStanding[];
  unowned: string[];
  /** The approvals still missing: for each, the owners any one of whom may give it. */
  waiting: string[][];
  approvals: ApprovalStatus;
  approval?: ApprovalSummary;
  /** Others GitHub asks to review, who own none of the files. */
  requested: string[];
}

export interface PrDetail {
  pr: {
    repo: string;
    number: number;
    title: string;
    body: string;
    author: string;
    head_sha: string;
    base_ref: string;
    head_ref?: string;
    mergeable?: "MERGEABLE" | "CONFLICTING" | "UNKNOWN";
    url: string;
    labels: string[];
    is_draft: boolean;
    state: string;
    files: { path: string; additions: number; deletions: number }[];
    checks: {
      id: number;
      name: string;
      workflow: string | null;
      status: string;
      conclusion: string | null;
      /** The check's own page on the forge; missing for a check that has none. */
      details_url?: string | null;
    }[];
    reviews?: { id: number; author: string; state: string; submitted_at: string | null; commit_sha: string | null }[];
    requested_reviewers?: string[];
  };
  assignees: Assignee[];
  review_state: { state: string; reviewed_sha: string | null; latest_review_state: string | null };
  /** Review starts a review of the person's own: they own or review this PR here, and
   * the reviews it has are others'. */
  own_review?: boolean;
  evaluation: Evaluation | null;
  quality: ReviewQuality | null;
  quick_quality: QuickQuality | null;
  actions: (ActionRecord & { proposal: Proposal | null })[];
  reviews: { sha: string; kind: string; action_id: string; result: Record<string, unknown>; created_at: string }[];
  ci: CiObservation[];
  hygiene: { sha: string; results: HygieneResult[] } | null;
  arm: { sha: string; armed_by: string; armed_at: string } | null;
  gate: { outcome: string; items: GateItem[]; needs_ci_gate: boolean } | null;
  run_ci: RunCiPlan;
  /** The failed jobs worth a look: the newest runs, without the repository's gate and summary jobs. */
  ci_failed?: string[];
  /** The CI as the merge schedule and GitHub read it: the newest run of each workflow,
   * the repository's gate and summary jobs aside; ``latest`` are those runs' checks. */
  ci_status?: { state: string; passed: number; running: number; failed: string[]; label: string | null; latest: number[] };
  merge_queue: { head: number | null; order: { number: number; title: string }[] } | null;
}

export interface RunCiPlan {
  /** "approval": GitHub holds a workflow until a maintainer approves running it. */
  state: "gated" | "failed" | "approval" | "running" | "passed" | "none";
  label: string | null;
  waiting?: string[];
  approve_url?: string | null;
  approve_runs?: number[];
  reruns: { run_id: number; attempt: number | null; workflow: string | null }[];
}

export interface HygieneResult {
  id: string;
  passed: boolean;
  blocking: boolean;
  summary: string;
  details: string[];
  skipped: boolean;
}

export interface CiObservation {
  repo: string;
  number: number | null;
  sha: string;
  run_id: number | null;
  check_id: number;
  check_name: string;
  workflow: string | null;
  category: string;
  signature: string | null;
  confidence: number;
  explanation: string;
  rerun: boolean;
  /** For a failure that is not a flake: what a look at the code found. */
  root_cause: RootCause | null;
  /** A person's word on the failure, which the CI doctor learns from. */
  verdict?: "flaky" | "real" | null;
  verdict_by?: string | null;
  id?: string;
  created_at: string;
}

export interface RootCause {
  jobs: string[];
  failing: string;
  origin: "change" | "base" | "infra" | "flaky" | "unclear";
  cause: string;
  culprits: { path: string; line: number | null; why: string }[];
  fix: string;
  evidence: string[];
  confidence: number;
}

export interface RepoRow {
  repo: string;
  active: boolean;
  paused: boolean;
  added_by: string;
  added_at: string;
  last_poll_at: string | null;
  open_prs: number;
  pending_proposals: number;
  candidate_pending: boolean;
  candidate_error: string | null;
}

export interface GateDecision {
  allowed: boolean;
  repo: string;
  login: string;
  required: string;
  actual: string;
  reason: string;
}

export interface ProfileResponse {
  lock: {
    repo: string;
    hash: string;
    layers: Record<string, string>;
    approved_by: string[];
    approved_at: string;
    source: string;
    profile: Record<string, unknown>;
  } | null;
  candidate: {
    repo: string;
    lock_hash: string | null;
    /** The profile that approving would lock, as plain data. */
    candidate: Record<string, unknown> | null;
    candidate_hash: string | null;
    candidate_layers: Record<string, string>;
    candidate_error: string | null;
    candidate_diff: string | null;
  };
  layers: { name: string; kind: string; source: string; hash: string }[];
  memory_layer: string;
  can_configure: boolean;
  gate: GateDecision;
}

export interface MemoryChange {
  service?: boolean;
  id: string;
  tier: string;
  title: string;
  description: string;
  files: Record<string, string | null>;
  repos: string[];
  author: string;
  requested_by: string | null;
  state: string;
  approvals: Record<string, string[]>;
  branch: string | null;
  created_at: string;
  merged_at: string | null;
  rejected_reason: string | null;
  /** For each repository whose profile the change locks: the approved profile and the new one. */
  profiles?: { repo: string; before: Record<string, unknown> | null; after: Record<string, unknown> }[];
}

export interface AgentHealth {
  name: string;
  available: boolean;
  version: string | null;
  authenticated: boolean | null;
  structured_output: boolean | null;
  latency_seconds: number | null;
  detail: string;
  /** Its models and reasoning levels, when it is installed. */
  choices?: AgentChoices | null;
}

export interface AgentChoices {
  /** Empty when the CLI cannot list them; any name it accepts still works. */
  models: { id: string; name: string }[];
  default_model: string | null;
  efforts: string[];
  default_effort: string | null;
  /** ``cli``: the CLI reported them itself; ``known``: what the adapter knows it accepts. */
  source: "cli" | "known";
}

export interface AgentRun {
  permissions?: string;
  id: string;
  job_id: string | null;
  repo: string | null;
  runtime: string;
  task_kind: string;
  ok: boolean;
  error: string | null;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number | null;
  duration_seconds: number;
  /** The person whose request the run worked for; null for the service's own work. */
  requested_by?: string | null;
  created_at: string;
}

/** Tokens of some runs. ``input_tokens`` includes ``cached_input_tokens``, the part
 * read from the provider's prompt cache (recorded since 2026-11-11). */
export interface UsagePart {
  runs: number;
  failed: number;
  tokens: number;
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  cost_usd: number | null;
}

export interface UsageRow {
  /** null: the service's own work, such as scans and scheduled checks. */
  login: string | null;
  runs: number;
  failed: number;
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  cost_usd: number | null;
  seconds: number;
  last_at: string | null;
  /** By task kind, or the activity when the run names one (a review's discussion). */
  by_kind: Record<string, UsagePart>;
  by_runtime: Record<string, UsagePart>;
  /** By repository; "" for runs outside any. */
  by_repo: Record<string, UsagePart>;
  /** Tokens by UTC day (YYYY-MM-DD), then task kind. */
  by_day: Record<string, Record<string, number>>;
}

/** The most tokens one person's requests may use on the service's agents in a
 * rolling day, week, and month; null is no limit. */
export interface TokenLimit {
  daily_tokens?: number | null;
  weekly_tokens?: number | null;
  monthly_tokens?: number | null;
}

export interface TokenLimits {
  default: TokenLimit;
  people: Record<string, TokenLimit>;
}

export interface PersonUsage {
  login: string;
  /** Runs they asked for on the service's agent accounts and model APIs: what their limit counts. */
  service: UsageRow;
  /** Runs on their own agent accounts and model APIs. */
  own: UsageRow;
  limit: TokenLimit;
  /** Whether the limit is theirs alone rather than the default. */
  limit_own: boolean;
  used: { day: number; week: number; month: number };
}

export interface PeopleUsage {
  days: number;
  since: string;
  complete: boolean;
  everything: boolean;
  can_manage: boolean;
  limits: TokenLimits;
  people: PersonUsage[];
  /** The service's own work nobody asked for; for the owner and operators. */
  background: UsageRow | null;
}

export interface QuotaBucket {
  group: string;
  name: string;
  /** The fraction left, 0 to 1; null when the CLI does not say. */
  remaining: number | null;
  resets_at: string | null;
  /** When it resets, in the CLI's own words, when it gives no exact time. */
  resets: string;
}

export interface AgentQuota {
  runtime: string;
  read_at: string;
  buckets: QuotaBucket[];
  note: string;
}

export interface AgentUsageRow extends UsageRow {
  name: string;
  ready: boolean;
  /** What the CLI's own /usage says is left; null when it cannot report it. */
  quota: AgentQuota | null;
}

export interface AgentsUsage {
  days: number;
  since: string;
  complete: boolean;
  /** Whether the tokens count every run, or only those in repositories the viewer sees. */
  everything: boolean;
  /** Only the runs on the viewer's own agent accounts. */
  mine: boolean;
  agents: AgentUsageRow[];
}

export interface RepoUsage {
  days: number;
  since: string;
  /** False when the summary hit the most runs it reads; older runs are left out. */
  complete: boolean;
  /** Whether the viewer sees everyone (a maintainer) or only themselves. */
  everyone: boolean;
  total: UsageRow;
  people: UsageRow[];
}

export interface AuditEntry {
  id: string;
  kind: string;
  repo: string | null;
  number: number | null;
  actor: string;
  action_id: string | null;
  summary: string;
  created_at: string;
}

export interface ChatThread {
  id: string;
  owner: string;
  title: string;
  repo: string | null;
  runtime: string | null;
  /** The agent chosen for this conversation, or null for automatic routing. */
  agent?: string | null;
  /** The turn whose agent answers now, if one does. */
  live_run?: string | null;
  created_at: string;
}

export interface ChatMessage {
  id: string;
  thread_id: string;
  role: string;
  text: string;
  /** Who wrote a user message, in a conversation several people share. */
  author?: string | null;
  proposals: string[];
  changes?: string[];
  /** Jobs the answer started, by the id of their request. */
  jobs?: string[];
  /** What the answer asks to do once a person approves its card. */
  requests?: ChatRequest[];
  /** The agent run that wrote an answer; its steps stay readable. */
  run_id?: string | null;
  /** The agent runtime that wrote an answer. */
  runtime?: string | null;
  /** A message sent to the agent while it wrote the review. */
  while_running?: boolean;
  created_at: string;
}

export interface ChatRequest {
  id: string;
  /** ``improve``: change the service's own code; ``add_repo``: onboard a repository. */
  kind: "improve" | "add_repo";
  text: string;
  state: "pending" | "approved" | "declined";
  decided_by: string | null;
  job: string | null;
}

export interface PendingRequest {
  thread_id: string;
  message_id: string;
  asked_by: string;
  created_at: string;
  /** Whether the viewer may approve it (else it is their own, waiting for someone who may). */
  can_decide: boolean;
  request: ChatRequest;
}

export interface ChatSteps {
  run: string | null;
  live: boolean;
  steps: JobStep[];
}

export interface AgreementRow {
  matched: number;
  total: number;
  unknown: number;
  agreement: number | null;
}

export interface Agreement {
  shadowed: number;
  kinds: Record<string, AgreementRow>;
  levels?: Record<string, "off" | "shadow" | "propose" | "auto">;
  can_configure?: boolean;
}

export interface SecretInfo {
  name: string;
  configured: boolean;
  source: "dashboard" | "host" | "missing";
  updated_at: string | null;
  updated_by: string | null;
}

export interface SettingsAgent {
  name: string;
  adapter: string;
  enabled: boolean;
  model: string | null;
  /** A faster model for quick tasks (triage, scores, CI classification, comment checks). */
  quick_model: string | null;
  effort: string | null;
  /** Whether its CLI is on this host; ``null`` while it is turned off. */
  installed: boolean | null;
  /** Installed, and its account signed in or a model provider set: it can run tasks. */
  usable: boolean;
  /** Whether the work the signed-in person asks for runs on their own account of it
   * (their own model API, or their own sign-in), and so tries it first. */
  mine: "api" | "sign-in" | null;
  /** Whether its CLI has a sign-in the dashboard can run; dsh has none and runs only on a model API. */
  can_sign_in: boolean;
  provider: string | null;
  budget: { daily_usd: number | null; daily_runs: number | null };
  allow_bypass: boolean;
  permissions: "auto" | "restricted" | "bypass" | null;
  can_bypass: boolean;
  /** Runs under a ``command_prefix`` wrapper instead of as the service's own user. */
  confined: boolean;
  /** Commands the service approves for this agent without asking (leading words). */
  approve_commands: string[];
  binary: string | null;
  from_settings: boolean;
  providers_supported: string[];
  /** The account kinds its sign-in offers, when it must be told which (pi). */
  sign_in_options: SignInOption[];
}

export interface SignInOption {
  id: string;
  label: string;
}

export type Protocol = "anthropic" | "openai" | "openai-responses" | "google";

/** One model a provider serves and what is known of it; ``tested_at`` says it was
 * learned by talking to it (``examine``), else the endpoint's list said it. */
export interface ModelSpec {
  id: string;
  context_window: number | null;
  max_output: number | null;
  /** Reasoning levels it accepts; empty, none known. */
  reasoning: string[];
  /** APIs it answered a conversation on, when tested. */
  interfaces: Protocol[];
  tools: boolean | null;
  images: boolean | null;
  streaming: boolean | null;
  tested_at: string | null;
}

/** A model API as the forms show it: the service's or a person's own. */
export interface ApiRow {
  id: string;
  name: string | null;
  protocol: Protocol;
  base_url: string;
  api_key: string | null;
  models: ModelSpec[];
  /** The adapters each model can run (by model id). */
  works_with: Record<string, string[]>;
}

export interface SettingsProvider extends ApiRow {
  key: SecretInfo | null;
  used_by: string[];
}

export interface MyProvider extends ApiRow {
  has_key: boolean;
  used_by: string[];
}

export interface ModelList {
  ok: boolean;
  models: { id: string; context_window: number | null; max_output: number | null; reasoning: string[] }[];
  error?: string;
}

export interface Examination {
  ok: boolean;
  spec: Partial<ModelSpec> & { id: string };
  checks: { name: string; ok: boolean | null; detail: string }[];
  /** The adapters that can run on the model. */
  agents: string[];
}

export interface SettingsRoute {
  task: string;
  primary: string;
  fallback: string[];
  verifier: string | null;
  model: string | null;
  effort: string | null;
  timeout_seconds: number;
  permissions: string;
  from_settings: boolean;
  /** The agent the task runs on now: the first in its order that is installed. */
  runs_on: string | null;
}

export interface AgentLogin {
  id: string;
  agent: string;
  /** null: the service's own account. */
  account: string | null;
  by: string;
  state: "starting" | "waiting" | "done" | "failed" | "cancelled";
  url: string;
  /** A one-time code to enter on the sign-in page. */
  code: string;
  /** The CLI waits for the code the sign-in page shows, pasted back. */
  needs_code: boolean;
  message: string;
  started_at: string;
}

export interface MyAgent {
  name: string;
  can_sign_in: boolean;
  sign_in_options: SignInOption[];
  signed_in: boolean | null;
  protocols: string[];
  /** Whether each run gets a container of its own here, which a personal account needs. */
  isolated: boolean;
  adapter: string;
  setup: { agent: string; use_own: boolean; provider: string | null; model: string | null; effort: string | null };
  login: AgentLogin | null;
}

export interface AgentInstall {
  name: string;
  by: string;
  state: "running" | "done" | "failed";
  source: string;
  started_at: string;
  finished_at: string | null;
  version: string;
  error: string;
  /** The installer's last lines of output. */
  lines: string[];
}

export interface SettingsView {
  /** The agent CLIs this host can install, with where each comes from. */
  installable: Record<string, string>;
  installs: Record<string, AgentInstall>;
  access: GateDecision;
  repo: string | null;
  /** The one person who owns this service; ``null`` until someone takes ownership. */
  owner: string | null;
  document: string;
  status: { applied: boolean; problems: string[]; loaded_at: string | null };
  agents: SettingsAgent[];
  providers: SettingsProvider[];
  /** The agent every task tries first, and whether the settings or setup chose it. */
  default_agent: { name: string | null; source: "settings" | "setup" | null };
  routing: SettingsRoute[];
  system: {
    poll_seconds: number;
    max_public_writes_per_hour: number;
    max_repo_public_writes_per_hour: number;
    agent_slots: number;
    /** Agent jobs running now: for people's requests, and the service's own. */
    agent_slots_in_use: { people: number; service: number };
    from_settings: string[];
  };
  host: {
    dev_mode: boolean;
    standby?: boolean;
    paused: boolean;
    code_repo: string | null;
    memory_repo: string | null;
    base_url: string | null;
    sandbox_repo: string | null;
    operators: string[];
  };
  secrets: SecretInfo[];
  pending: MemoryChange[];
  labs: LabMachine[];
}

export interface LabMachine {
  name: string;
  /** Its environment and rules in plain language; the settings below are drafted from it. */
  description: string;
  /** What it is for: the PRs and tests it suits, such as those for hardware attached to it. */
  suited_for?: string;
  transport: "ssh" | "local";
  host: string | null;
  user?: string | null;
  port?: number | null;
  proxy_jump?: string | null;
  identity_file?: string | null;
  workdir: string;
  setup: string;
  repos: string[];
  forbid: string[];
  timeout_seconds: number;
  max_gpu_memory_mib: number;
  max_gpu_util_pct: number;
}

/** What the service would enforce on a machine, drafted from its description. */
export interface MachineDraft {
  machine: Omit<LabMachine, "name">;
  left_to_agent: string[];
  problems: string[];
  diff: string;
}

export interface MachineCheck {
  ok: boolean;
  error?: string;
  system?: string;
  cpus?: string;
  memory_gb?: string;
  gpus?: { index: number; memory_used_mib: number; memory_total_mib: number; utilization_pct: number; processes: number; idle: boolean }[];
}

export interface JobStep {
  kind: "start" | "message" | "thinking" | "tool" | "result" | "question" | "error" | "done" | "output";
  text: string;
  detail: string;
  /** For ``done``: what the agent said when it finished; ``text`` holds the error. */
  output: string;
  runtime: string;
  task_kind: string;
  ok: boolean | null;
  at: string;
}

/** Agent slots: ``size`` jobs run at once for people's requests (``people`` now);
 * the service's own work (``service`` now) takes only slots nobody uses. ``free`` is
 * how many this job could start in now, ``ahead`` the queued jobs that go first. */
export interface JobSlot {
  size: number;
  people: number;
  service: number;
  free: number;
  ahead: number;
}

export interface AgentJob {
  id: string;
  playbook: string;
  status: "queued" | "running" | "waiting" | "done" | "failed" | "cancelled";
  repo: string | null;
  number: number | null;
  title: string | null;
  by: string | null;
  mine: boolean;
  agent: string | null;
  note: string | null;
  /** Jobs started by one request (every PR scored again) share it. */
  batch: string | null;
  created_at: string;
  updated_at: string;
  error: string | null;
  result: Record<string, unknown> | null;
  wait: string | null;
  /** For a job queued to start now: its pool's slots, and how many queued jobs go first. */
  slot: JobSlot | null;
  questions: string[];
  questions_for_me: number;
  produced: { id: string; kind: string; status: string; proposal: string | null; for: string | null }[];
  last_step: JobStep | null;
  /** What the agent said when it finished: its reasoning and the problems it found. */
  final: string | null;
  steps?: JobStep[];
}

export interface SettingsCheck {
  ok: boolean;
  problems: string[];
  diff: string;
}

export interface Automation {
  repo: string;
  trigger: "manual" | "scan" | "events";
  schedule: string;
  every_hours: number | null;
  max_prs: number;
  followups: boolean;
  can_configure: boolean;
  reason: string;
}

/** The conversation about a review or follow-up, kept on its job. */
export interface ReviewTalkState {
  /** What the conversation is about: a review (or follow-up), or a change an agent made. */
  subject?: "review" | "fix";
  /** For a change: whether its job ended, so a message starts a turn. */
  open?: boolean;
  thread: string | null;
  agent?: string | null;
  runtime?: string | null;
  /** The run answering the newest message, while it runs. */
  live_run: string | null;
  messages: ChatMessage[];
  /** The review draft the job produced, by action id. */
  draft: string | null;
  /** Whether the draft still waits for a decision, so a revision can be saved on it. */
  draft_pending: boolean;
  /** Whether the agent writing the review takes messages now. */
  listening: boolean;
  /** Whether the draft was posted on GitHub; a revision then drafts an edit of it. */
  posted: boolean;
  /** The pending edit of the posted review, by action id. */
  revision: string | null;
}


/** Where agents run on this host (agents/container.py): each task in a container of
 *  its own, or on the host. */
export interface SandboxView {
  available: boolean;
  enabled?: boolean;
  ready?: boolean;
  status?: string;
  docker?: boolean;
  image?: string;
  network?: "bridge" | "host";
  cpus?: number;
  memory?: string;
  pids?: number;
  can_configure?: boolean;
  reason?: string;
}
