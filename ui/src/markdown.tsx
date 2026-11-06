// GitHub-flavoured Markdown as React elements, for review drafts, comments, and agents'
// messages: what a draft will look like once posted. No HTML is ever injected: the few
// tags GitHub comments use (<sub>, <br>, <details>, <summary>) become elements, any other
// tag stays text, and links open only http(s) and mailto URLs.

import { Fragment, type ReactNode } from "react";

type Block =
  | { kind: "p"; lines: string[] }
  | { kind: "h"; level: number; text: string }
  | { kind: "code"; lang: string; text: string }
  | { kind: "quote"; blocks: Block[] }
  | { kind: "list"; ordered: boolean; start: number; items: { blocks: Block[]; task: boolean | null }[] }
  | { kind: "table"; head: string[]; rows: string[][]; align: ("left" | "center" | "right" | null)[] }
  | { kind: "details"; summary: string; blocks: Block[] }
  | { kind: "hr" };

const LIST = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const FENCE = /^\s{0,3}(```+|~~~+)\s*([\w+-]*)/;

function splitRow(line: string): string[] {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return trimmed.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
}

function parse(lines: string[]): Block[] {
  const out: Block[] = [];
  let i = 0;
  let para: string[] = [];
  const flush = () => {
    if (para.length) out.push({ kind: "p", lines: para });
    para = [];
  };
  while (i < lines.length) {
    const line = lines[i];
    const fence = FENCE.exec(line);
    if (fence) {
      flush();
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) body.push(lines[i++]);
      i += 1;
      out.push({ kind: "code", lang: fence[2], text: body.join("\n") });
      continue;
    }
    if (!line.trim()) {
      flush();
      i += 1;
      continue;
    }
    const h = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line);
    if (h) {
      flush();
      out.push({ kind: "h", level: h[1].length, text: h[2] });
      i += 1;
      continue;
    }
    if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line) && !para.length) {
      flush();
      out.push({ kind: "hr" });
      i += 1;
      continue;
    }
    if (/^\s*<details>/i.test(line)) {
      flush();
      const inner: string[] = [];
      let summary = "Details";
      i += 1;
      let depth = 1;
      while (i < lines.length) {
        if (/<details>/i.test(lines[i])) depth += 1;
        if (/<\/details>/i.test(lines[i])) {
          depth -= 1;
          if (depth === 0) break;
        }
        const s = /<summary>(.*?)<\/summary>/i.exec(lines[i]);
        if (s && depth === 1) summary = s[1];
        else inner.push(lines[i]);
        i += 1;
      }
      i += 1;
      out.push({ kind: "details", summary, blocks: parse(inner) });
      continue;
    }
    if (/^\s{0,3}>/.test(line)) {
      flush();
      const inner: string[] = [];
      while (i < lines.length && /^\s{0,3}>/.test(lines[i])) inner.push(lines[i++].replace(/^\s{0,3}>\s?/, ""));
      out.push({ kind: "quote", blocks: parse(inner) });
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(lines[i + 1])) {
      flush();
      const head = splitRow(line);
      const align = splitRow(lines[i + 1]).map((c) =>
        c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : c.startsWith(":") ? "left" : null,
      );
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(splitRow(lines[i++]));
      out.push({ kind: "table", head, rows, align });
      continue;
    }
    const item = LIST.exec(line);
    if (item && (!para.length || item[1].length === 0)) {
      flush();
      const indent = item[1].length;
      const ordered = /\d/.test(item[2]);
      const items: { blocks: Block[]; task: boolean | null }[] = [];
      const start = ordered ? parseInt(item[2], 10) : 1;
      while (i < lines.length) {
        const m = LIST.exec(lines[i]);
        if (!m || m[1].length !== indent || /\d/.test(m[2]) !== ordered) break;
        const body = [m[3]];
        i += 1;
        while (i < lines.length) {
          const next = lines[i];
          const nm = LIST.exec(next);
          if (nm && nm[1].length <= indent) break;
          if (!next.trim()) {
            if (i + 1 < lines.length && /^\s+\S/.test(lines[i + 1]) && (lines[i + 1].length - lines[i + 1].trimStart().length) > indent) {
              body.push("");
              i += 1;
              continue;
            }
            break;
          }
          if (!/^\s/.test(next) && !nm) {
            body.push(next);
            i += 1;
            continue;
          }
          body.push(next.slice(Math.min(indent + 2, next.length - next.trimStart().length)));
          i += 1;
        }
        let task: boolean | null = null;
        const t = /^\[([ xX])\]\s+/.exec(body[0]);
        if (t) {
          task = t[1] !== " ";
          body[0] = body[0].slice(t[0].length);
        }
        items.push({ blocks: parse(body), task });
      }
      out.push({ kind: "list", ordered, start, items });
      continue;
    }
    para.push(line);
    i += 1;
  }
  flush();
  return out;
}

const SAFE_URL = /^(https?:\/\/|mailto:|#)/i;

function inline(text: string, key = "i"): ReactNode[] {
  const out: ReactNode[] = [];
  const re =
    /(`+)([\s\S]*?[^`])\1(?!`)|\*\*([^*]+?)\*\*|__([^_]+?)__|~~([^~]+?)~~|(?<![\w*])\*([^*\s][^*]*?)\*(?![\w*])|(?<![\w_])_([^_\s][^_]*?)_(?![\w_])|!?\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)|<sub>([\s\S]*?)<\/sub>|<br\s*\/?>|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/gi;
  let last = 0;
  let n = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const k = `${key}-${n++}`;
    if (m[1]) out.push(<code key={k}>{m[2].replace(/^ (.*) $/, "$1")}</code>);
    else if (m[3] !== undefined || m[4] !== undefined) out.push(<strong key={k}>{inline(m[3] ?? m[4], k)}</strong>);
    else if (m[5] !== undefined) out.push(<del key={k}>{inline(m[5], k)}</del>);
    else if (m[6] !== undefined || m[7] !== undefined) out.push(<em key={k}>{inline(m[6] ?? m[7], k)}</em>);
    else if (m[9] !== undefined) {
      const url = m[9];
      const label = m[8] || url;
      out.push(
        SAFE_URL.test(url) ? (
          <a key={k} href={url} target="_blank" rel="noreferrer noopener">
            {inline(label, k)}
          </a>
        ) : (
          <Fragment key={k}>{label}</Fragment>
        ),
      );
    } else if (m[10] !== undefined) out.push(<sub key={k}>{inline(m[10], k)}</sub>);
    else if (m[11] !== undefined)
      out.push(
        <a key={k} href={m[11]} target="_blank" rel="noreferrer noopener">
          {m[11]}
        </a>,
      );
    else out.push(<br key={k} />);
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function render(blocks: Block[], key = "b"): ReactNode[] {
  return blocks.map((b, i) => {
    const k = `${key}-${i}`;
    switch (b.kind) {
      case "p":
        return (
          <p key={k}>
            {b.lines.map((line, j) => (
              <Fragment key={j}>
                {j > 0 && <br />}
                {inline(line.trim(), `${k}-${j}`)}
              </Fragment>
            ))}
          </p>
        );
      case "h":
        return (
          <div key={k} className={`md-h md-h${b.level}`} role="heading" aria-level={b.level}>
            {inline(b.text, k)}
          </div>
        );
      case "code":
        return (
          <pre key={k} className="md-code" data-lang={b.lang || undefined}>
            <code>{b.text}</code>
          </pre>
        );
      case "quote":
        return <blockquote key={k}>{render(b.blocks, k)}</blockquote>;
      case "hr":
        return <hr key={k} />;
      case "details":
        return (
          <details key={k}>
            <summary>{inline(b.summary, k)}</summary>
            {render(b.blocks, k)}
          </details>
        );
      case "table":
        return (
          <div key={k} className="md-table">
            <table>
              <thead>
                <tr>
                  {b.head.map((c, j) => (
                    <th key={j} style={{ textAlign: b.align[j] ?? undefined }}>
                      {inline(c, `${k}-h${j}`)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {b.rows.map((row, r) => (
                  <tr key={r}>
                    {row.map((c, j) => (
                      <td key={j} style={{ textAlign: b.align[j] ?? undefined }}>
                        {inline(c, `${k}-${r}-${j}`)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      case "list": {
        const items = b.items.map((it, j) => (
          <li key={j} className={it.task !== null ? "task" : undefined}>
            {it.task !== null && <input type="checkbox" checked={it.task} readOnly disabled />}
            {it.blocks.length === 1 && it.blocks[0].kind === "p"
              ? it.blocks[0].lines.map((line, x) => (
                  <Fragment key={x}>
                    {x > 0 && <br />}
                    {inline(line.trim(), `${k}-${j}-${x}`)}
                  </Fragment>
                ))
              : render(it.blocks, `${k}-${j}`)}
          </li>
        ));
        return b.ordered ? (
          <ol key={k} start={b.start}>
            {items}
          </ol>
        ) : (
          <ul key={k}>{items}</ul>
        );
      }
    }
    return null;
  });
}

/** ``text`` rendered as GitHub renders a comment. HTML comments (the service's
 * markers) are dropped. */
export function Markdown({ text, className }: { text: string; className?: string }) {
  const clean = text.replace(/<!--[\s\S]*?-->/g, "").replace(/\r\n?/g, "\n");
  return <div className={`md ${className ?? ""}`}>{render(parse(clean.split("\n")))}</div>;
}
