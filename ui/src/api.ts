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
  paused: boolean;
  pending_questions: number;
  can_post_as_me?: boolean;
}

export interface ServiceView {
  code_repo: string | null;
  branch?: string;
  approve_permission?: string;
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
  prs?: { number: number; title: string; author: string; branch: string; url: string; self_improvement: boolean }[];
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

export interface PrRow {
  repo: string;
  number: number;
  title: string;
  author: string;
  url: string;
  head: string;
  labels: string[];
  draft: boolean;
  updated_at: string | null;
  review_state: string;
  reviewed_sha: string;
  claimed_by: string | null;
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
export interface ReviewGuides {
  repo: string;
  writing_guide: string | null;
  quality_guide: string | null;
  /** Whether every open PR gets a quick score before its review. */
  quick_quality: boolean;
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

export interface PrDetail {
  pr: {
    repo: string;
    number: number;
    title: string;
    body: string;
    author: string;
    head_sha: string;
    base_ref: string;
    url: string;
    labels: string[];
    is_draft: boolean;
    state: string;
    files: { path: string; additions: number; deletions: number }[];
    checks: { id: number; name: string; workflow: string | null; status: string; conclusion: string | null }[];
  };
  review_state: { state: string; reviewed_sha: string | null; latest_review_state: string | null };
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
  merge_queue: { head: number | null; order: { number: number; title: string }[] } | null;
}

export interface RunCiPlan {
  state: "gated" | "failed" | "running" | "passed" | "none";
  label: string | null;
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
  created_at: string;
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
  effort: string | null;
  /** Whether its CLI is on this host; ``null`` while it is turned off. */
  installed: boolean | null;
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
}

export interface SettingsProvider {
  id: string;
  name: string;
  protocol: "anthropic" | "openai" | "openai-responses";
  base_url: string;
  api_key: string | null;
  models: string[];
  key: SecretInfo | null;
  used_by: string[];
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

export interface SettingsView {
  access: GateDecision;
  repo: string | null;
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
    from_settings: string[];
  };
  host: {
    dev_mode: boolean;
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
  kind: "start" | "message" | "thinking" | "tool" | "result" | "question" | "error" | "done";
  text: string;
  detail: string;
  runtime: string;
  task_kind: string;
  ok: boolean | null;
  at: string;
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
  questions: string[];
  questions_for_me: number;
  produced: { id: string; kind: string; status: string; proposal: string | null; for: string | null }[];
  last_step: JobStep | null;
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
  thread: string | null;
  /** The run answering the newest message, while it runs. */
  live_run: string | null;
  messages: ChatMessage[];
  /** The review draft the job produced, by action id. */
  draft: string | null;
  /** Whether the draft still waits for a decision, so a revision can be saved on it. */
  draft_pending: boolean;
}
