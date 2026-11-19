// The guide: how to use the dashboard, by task. A landing page that asks what the reader
// does and lays out a reading path for it, every topic by group, the common questions
// and the words used here; and a page per topic with its steps, buttons to the pages it
// is about, and the agent one click away. What a reader has opened is remembered in this
// browser only (a convenience; the guide works without it).

import { Rich } from "../rich";
import { ToursCard } from "../tour";
import { useEffect, useMemo, useState } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useAgent } from "../agent";
import { Empty, PageHead } from "../components";
import {
  FAQ,
  GLOSSARY,
  GROUPS,
  PATHS,
  ROLE_LABEL,
  TOPICS,
  plain,
  searchTopics,
  topicById,
  type Role,
  type Text,
  type Topic,
} from "../guide";
import { useLang, useT } from "../i18n";
import { Icon } from "../icons";

const READ_KEY = "am.guide.read";
const ROLE_KEY = "am.guide.role";
const BROWSE_KEY = "am.guide.browse";
/** How many topics of a reading path show before the whole path is asked for. */
const PATH_SHOWN = 4;

function readBrowse(): boolean {
  try {
    return localStorage.getItem(BROWSE_KEY) === "1";
  } catch {
    return false;
  }
}

function readSet(): Set<string> {
  try {
    const raw = localStorage.getItem(READ_KEY);
    const list: unknown = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(list) ? list.filter((x): x is string => typeof x === "string") : []);
  } catch {
    return new Set();
  }
}

function writeSet(read: Set<string>) {
  try {
    localStorage.setItem(READ_KEY, JSON.stringify([...read]));
  } catch {
    // storage can be unavailable; what was read is then forgotten with the page
  }
}

function readRole(): Exclude<Role, "everyone"> {
  try {
    const r = localStorage.getItem(ROLE_KEY);
    return PATHS.some((p) => p.role === r) ? (r as Exclude<Role, "everyone">) : "reviewer";
  } catch {
    return "reviewer";
  }
}

function useRead(): [Set<string>, (id: string) => void, () => void] {
  const [read, setRead] = useState(readSet);
  const mark = (id: string) =>
    setRead((old) => {
      if (old.has(id)) return old;
      const next = new Set(old).add(id);
      writeSet(next);
      return next;
    });
  const reset = () => {
    const next = new Set<string>();
    writeSet(next);
    setRead(next);
  };
  return [read, mark, reset];
}

function useText(): (text: Text) => string {
  const { lang } = useLang();
  return (text) => text[lang];
}

export default function GuidePage() {
  const { topic } = useParams();
  return topic ? <TopicPage id={topic} /> : <Landing />;
}

// ---- landing ------------------------------------------------------------------------------

function Landing() {
  const t = useT();
  const tx = useText();
  const { lang } = useLang();
  const [read, , reset] = useRead();
  const [query, setQuery] = useState("");
  const location = useLocation();
  // Every topic folds away until asked for, so a first visit shows only where to start;
  // a crumb to a group (#group-…) opens them.
  const [browse, setBrowseState] = useState(() => readBrowse() || location.hash.startsWith("#group-"));
  const setBrowse = (open: boolean) => {
    setBrowseState(open);
    try {
      localStorage.setItem(BROWSE_KEY, open ? "1" : "0");
    } catch {
      // the choice then lasts for this page only
    }
  };
  useEffect(() => {
    if (location.hash.startsWith("#group-")) setBrowseState(true);
  }, [location.hash]);
  // A crumb links to a part of the landing page (#group-…, #tours): scroll to it once shown.
  useEffect(() => {
    if (location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView({ block: "start" });
  }, [location.hash, browse]);
  const found = useMemo(() => (query.trim() ? searchTopics(query, lang) : null), [query, lang]);
  const faqs = useMemo(() => {
    const words = query.toLowerCase().split(/\s+/).filter(Boolean);
    if (!words.length) return FAQ;
    return FAQ.filter((f) => words.every((w) => `${f.q.en} ${f.q.zh} ${f.a.en} ${f.a.zh}`.toLowerCase().includes(w)));
  }, [query]);

  return (
    <div className="guide">
      <PageHead
        title={
          <span className="row" style={{ gap: 10 }}>
            <Icon name="compass" size={22} />
            {t("Guide")}
          </span>
        }
        sub={t("How to use the dashboard, step by step. Pick what you do here, or search for a task.")}
      />

      <label className="search guide-search">
        <Icon name="search" />
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t("Search the guide: review, merge, test machine…")}
          aria-label={t("Search the guide")}
        />
        {query && (
          <button className="ghost icon sm" onClick={() => setQuery("")} aria-label={t("Clear the search")}>
            <Icon name="x" size={14} />
          </button>
        )}
      </label>

      {found ? (
        <SearchResults topics={found} faqs={faqs} query={query} read={read} />
      ) : (
        <>
          <ToursCard />
          <StartHere read={read} onReset={reset} />
          <button className="guide-browse" aria-expanded={browse} onClick={() => setBrowse(!browse)}>
            <Icon name={browse ? "chevronDown" : "chevronRight"} size={15} />
            {browse ? t("Hide the other topics") : `${t("Browse every topic")} (${TOPICS.length})`}
            <span className="small muted">{t("Questions people ask and the words the dashboard uses are there too.")}</span>
          </button>
          {browse && (
            <>
              {GROUPS.map((g) => (
                <section key={g.id} className="stack" style={{ gap: 10 }} id={`group-${g.id}`}>
                  <div className="guide-group-head">
                    <Icon name={g.icon} />
                    <h2>{tx(g.title)}</h2>
                    <span className="small muted">{tx(g.blurb)}</span>
                  </div>
                  <div className="guide-tiles">
                    {TOPICS.filter((topic) => topic.group === g.id).map((topic) => (
                      <TopicTile key={topic.id} topic={topic} read={read.has(topic.id)} />
                    ))}
                  </div>
                </section>
              ))}
              <FaqSection faqs={FAQ} />
              <GlossarySection />
            </>
          )}
          <p className="small muted guide-foot">
            <Icon name="keyboard" size={14} /> {t("Press ? anywhere for every keyboard shortcut.")}
          </p>
        </>
      )}
    </div>
  );
}

