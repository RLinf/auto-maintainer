// A repository's Lessons tab (design 5.11): its review, priority, and agent lessons,
// each with who approved it; editing them (a maintainer's edit applies at once,
// anyone else's waits for a maintainer); and talking with an agent about a file,
// which summarizes and revises it into a draft that a maintainer reads here and
// applies or discards.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "./api";
import { ErrorBox, GrowingTextarea, RejectButton, Section, useAct } from "./components";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { Markdown } from "./markdown";
import { SharedTalk, type Seed } from "./talk";
import { Skeleton, useToast } from "./ui";

type Kind = "review" | "priority" | "agent";

interface Origin {
  change: string;
  title: string;
  on: string;
  approved_by: string[];
  asked_by: string | null;
}

interface Pending {
  id: string;
  title: string;
  description: string;
  summary: boolean;
  state: string;
  asked_by: string | null;
  created_at: string;
  sections: Draft;
}

interface LessonsFile {
  kind: Kind;
  heading: string;
  path: string;
  text: string;
  sections: { title: string; items: { text: string; from: Origin | null }[] }[];
  pending: Pending[];
}

interface LessonsView {
  repo: string;
  files: LessonsFile[];
  can_configure: boolean;
  has_profile: boolean;
}

/** Lessons by topic, as an edit or a draft holds them. */
type Draft = { title: string; items: string[] }[];

const READERS: Record<Kind, string> = {
  review: "Every review, follow-up, triage, and quality score of this repository reads them.",
  priority: "Only priority scoring reads them.",
  agent: "What agents learned from trouble in their own work here; the agents' later work reads them.",
};

export function LessonsTab({ repo }: { repo: string }) {
  const t = useT();
  const q = useQuery({ queryKey: ["lessons", repo], queryFn: () => api.get<LessonsView>(`/api/repos/${repo}/lessons`) });
  if (q.isLoading) return <Skeleton rows={6} />;
  if (!q.data) return <ErrorBox error={q.error} />;
  return (
    <div className="stack" style={{ gap: 16 }}>
      <span className="small muted">{t("Lessons are approved one at a time, as reviewers teach them. Edit them here, or have an agent summarize a list: it merges repeats and groups the rest, and a maintainer applies what it drafts.")}</span>
      {q.data.files.map((f) => (
        <LessonsCard key={f.kind} repo={repo} file={f} canConfigure={q.data.can_configure} hasProfile={q.data.has_profile} />
      ))}
    </div>
  );
}

