// Small shared pieces: status pills, loading and error boxes, diffs, and mutation helpers.

import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { ApiError } from "./api";

export function Tier({ tier }: { tier: string | null | undefined }) {
  if (!tier) return <span className="muted">—</span>;
  return <span className={`pill tier tier-${tier.toLowerCase()}`}>{tier}</span>;
}

const STATUS_CLASS: Record<string, string> = {
  executed: "ok",
  approved: "ok",
  merged: "ok",
  noop: "muted",
  shadowed: "muted",
  superseded: "muted",
  expired: "muted",
  proposed: "warn",
  pending: "warn",
  queued: "info",
  intent: "info",
  open: "warn",
  failed: "bad",
  denied: "bad",
  rejected: "bad",
  conflict: "bad",
};

export function Status({ value }: { value: string | null | undefined }) {
  if (!value) return null;
  return <span className={`pill status-${STATUS_CLASS[value.toLowerCase()] ?? "info"}`}>{value}</span>;
}

export function Labels({ labels }: { labels: string[] }) {
  if (!labels.length) return <span className="muted">—</span>;
  return (
    <span className="labels">
      {labels.map((l) => (
        <span key={l} className="label">
          {l}
        </span>
      ))}
    </span>
  );
}

export function Loading() {
  return <div className="muted pad">Loading…</div>;
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  const message = error instanceof Error ? error.message : String(error);
  const status = error instanceof ApiError ? ` (${error.status})` : "";
  return (
    <div className="error" role="alert">
      {message}
      {status}
    </div>
  );
}

export function Empty({ children }: { children?: ReactNode }) {
  return <div className="muted pad">{children ?? "Nothing here."}</div>;
}

export function Section({ title, children, aside }: { title: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <section className="card">
      <header className="card-head">
        <h2>{title}</h2>
        {aside}
      </header>
      {children}
    </section>
  );
}

export function Diff({ text }: { text: string | null | undefined }) {
  if (!text) return <Empty>No differences.</Empty>;
  return (
    <pre className="diff">
      {text.split("\n").map((line, i) => {
        const cls = line.startsWith("+++") || line.startsWith("---")
          ? "diff-file"
          : line.startsWith("@@")
            ? "diff-hunk"
            : line.startsWith("+")
              ? "diff-add"
              : line.startsWith("-")
                ? "diff-del"
                : "";
        return (
          <div key={i} className={cls}>
            {line || " "}
          </div>
        );
      })}
    </pre>
  );
}

export function Pre({ text }: { text: string }) {
  return <pre className="pre">{text}</pre>;
}

export function When({ at }: { at: string | null | undefined }) {
  if (!at) return <span className="muted">—</span>;
  const d = new Date(at);
  if (Number.isNaN(d.getTime())) return <span>{at}</span>;
  return (
    <time dateTime={at} title={d.toISOString()}>
      {d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}
    </time>
  );
}

/** A mutation that invalidates the given query key prefixes when it succeeds. */
export function useAct<TArgs, TResult>(fn: (args: TArgs) => Promise<TResult>, invalidate: string[][]) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => invalidate.forEach((queryKey) => client.invalidateQueries({ queryKey })),
  });
}

export function prPath(repo: string, number: number | null | undefined): string {
  return `/pr/${repo}/${number ?? ""}`;
}