function StartHere({ read, onReset }: { read: Set<string>; onReset: () => void }) {
  const t = useT();
  const tx = useText();
  const navigate = useNavigate();
  const [role, setRoleState] = useState(readRole);
  const setRole = (r: Exclude<Role, "everyone">) => {
    setRoleState(r);
    try {
      localStorage.setItem(ROLE_KEY, r);
    } catch {
      // the choice then lasts for this page only
    }
  };
  const [whole, setWhole] = useState(false);
  const path = PATHS.find((p) => p.role === role) ?? PATHS[0];
  const done = path.topics.filter((id) => read.has(id)).length;
  const next = path.topics.find((id) => !read.has(id));
  // A few topics from the next one to read; the whole path on request.
  const from = Math.max(0, next ? path.topics.indexOf(next) : 0);
  const shown = whole ? path.topics : path.topics.slice(from, from + PATH_SHOWN);
  return (
    <section className="card guide-start">
      <div className="row" style={{ marginBottom: 12, flexWrap: "wrap" }}>
        <Icon name="sparkles" />
        <h2>{t("Start here")}</h2>
        <span className="small muted">{t("What do you do here? The guide picks the topics for it, in order.")}</span>
      </div>
      <div className="guide-roles" role="radiogroup" aria-label={t("What do you do here?")}>
        {PATHS.map((p) => (
          <button
            key={p.role}
            role="radio"
            aria-checked={p.role === role}
            className={`guide-role ${p.role === role ? "on" : ""}`}
            onClick={() => setRole(p.role)}
          >
            <span className="row" style={{ gap: 8 }}>
              <Icon name={p.icon} />
              <strong>{tx(p.label)}</strong>
            </span>
            <span className="small muted">{tx(p.blurb)}</span>
          </button>
        ))}
      </div>

      <div className="guide-path-head">
        <div className="guide-meter" aria-hidden="true">
          <span style={{ width: `${(done / path.topics.length) * 100}%` }} />
        </div>
        <span className="small muted">
          {done} / {path.topics.length} {t("read")}
        </span>
        <span className="spacer" />
        {done > 0 && (
          <button className="ghost sm" onClick={onReset}>
            {t("Start over")}
          </button>
        )}
        <button className="primary" onClick={() => navigate(`/guide/${next ?? path.topics[0]}?path=${role}`)}>
          {next ? (done ? t("Continue") : t("Start")) : t("Read it again")}
          <Icon name="chevronRight" size={14} />
        </button>
      </div>

      <ol className="guide-path">
        {shown.map((id) => {
          const i = path.topics.indexOf(id);
          const topic = topicById(id);
          if (!topic) return null;
          const isRead = read.has(id);
          return (
            <li key={id}>
              <Link to={`/guide/${id}?path=${role}`} className={`guide-path-item ${isRead ? "done" : ""} ${id === next ? "next" : ""}`}>
                <span className={`step-dot ${isRead ? "done" : ""}`}>{isRead ? <Icon name="check" size={13} /> : i + 1}</span>
                <span className="stack" style={{ gap: 0, minWidth: 0 }}>
                  <span className="guide-path-title">{tx(topic.title)}</span>
                  <span className="small muted">{tx(topic.summary)}</span>
                </span>
              </Link>
            </li>
          );
        })}
      </ol>
      {path.topics.length > PATH_SHOWN && (
        <button className="ghost sm" onClick={() => setWhole(!whole)}>
          {whole ? t("Show only the next topics") : `${t("Show the whole path")} (${path.topics.length})`}
        </button>
      )}
    </section>
  );
}