function LessonsCard({ repo, file, canConfigure, hasProfile }: { repo: string; file: LessonsFile; canConfigure: boolean; hasProfile: boolean }) {
  const t = useT();
  const toast = useToast();
  const [draft, setDraft] = useState<Draft | null>(null);
  const count = file.sections.reduce((n, s) => n + s.items.length, 0);
  const keys = [["lessons", repo], ["memory"]];
  const save = useAct((sections: Draft) => api.put<{ applied: boolean }>(`/api/repos/${repo}/lessons/${file.kind}`, { base: file.text, sections }), keys);
  const [talk, setTalk] = useState<{ open: boolean; seed: Seed | null }>({ open: false, seed: null });
  const summarizeText = t("Summarize these lessons: merge the ones that say the same thing, group related ones under topics, and tell me which you drop and why.");
  const summarize = () => setTalk({ open: true, seed: { text: summarizeText, n: Date.now(), send: true } });
  const edit = () => setDraft(file.sections.length ? file.sections.map((s) => ({ title: s.title, items: s.items.map((i) => i.text) })) : [{ title: "", items: [""] }]);
  const submit = () =>
    draft &&
    save.mutate(draft, {
      onSuccess: (out) => {
        toast({ title: out.applied ? t("Saved") : t("Waiting for a maintainer"), body: out.applied ? t("Later agents read the lessons as you left them.") : t("A maintainer applies your edit on this tab."), icon: out.applied ? "check" : "inbox" });
        setDraft(null);
      },
      onError: (e) => toast({ title: t("Not saved"), body: String((e as Error).message), icon: "x" }),
    });
  return (
    <>
    <Section
      title={`${t(file.heading)} · ${count}`}
      hint={t(READERS[file.kind])}
      aside={
        hasProfile && !draft ? (
          <div className="row" style={{ gap: 6 }}>
            <button className="sm ghost" disabled={count < 2} title={count < 2 ? t("There is nothing to summarize yet.") : undefined} onClick={summarize}>
              <Icon name="sparkles" size={13} /> {t("Summarize")}
            </button>
            <button className={`sm ghost ${talk.open ? "on" : ""}`} aria-expanded={talk.open} onClick={() => setTalk({ ...talk, open: !talk.open })}>
              <Icon name="message" size={13} /> {t("Talk with the agent")}
            </button>
            <button className="sm ghost" onClick={edit}>
              <Icon name="pencil" size={13} /> {t("Edit")}
            </button>
          </div>
        ) : undefined
      }
    >
      <div className="stack" style={{ gap: 12 }}>
        {file.pending.map((p) => (
          <PendingLessons key={p.id} p={p} canConfigure={canConfigure} keys={keys} />
        ))}
        {draft ? (
          <LessonsEditor draft={draft} onChange={setDraft} />
        ) : count === 0 ? (
          <span className="small muted">{t("None yet.")}</span>
        ) : (
          file.sections.map((s, i) => (
            <div key={`${s.title}-${i}`} className="stack" style={{ gap: 6 }}>
              {s.title && <strong className="small">{s.title}</strong>}
              <ul className="lessons">
                {s.items.map((item, j) => (
                  <li key={j}>
                    <Markdown text={item.text} className="lesson-text" />
                    <span className="small muted" title={item.from?.title}>
                      {item.from
                        ? `${item.from.approved_by.length ? `${t("approved by")} ${item.from.approved_by.join(", ")}` : t("merged on its own")} · ${item.from.on}`
                        : t("written outside the dashboard")}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ))
        )}
        {draft && (
          <div className="row" style={{ gap: 8 }}>
            <span className="small muted">{canConfigure ? t("You are a maintainer: your edit applies at once.") : t("A maintainer applies your edit.")}</span>
            <span className="spacer" />
            <button className="sm ghost" onClick={() => setDraft(null)}>
              {t("Cancel")}
            </button>
            <button className="sm primary" disabled={save.isPending} onClick={submit}>
              {t("Save")}
            </button>
          </div>
        )}
        <ErrorBox error={save.error} />
      </div>
    </Section>
    {talk.open && (
      <SharedTalk
        base={`/api/repos/${repo}/lessons/${file.kind}/talk`}
        queryKey={["lessons", repo, file.kind, "talk"]}
        invalidate={[["lessons", repo], ["memory"]]}
        title={`${t("Talk with the agent")}: ${t(file.heading)}`}
        hint={t("shared by this repository's reviewers")}
        intro={t("Ask it to summarize these lessons, or to reword, merge, regroup, add, or drop some. Each revision it drafts waits above for a maintainer to apply.")}
        placeholder={t("For example: keep the CI lessons apart; merge the two about review length; drop the one about install scripts.")}
        enabled={hasProfile}
        task="learn"
        seed={talk.seed}
        actions={(send, busy) => (
          <>
            <button className="sm" disabled={busy || count < 2 || !hasProfile} onClick={() => send(summarizeText)}>
              <Icon name="sparkles" size={13} />
              {t("Summarize")}
            </button>
            <span className="small muted">{t("Nothing changes until a maintainer applies the draft.")}</span>
          </>
        )}
      />
    )}
    </>
  );
}

function LessonsEditor({ draft, onChange }: { draft: Draft; onChange: (d: Draft) => void }) {
  const t = useT();
  const set = (i: number, patch: Partial<Draft[number]>) => onChange(draft.map((s, k) => (k === i ? { ...s, ...patch } : s)));
  return (
    <div className="stack" style={{ gap: 14 }}>
      {draft.map((s, i) => (
        <div key={i} className="stack lessons-edit" style={{ gap: 6 }}>
          <div className="row" style={{ gap: 6 }}>
            <input value={s.title} placeholder={t("Topic (optional)")} aria-label={t("Topic")} onChange={(e) => set(i, { title: e.target.value })} style={{ flex: "1 1 auto", fontWeight: 600 }} />
            {draft.length > 1 && (
              <button className="ghost icon sm" title={t("Remove this topic and its lessons")} aria-label={t("Remove this topic and its lessons")} onClick={() => onChange(draft.filter((_, k) => k !== i))}>
                <Icon name="x" size={13} />
              </button>
            )}
          </div>
          {s.items.map((item, j) => (
            <div key={j} className="row" style={{ gap: 6, alignItems: "flex-start" }}>
              <GrowingTextarea value={item} minRows={1} aria-label={t("Lesson")} onChange={(e) => set(i, { items: s.items.map((x, k) => (k === j ? e.target.value : x)) })} style={{ flex: 1 }} />
              <button className="ghost icon sm" title={t("Remove this lesson")} aria-label={t("Remove this lesson")} onClick={() => set(i, { items: s.items.filter((_, k) => k !== j) })}>
                <Icon name="x" size={13} />
              </button>
            </div>
          ))}
          <button className="ghost sm" style={{ alignSelf: "flex-start" }} onClick={() => set(i, { items: [...s.items, ""] })}>
            <Icon name="plus" size={13} /> {t("Add a lesson")}
          </button>
        </div>
      ))}
      <button className="ghost sm" style={{ alignSelf: "flex-start" }} onClick={() => onChange([...draft, { title: "", items: [""] }])}>
        <Icon name="plus" size={13} /> {t("Add a topic")}
      </button>
    </div>
  );
}

/** An edit or a summary that waits: what it says, the lessons it would leave, and
 * Apply or Discard for a maintainer. */
function PendingLessons({ p, canConfigure, keys }: { p: Pending; canConfigure: boolean; keys: string[][] }) {
  const t = useT();
  const toast = useToast();
  const approve = useAct(() => api.post<{ state: string }>(`/api/memory/changes/${p.id}/approve`), keys);
  const reject = useAct((reason: string) => api.post(`/api/memory/changes/${p.id}/reject`, { reason }), keys);
  return (
    <div className="proposal" style={{ borderColor: "color-mix(in srgb, var(--accent) 35%, var(--border))" }}>
      <div className="row" style={{ gap: 8 }}>
        <Icon name={p.summary ? "sparkles" : "pencil"} />
        <strong>{p.summary ? t("The agent's summary") : t("An edit")}</strong>
        <span className="small muted right">
          {p.asked_by ?? ""} · {p.created_at.slice(0, 10)}
          {p.state === "conflict" ? ` · ${t("the lessons changed since; discard it and summarize again")}` : ""}
        </span>
      </div>
      {p.summary && p.description && <Markdown text={p.description} className="small" />}
      <details open={p.summary}>
        <summary className="small">
          {t("The lessons it leaves")} · {p.sections.reduce((n, s) => n + s.items.length, 0)}
        </summary>
        <div className="stack" style={{ gap: 6, marginTop: 6 }}>
          {p.sections.map((s, i) => (
            <div key={i} className="stack" style={{ gap: 4 }}>
              {s.title && <strong className="small">{s.title}</strong>}
              <ul className="lessons">
                {s.items.map((item, j) => (
                  <li key={j}>
                    <Markdown text={item} className="lesson-text" />
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </details>
      <div className="row" style={{ gap: 8 }}>
        {canConfigure ? (
          <button className="sm primary" disabled={approve.isPending || p.state !== "open"} onClick={() => approve.mutate(undefined, { onSuccess: (r) => toast({ title: r.state === "merged" ? t("Applied") : `${t("Approved")}: ${r.state}`, icon: "check" }) })}>
            <Icon name="check" size={13} /> {t("Apply")}
          </button>
        ) : (
          <span className="small muted">{t("A maintainer applies it.")}</span>
        )}
        <RejectButton label="Discard" size="sm" busy={reject.isPending} onReject={(reason) => reject.mutate(reason)} />
      </div>
      <ErrorBox error={approve.error || reject.error} />
    </div>
  );
}
