// A review comment where GitHub will show it: under the diff lines it refers to.

import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api } from "./api";

export interface DiffLine {
  kind: "add" | "del" | "ctx" | "hunk";
  old: number | null;
  new: number | null;
  text: string;
}

/** Lines of each file in a unified diff, with their old and new line numbers. */
export function parseDiff(text: string): Map<string, DiffLine[]> {
  const files = new Map<string, DiffLine[]>();
  let current: DiffLine[] | null = null;
  let oldNo = 0;
  let newNo = 0;
  for (const line of text.split("\n")) {
    if (line.startsWith("diff --git ")) {
      current = null;
      continue;
    }
    if (line.startsWith("+++ ")) {
      const path = line.slice(4).replace(/^b\//, "").trim();
      current = [];
      if (path !== "/dev/null") files.set(path, current);
      continue;
    }
    if (line.startsWith("--- ") || current === null) continue;
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$/.exec(line);
    if (hunk) {
      oldNo = parseInt(hunk[1], 10);
      newNo = parseInt(hunk[2], 10);
      current.push({ kind: "hunk", old: null, new: null, text: line });
      continue;
    }
    if (line.startsWith("+")) current.push({ kind: "add", old: null, new: newNo++, text: line.slice(1) });
    else if (line.startsWith("-")) current.push({ kind: "del", old: oldNo++, new: null, text: line.slice(1) });
    else if (line.startsWith(" ") || line === "") current.push({ kind: "ctx", old: oldNo++, new: newNo++, text: line.slice(1) });
  }
  return files;
}

export function usePrDiff(repo: string, number: number | null | undefined, enabled: boolean) {
  return useQuery({
    queryKey: ["diff", repo, number],
    queryFn: () => api.get<{ sha: string; diff: string; truncated: boolean }>(`/api/prs/${repo}/${number}/diff`),
    enabled: enabled && number != null,
    staleTime: 5 * 60_000,
  });
}

/** The diff lines a comment on ``path`` at ``line`` (or ``start``..``line``) refers to,
 * with up to ``context`` lines before them; null when the line is not in the diff. */
export function linesFor(
  files: Map<string, DiffLine[]>,
  path: string,
  line: number,
  start: number | null | undefined,
  side: string | undefined,
  context = 3,
): DiffLine[] | null {
  const lines = files.get(path);
  if (!lines) return null;
  const left = side === "LEFT";
  const at = lines.findIndex((l) => (left ? l.old === line && l.kind !== "add" : l.new === line && l.kind !== "del"));
  if (at < 0) return null;
  let from = at;
  if (start != null) {
    const s = lines.findIndex((l) => (left ? l.old === start : l.new === start));
    if (s >= 0 && s <= at) from = s;
  }
  from = Math.max(0, from - context);
  while (from < at && lines[from].kind === "hunk") from += 1;
  return lines.slice(from, at + 1);
}

export function DiffLines({ lines, highlight }: { lines: DiffLine[]; highlight?: number }) {
  return (
    <table className="diff-lines">
      <tbody>
        {lines.map((l, i) => (
          <tr key={i} className={`dl-${l.kind} ${i === lines.length - 1 && highlight !== undefined ? "dl-target" : ""}`}>
            <td className="dl-no">{l.old ?? ""}</td>
            <td className="dl-no">{l.new ?? ""}</td>
            <td className="dl-sign">{l.kind === "add" ? "+" : l.kind === "del" ? "−" : ""}</td>
            <td className="dl-text">{l.text || " "}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** One inline comment, under the lines it is about. */
export function CommentOnDiff({
  files,
  loading,
  path,
  line,
  start,
  side,
  children,
  outdated,
}: {
  files: Map<string, DiffLine[]> | null;
  loading: boolean;
  path: string;
  line: number;
  start?: number | null;
  side?: string;
  children: ReactNode;
  outdated: string;
}) {
  const lines = files ? linesFor(files, path, line, start, side) : null;
  return (
    <div className="diff-comment">
      <div className="diff-comment-file mono">
        {path}
        <span className="muted">
          :{start ? `${start}-` : ""}
          {line}
        </span>
      </div>
      {lines ? (
        <DiffLines lines={lines} highlight={line} />
      ) : !loading && files ? (
        <div className="small muted" style={{ padding: "6px 10px" }}>
          {outdated}
        </div>
      ) : null}
      <div className="diff-comment-body">{children}</div>
    </div>
  );
}
