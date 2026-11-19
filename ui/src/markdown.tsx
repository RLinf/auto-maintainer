// GitHub-flavoured Markdown as React elements, for PR descriptions, review drafts,
// comments, and agents' messages: what a text looks like on GitHub. ``marked`` turns the
// Markdown, with the HTML GitHub allows in it, into HTML; an inert document parses that
// (nothing in it runs or loads), and only the tags and attributes in ``TAGS`` become
// React elements. No HTML is ever injected: other tags keep only their text, scripts,
// styles, event handlers, and ids are dropped, links open only http(s), mailto, and
// in-page URLs, and images load only over http(s).
//
// Math is written as on GitHub: ``$...$`` inline, ``$$...$$`` displayed, or a
// ``math`` code block. KaTeX turns it into MathML, which the browser lays out itself
// (no styles or fonts), and that MathML goes through an allowlist of its own
// (``MATH_TAGS``) the same way; a formula KaTeX cannot read shows as its source.

import katex from "katex";
import { Marked, type TokenizerExtension, type RendererExtension } from "marked";
import { createElement, Fragment, type ReactNode, useMemo } from "react";

const marked = new Marked({ gfm: true, breaks: true, async: false });

const attr = (text: string) => text.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");

/** ``$$...$$`` on lines of their own: a displayed formula. */
const mathBlock: TokenizerExtension & RendererExtension = {
  name: "mathBlock",
  level: "block",
  start: (src) => src.match(/^ {0,3}\$\$/m)?.index,
  tokenizer(src) {
    const m = /^ {0,3}\$\$([\s\S]+?)\$\$[ \t]*(?:\n|$)/.exec(src);
    return m ? { type: "mathBlock", raw: m[0], text: m[1].trim() } : undefined;
  },
  renderer: (token) => `<div data-math="${attr(token.text)}" data-display="1"></div>`,
};

/** ``$...$`` in a line (``$$...$$`` displayed). As on GitHub, the opening ``$`` is not
 * followed by a space, the closing one not preceded by one nor followed by a digit,
 * so prices such as "$5 and $10" stay text. */
const mathInline: TokenizerExtension & RendererExtension = {
  name: "mathInline",
  level: "inline",
  start(src) {
    const i = src.indexOf("$");
    return i < 0 ? undefined : i;
  },
  tokenizer(src) {
    const display = /^\$\$(?!\s)([^$]+?)(?<!\s)\$\$/.exec(src);
    if (display) return { type: "mathInline", raw: display[0], text: display[1], display: true };
    const m = /^\$(?![\s$])((?:\\\$|[^$\n])+?)(?<![\s\\])\$(?!\d)/.exec(src);
    return m ? { type: "mathInline", raw: m[0], text: m[1], display: false } : undefined;
  },
  renderer: (token) => `<span data-math="${attr(token.text)}"${token.display ? ' data-display="1"' : ""}></span>`,
};

marked.use({
  extensions: [mathBlock, mathInline],
  renderer: {
    // A ``math`` code block is a displayed formula, as on GitHub.
    code(token) {
      return token.lang?.trim() === "math" ? `<div data-math="${attr(token.text)}" data-display="1"></div>` : false;
    },
  },
});

/** The MathML KaTeX writes, with the attributes each element may keep. */
const MATH_ATTRS = ["mathvariant", "stretchy", "fence", "separator", "lspace", "rspace", "accent", "accentunder", "columnalign", "rowspacing", "columnspacing", "columnlines", "rowlines", "frame", "width", "height", "depth", "voffset", "scriptlevel", "displaystyle", "linethickness", "minsize", "maxsize", "movablelimits", "symmetric", "largeop", "notation", "display", "form", "encoding"];
const MATH_TAGS = new Set(["math", "semantics", "mrow", "mi", "mo", "mn", "ms", "mtext", "mspace", "msup", "msub", "msubsup", "mfrac", "msqrt", "mroot", "mstyle", "mpadded", "mphantom", "menclose", "mover", "munder", "munderover", "mtable", "mtr", "mtd", "mlabeledtr", "merror", "annotation"]);
const MATH_PROP: Record<string, string> = { columnalign: "columnAlign", rowspacing: "rowSpacing", columnspacing: "columnSpacing", columnlines: "columnLines", rowlines: "rowLines", scriptlevel: "scriptLevel", displaystyle: "displayStyle", linethickness: "lineThickness", minsize: "minSize", maxsize: "maxSize", movablelimits: "movableLimits", largeop: "largeOp", accentunder: "accentUnder", mathvariant: "mathVariant" };

/** Where each math alphabet starts in Unicode (capitals, small letters, digits):
 * browsers lay out ``mathvariant`` only as "normal", so ``\\mathbb{E}`` and the like
 * are written in these letters instead. */