function TopicTile({ topic, read }: { topic: Topic; read: boolean }) {
  const t = useT();
  const tx = useText();
  return (
    <Link to={`/guide/${topic.id}`} className="guide-tile">
      <span className="row" style={{ gap: 8 }}>
        <span className="guide-tile-icon">
          <Icon name={topic.icon} size={15} />
        </span>
        <strong className="grow">{tx(topic.title)}</strong>
        {read && (
          <span className="small muted row" style={{ gap: 3 }} title={t("You opened this topic")}>
            <Icon name="check" size={13} />
          </span>
        )}
      </span>
      <span className="small muted">{tx(topic.summary)}</span>
      <WhoPills who={topic.who} />
    </Link>
  );
}

function WhoPills({ who }: { who: Role[] }) {
  const tx = useText();
  return (
    <span className="guide-who">
      {who.map((r) => (
        <span key={r} className={`pill guide-role-pill ${r}`}>
          {tx(ROLE_LABEL[r])}
        </span>
      ))}
    </span>
  );
}

function SearchResults({ topics, faqs, query, read }: { topics: Topic[]; faqs: typeof FAQ; query: string; read: Set<string> }) {
  const t = useT();
  const agent = useAgent();
  return (
    <div className="stack" style={{ gap: 16 }}>
      {topics.length > 0 ? (
        <div className="guide-tiles">
          {topics.map((topic) => (
            <TopicTile key={topic.id} topic={topic} read={read.has(topic.id)} />
          ))}
        </div>
      ) : (
        !faqs.length && (
          <Empty icon="search">
            {t("Nothing in the guide matches.")}{" "}
            <button className="link-button" onClick={() => agent.open({ prompt: query.trim() })}>
              {t("Ask the agent instead")}
            </button>
          </Empty>
        )
      )}
      {faqs.length > 0 && <FaqSection faqs={faqs} />}
      {(topics.length > 0 || faqs.length > 0) && (
        <p className="small muted">
          {t("Not what you are looking for?")}{" "}
          <button className="link-button" onClick={() => agent.open({ prompt: query.trim() })}>
            <Icon name="sparkles" size={13} />
            {t("Ask the agent")}
          </button>
        </p>
      )}
    </div>
  );
}

function FaqSection({ faqs }: { faqs: typeof FAQ }) {
  const t = useT();
  const tx = useText();
  return (
    <section className="card" id="faq">
      <div className="card-head">
        <div className="row" style={{ gap: 8 }}>
          <Icon name="question" />
          <h2>{t("Questions people ask")}</h2>
        </div>
      </div>
      <div className="guide-faq">
        {faqs.map((f) => (
          <details key={f.q.en}>
            <summary>{tx(f.q)}</summary>
            <p>{tx(f.a)}</p>
          </details>
        ))}
      </div>
    </section>
  );
}

