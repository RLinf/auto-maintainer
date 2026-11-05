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

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && path.startsWith("/api/")) headers["X-AM-CSRF"] = cookie("am_csrf");
  const res = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
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
    const detail =
      data && typeof data === "object" && "detail" in data
        ? String((data as { detail: unknown }).detail)
        : typeof data === "string"
          ? data
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
}

export interface ReviewActivity {
  job?: { playbook: string; status: string; updated_at: string; result: Record<string, unknown> | null; error: string | null };
  draft?: { proposal: string; for: string | null; sha: string };
  posted?: { sha: string; by: string | null };
}

export interface ReviewStart {
  repo: string;
  number: number;
  run: "review" | "followup" | "skip";
  reason?: string;
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
  actions: (ActionRecord & { proposal: Proposal | null })[];
  reviews: { sha: string; kind: string; action_id: string; result: Record<string, unknown>; created_at: string }[];
  ci: CiObservation[];
  hygiene: { sha: string; results: HygieneResult[] } | null;
  arm: { sha: string; armed_by: string; armed_at: string } | null;
  gate: { outcome: string; items: GateItem[]; needs_ci_gate: boolean } | null;
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
  created_at: string;
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
}

export interface AgentHealth {
  name: string;
  available: boolean;
  version: string | null;
  authenticated: boolean | null;
  structured_output: boolean | null;
  latency_seconds: number | null;
  detail: string;
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
  proposals: string[];
  created_at: string;
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
}