const VARIANTS: Record<string, [number, number, number?]> = {
  bold: [0x1d400, 0x1d41a, 0x1d7ce],
  italic: [0x1d434, 0x1d44e],
  "bold-italic": [0x1d468, 0x1d482],
  script: [0x1d49c, 0x1d4b6],
  "bold-script": [0x1d4d0, 0x1d4ea],
  fraktur: [0x1d504, 0x1d51e],
  "double-struck": [0x1d538, 0x1d552, 0x1d7d8],
  "bold-fraktur": [0x1d56c, 0x1d586],
  "sans-serif": [0x1d5a0, 0x1d5ba, 0x1d7e2],
  "bold-sans-serif": [0x1d5d4, 0x1d5ee, 0x1d7ec],
  "sans-serif-italic": [0x1d608, 0x1d622],
  monospace: [0x1d670, 0x1d68a, 0x1d7f6],
};
/** Letters Unicode keeps in its older letterlike block instead of the alphabets. */
const LETTERLIKE: Record<string, string> = {
  "script:B": "ℬ", "script:E": "ℰ", "script:F": "ℱ", "script:H": "ℋ", "script:I": "ℐ", "script:L": "ℒ", "script:M": "ℳ", "script:R": "ℛ",
  "script:e": "ℯ", "script:g": "ℊ", "script:o": "ℴ", "fraktur:C": "ℭ", "fraktur:H": "ℌ", "fraktur:I": "ℑ", "fraktur:R": "ℜ", "fraktur:Z": "ℨ",
  "double-struck:C": "ℂ", "double-struck:H": "ℍ", "double-struck:N": "ℕ", "double-struck:P": "ℙ", "double-struck:Q": "ℚ", "double-struck:R": "ℝ",
  "double-struck:Z": "ℤ", "italic:h": "ℎ",
};

function inVariant(text: string, variant: string): string {
  const base = VARIANTS[variant];
  if (!base) return text;
  return Array.from(text, (ch) => {
    const hole = LETTERLIKE[`${variant}:${ch}`];
    if (hole) return hole;
    const c = ch.charCodeAt(0);
    if (c >= 65 && c <= 90) return String.fromCodePoint(base[0] + c - 65);
    if (c >= 97 && c <= 122) return String.fromCodePoint(base[1] + c - 97);
    if (c >= 48 && c <= 57 && base[2] !== undefined) return String.fromCodePoint(base[2] + c - 48);
    return ch;
  }).join("");
}

function convertMath(node: Node, key: string, variant?: string): ReactNode {
  if (node.nodeType === Node.TEXT_NODE) return variant ? inVariant(node.textContent ?? "", variant) : node.textContent;
  if (node.nodeType !== Node.ELEMENT_NODE) return null;
  const el = node as Element;
  const tag = el.localName;
  if (!MATH_TAGS.has(tag) || tag === "annotation") return null; // the TeX source is shown apart
  const p: Record<string, unknown> = { key };
  for (const name of MATH_ATTRS) {
    const value = el.getAttribute(name);
    if (value !== null && !/[<>()]|url|script/i.test(value)) p[MATH_PROP[name] ?? name] = value;
  }
  // The invisible "function application" after \\log, \\sin, ... gets the thin space
  // TeX puts there, which browsers leave out.
  if (tag === "mo" && el.textContent === "\u2061") p.rspace = "0.1667em";
  const own = el.getAttribute("mathvariant");
  const letters = own && own in VARIANTS && ["mi", "mn", "mtext"].includes(tag) ? own : variant;
  if (letters && own) p.mathVariant = "normal"; // the letters carry the alphabet now
  return createElement(tag, p, Array.from(el.childNodes, (c, i) => convertMath(c, `${key}.${i}`, letters)));
}

/** A formula as MathML, or its TeX source when KaTeX cannot read it. */
function MathTex({ tex, display }: { tex: string; display: boolean }) {
  const nodes = useMemo(() => {
    try {
      const html = katex.renderToString(tex, { output: "mathml", displayMode: display, throwOnError: true, trust: false, strict: "ignore", maxSize: 50, maxExpand: 500 });
      const doc = new DOMParser().parseFromString(`<body>${html}</body>`, "text/html");
      const math = doc.querySelector("math");
      return math ? convertMath(math, "math") : null;
    } catch {
      return null;
    }
  }, [tex, display]);
  if (nodes === null) return <code className="md-math-source">{display ? `$$${tex}$$` : `$${tex}$`}</code>;
  return display ? <div className="md-math" title={tex}>{nodes}</div> : <span className="md-math" title={tex}>{nodes}</span>;
}

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
  // A formula the Markdown held (``mathBlock``, ``mathInline``); raw <math> stays dropped.
  if ((tag === "span" || tag === "div") && el.hasAttribute("data-math")) {
    return <MathTex key={key} tex={el.getAttribute("data-math") ?? ""} display={el.hasAttribute("data-display")} />;
  }
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