function GlossarySection() {
  const t = useT();
  const tx = useText();
  return (
    <section className="card" id="words">
      <div className="card-head">
        <div className="row" style={{ gap: 8 }}>
          <Icon name="book" />
          <h2>{t("Words used here")}</h2>
        </div>
      </div>
      <dl className="guide-gloss">
        {GLOSSARY.map((g) => (
          <div key={g.term.en}>
            <dt>{tx(g.term)}</dt>
            <dd>{tx(g.text)}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

// ---- one topic ----------------------------------------------------------------------------

function TopicPage({ id }: { id: string }) {
  const t = useT();
  const tx = useText();
  const agent = useAgent();
  const [read, mark] = useRead();
  const topic = topicById(id);
  const [params] = useSearchParams();
  const path = PATHS.find((p) => p.role === params.get("path") && p.topics.includes(id));
  const order = path ? path.topics : TOPICS.map((x) => x.id);
  const at = order.indexOf(id);
  const prev = at > 0 ? topicById(order[at - 1]) : undefined;
  const next = at >= 0 && at < order.length - 1 ? topicById(order[at + 1]) : undefined;
  const suffix = path ? `?path=${path.role}` : "";

  useEffect(() => {
    if (topic) mark(topic.id);
    window.scrollTo({ top: 0 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  if (!topic) {
    return (
      <Empty icon="search">
        {t("This topic is not in the guide.")} <Link to="/guide">{t("Back to the guide")}</Link>
      </Empty>
    );
  }
  const group = GROUPS.find((g) => g.id === topic.group)!;
  const prompt = topic.ask ? tx(topic.ask) : `${t("Walk me through this:")} ${tx(topic.title)}`;

  return (
    <div className="guide guide-layout">
      <Toc current={topic.id} read={read} />
      <article className="stack guide-article" style={{ gap: 18 }}>
        <PageHead
          crumbs={[
            { to: "/guide", label: t("Guide") },
            { to: `/guide#group-${group.id}`, label: tx(group.title) },
          ]}
          title={tx(topic.title)}
          sub={tx(topic.summary)}
        />
        {path && (
          <div className="guide-path-strip small">
            <Icon name={path.icon} size={14} />
            <span>
              {tx(path.label)} · {at + 1} / {path.topics.length}
            </span>
            <div className="guide-meter" aria-hidden="true">
              <span style={{ width: `${((at + 1) / path.topics.length) * 100}%` }} />
            </div>
          </div>
        )}
        <WhoPills who={topic.who} />

        <section className="card">
          <ol className="guide-steps">
            {topic.steps.map((s, i) => (
              <li key={i}>
                <div>
                  <Rich text={tx(s)} />
                </div>
              </li>
            ))}
          </ol>
        </section>

        {topic.tips && topic.tips.length > 0 && (
          <aside className="guide-tip">
            <strong className="row" style={{ gap: 6 }}>
              <Icon name="sparkles" size={14} />
              {t("Good to know")}
            </strong>
            {topic.tips.map((tip, i) => (
              <p key={i}>
                <Rich text={tx(tip)} />
              </p>
            ))}
          </aside>
        )}

        <div className="row guide-actions">
          {(topic.open ?? []).map((o, i) => (
            <Link key={o.to} to={o.to} className={`button ${i === 0 ? "primary" : ""}`}>
              {tx(o.label)}
              <Icon name="chevronRight" size={14} />
            </Link>
          ))}
          <button className="agent" onClick={() => agent.open({ prompt })} title={plain(prompt)}>
            <Icon name="sparkles" />
            {t("Ask the agent about this")}
          </button>
        </div>

        <nav className="guide-pager" aria-label={t("Guide")}>
          {prev ? (
            <Link to={`/guide/${prev.id}${suffix}`} className="guide-pager-link">
              <span className="small muted">← {t("Previous topic")}</span>
              <span>{tx(prev.title)}</span>
            </Link>
          ) : (
            <Link to="/guide" className="guide-pager-link">
              <span className="small muted">← {t("Guide")}</span>
              <span>{t("Back to the guide")}</span>
            </Link>
          )}
          {next ? (
            <Link to={`/guide/${next.id}${suffix}`} className="guide-pager-link next">
              <span className="small muted">{t("Next topic")} →</span>
              <span>{tx(next.title)}</span>
            </Link>
          ) : (
            <Link to="/guide" className="guide-pager-link next">
              <span className="small muted">{path ? t("You finished this path") : t("That was the last topic")}</span>
              <span>{t("Back to the guide")} →</span>
            </Link>
          )}
        </nav>
        <span className="small muted">
          {t("Something here does not match what you see?")}{" "}
          <button className="link-button" onClick={() => agent.open({ prompt: `${t("The guide says this, but I see something else:")} ${tx(topic.title)}. ` })}>
            {t("Tell the agent")}
          </button>
        </span>
      </article>
    </div>
  );
}

function Toc({ current, read }: { current: string; read: Set<string> }) {
  const t = useT();
  const tx = useText();
  const [open, setOpen] = useState(false);
  const list = (
    <nav className="guide-toc-list" aria-label={t("All topics")}>
      {GROUPS.map((g) => (
        <div key={g.id} className="stack" style={{ gap: 2 }}>
          <div className="menu-label">{tx(g.title)}</div>
          {TOPICS.filter((x) => x.group === g.id).map((x) => (
            <Link key={x.id} to={`/guide/${x.id}`} className={`guide-toc-item ${x.id === current ? "on" : ""}`} aria-current={x.id === current ? "page" : undefined} onClick={() => setOpen(false)}>
              <span className="grow">{tx(x.title)}</span>
              {read.has(x.id) && x.id !== current && <Icon name="check" size={12} />}
            </Link>
          ))}
        </div>
      ))}
    </nav>
  );
  return (
    <aside className="guide-toc">
      <Link to="/guide" className="guide-toc-home">
        <Icon name="compass" size={15} />
        {t("Guide")}
      </Link>
      <button className="guide-toc-toggle ghost sm" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <Icon name="list" size={14} />
        {t("All topics")}
        <Icon name="chevronDown" size={14} />
      </button>
      <div className={`guide-toc-body ${open ? "open" : ""}`}>{list}</div>
    </aside>
  );
}

