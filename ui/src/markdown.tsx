// GitHub-flavoured Markdown as React elements, for PR descriptions, review drafts,
// comments, and agents' messages: what a text looks like on GitHub. ``marked`` turns the
// Markdown, with the HTML GitHub allows in it, into HTML; an inert document parses that
// (nothing in it runs or loads), and only the tags and attributes in ``TAGS`` become
// React elements. No HTML is ever injected: other tags keep only their text, scripts,
// styles, event handlers, and ids are dropped, links open only http(s), mailto, and
// in-page URLs, and images load only over http(s).

import { Marked } from "marked";
import { createElement, Fragment, type ReactNode, useMemo } from "react";

const marked = new Marked({ gfm: true, breaks: true, async: false });

/** The tags kept, with the attributes each may keep (as React props). */
const TAGS: Record<string, string[]> = {
  a: ["href", "title"],
  abbr: ["title"],
  b: [],
  blockquote: [],
  br: [],
  center: [],
  code: [],
  dd: [],
  del: [],
  details: ["open"],
  div: ["align"],
  dl: [],
  dt: [],
  em: [],
  h1: ["align"],
  h2: ["align"],
  h3: ["align"],
  h4: ["align"],
  h5: ["align"],
  h6: ["align"],
  hr: [],
  i: [],
  img: ["src", "alt", "title", "width", "height", "align"],
  input: ["type", "checked"],
  ins: [],
  kbd: [],
  li: [],
  mark: [],
  ol: ["start"],
  p: ["align"],
  picture: [],
  pre: [],
  q: [],
  s: [],
  samp: [],
  small: [],
  span: [],
  strike: [],
  strong: [],
  sub: [],
  summary: [],
  sup: [],
  table: [],
  tbody: [],
  td: ["align", "colspan", "rowspan"],
  tfoot: [],
  th: ["align", "colspan", "rowspan"],
  thead: [],
  tr: [],
  tt: [],
  u: [],
  ul: [],
  var: [],
};

/** Tags whose content is never shown either. */
const DROPPED = new Set(["script", "style", "template", "iframe", "object", "embed", "noscript", "textarea", "select", "button", "form", "svg", "math"]);

const STRUCTURAL = new Set(["table", "thead", "tbody", "tfoot", "tr", "ul", "ol", "dl", "picture"]);

const SAFE_HREF = /^(https?:\/\/|mailto:|#)/i;
const SAFE_SRC = /^https?:\/\//i;
const ALIGN = new Set(["left", "center", "right", "justify"]);
const PROP: Record<string, string> = { colspan: "colSpan", rowspan: "rowSpan" };

function props(el: Element, tag: string, key: string): Record<string, unknown> | null {
  const out: Record<string, unknown> = { key };
  for (const name of TAGS[tag]) {
    const value = el.getAttribute(name);
    if (value === null) continue;
    if (name === "href") {
      if (!SAFE_HREF.test(value.trim())) return null;
      out.href = value.trim();
      if (!value.startsWith("#")) {
        out.target = "_blank";
        out.rel = "noreferrer noopener";
      }
    } else if (name === "src") {
      if (!SAFE_SRC.test(value.trim())) return null;
      out.src = value.trim();
      out.referrerPolicy = "no-referrer";
      out.loading = "lazy";
    } else if (name === "align") {
      if (ALIGN.has(value.toLowerCase())) out.style = { textAlign: value.toLowerCase() };
    } else if (name === "width" || name === "height" || name === "colspan" || name === "rowspan" || name === "start") {
      if (/^\d{1,5}%?$/.test(value)) out[PROP[name] ?? name] = value;
    } else if (name === "open" || name === "checked") {
      out[name] = true;
    } else {
      out[name] = value;
    }
  }
  return out;
}

function convert(node: Node, key: string): ReactNode {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent;
  if (node.nodeType !== Node.ELEMENT_NODE) return null; // comments: the service's markers
  const el = node as Element;
  const tag = el.tagName.toLowerCase();
  if (DROPPED.has(tag)) return null;
  const kids = () =>
    Array.from(el.childNodes, (c, i) =>
      // Tables and lists hold no text of their own; the source's line breaks there are not content.
      STRUCTURAL.has(tag) && c.nodeType === Node.TEXT_NODE && !c.textContent?.trim() ? null : convert(c, `${key}.${i}`),
    );
  if (!(tag in TAGS)) return <Fragment key={key}>{kids()}</Fragment>;
  const p = props(el, tag, key);
  if (p === null) return tag === "img" ? (el.getAttribute("alt") ?? null) : <Fragment key={key}>{kids()}</Fragment>;
  switch (tag) {
    case "input":
      // Only a task list's box, which nobody can tick here.
      return el.getAttribute("type") === "checkbox" ? <input key={key} type="checkbox" checked={!!p.checked} readOnly disabled /> : null;
    case "img": {
      // ``align`` on an image floats it, as on GitHub.
      const side = (p.style as { textAlign?: string } | undefined)?.textAlign;
      const img = createElement("img", { ...p, style: side === "left" || side === "right" ? { float: side } : undefined });
      // An image opens in full size, as on GitHub, unless a link already holds it.
      return el.closest("a") ? img : (
        <a key={key} href={String(p.src)} target="_blank" rel="noreferrer noopener">
          {img}
        </a>
      );
    }
    case "h1":
    case "h2":
    case "h3":
    case "h4":
    case "h5":
    case "h6": {
      const level = Number(tag[1]);
      return (
        <div key={key} className={`md-h md-h${level}`} role="heading" aria-level={level} style={p.style as object}>
          {kids()}
        </div>
      );
    }
    case "pre": {
      const code = el.querySelector(":scope > code");
      const lang = /language-([\w+-]+)/.exec(code?.className ?? "")?.[1];
      return (
        <pre key={key} className="md-code" data-lang={lang}>
          <code>{(code ?? el).textContent}</code>
        </pre>
      );
    }
    case "table":
      return (
        <div key={key} className="md-table">
          {createElement("table", p, kids())}
        </div>
      );
    case "li":
      return createElement("li", { ...p, className: el.querySelector(":scope > input[type=checkbox]") ? "task" : undefined }, kids());
    case "br":
    case "hr":
      return createElement(tag, p);
    default:
      return createElement(tag, p, kids());
  }
}

/** ``text``'s Markdown and HTML as elements, built from the allowlist above. */
export function renderMarkdown(text: string): ReactNode[] {
  const html = marked.parse(text.replace(/\r\n?/g, "\n")) as string;
  const doc = new DOMParser().parseFromString(`<body>${html}</body>`, "text/html");
  return Array.from(doc.body.childNodes, (c, i) => convert(c, `m${i}`));
}

/** ``text`` rendered as GitHub renders a comment. HTML comments (the service's
 * markers) are dropped. */
export function Markdown({ text, className }: { text: string; className?: string }) {
  const nodes = useMemo(() => renderMarkdown(text), [text]);
  return <div className={`md ${className ?? ""}`}>{nodes}</div>;
}
