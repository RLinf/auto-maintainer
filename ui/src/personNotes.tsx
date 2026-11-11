// Settings → Agents → My preferences: the person's own notes on what they want from
// the agents (playbooks/person_notes.py), each about the reviews drafted for them, how
// the agents talk with them, or both. Learned notes come from everything they say in
// conversations and do with their drafts; any note they write or change is theirs, and
// learning keeps it as it is. Only what the agents do for them reads these.

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "./api";
import { ErrorBox, Section, useAct } from "./components";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { Skeleton, useToast } from "./ui";

type About = "reviews" | "conversations" | "both";

interface Note {
  text: string;
  about: About;
  source: "learned" | "person";
  evidence: number;
}

interface Window {
  posted: number;
  edited: number;
}

interface NotesView {
  login: string;
  learning: boolean;
  notes: Note[];
  learned_to: string | null;
  new_signals: number;
  progress: { recent: Window; before: Window };
}

const KEY = ["my-notes"];

export function MyNotesSection() {
  const t = useT();
  const toast = useToast();
  const q = useQuery({ queryKey: KEY, queryFn: () => api.get<NotesView>("/api/me/notes") });
  const [draft, setDraft] = useState<Note[] | null>(null);
  const [adding, setAdding] = useState("");
  useEffect(() => setDraft(null), [q.data]);
  const save = useAct((body: { learning: boolean; notes: Note[] }) => api.put<NotesView>("/api/me/notes", body), [KEY]);
  const learn = useAct(() => api.post<{ event: string }>("/api/me/notes/learn", {}), [KEY, ["work"]]);
  if (q.isLoading) return <Skeleton rows={3} />;
  if (!q.data) return <ErrorBox error={q.error} />;
  const v = q.data;
  const notes = draft ?? v.notes;
  const changed = draft !== null;
  const put = (next: Note[], learning = v.learning) => save.mutate({ learning, notes: next }, { onSuccess: () => toast({ title: t("Saved"), icon: "check" }) });
  const edit = (i: number, change: Partial<Note>) => setDraft(notes.map((n, j) => (j === i ? { ...n, ...change, source: "person" } : n)));
  const aboutLabel: Record<About, string> = { reviews: t("Reviews"), conversations: t("Conversations"), both: t("Both") };
  const share = (w: Window) => (w.posted ? `${w.edited}/${w.posted}` : "—");
  return (
    <Section
      title={t("My preferences")}
      hint={t("What you want from the agents: in the reviews drafted for you, and in conversations. Learned from everything you say in conversations and do with your drafts; only what the agents do for you reads them.")}
      aside={
        <label className="row small" style={{ gap: 6 }}>
          <input type="checkbox" checked={v.learning} disabled={save.isPending} onChange={(e) => put(notes, e.target.checked)} />
          {t("Learn from what I say and do")}
        </label>
      }
    >
      <div className="stack" style={{ gap: 10 }}>
        {notes.length === 0 ? (
          <span className="small muted">{t("No preferences yet. They appear as you talk with the agents and edit your drafts, or add one here, or tell the agent in a conversation.")}</span>
        ) : (
          <div className="note-list">
            {notes.map((n, i) => (
              <div key={i} className="note-row">
                <input value={n.text} maxLength={300} onChange={(e) => edit(i, { text: e.target.value })} aria-label={t("Preference")} />
                <select value={n.about} onChange={(e) => edit(i, { about: e.target.value as About })} aria-label={t("What it is about")} title={t("What it is about")}>
                  {(["reviews", "conversations", "both"] as const).map((a) => (
                    <option key={a} value={a}>
                      {aboutLabel[a]}
                    </option>
                  ))}
                </select>
                <span className={`pill${n.source === "learned" ? " status-info" : ""}`} title={n.source === "learned" ? t("Learned; how many of your messages, edits, and rejections back it") : t("Yours: learning keeps it as it is")}>
                  {n.source === "learned" ? `${t("learned")} · ${n.evidence}` : t("yours")}
                </span>
                <button className="ghost icon" aria-label={t("Remove")} title={t("Remove")} onClick={() => setDraft(notes.filter((_, j) => j !== i))}>
                  <Icon name="x" size={14} />
                </button>
              </div>
            ))}
          </div>
        )}
        <form
          className="row"
          style={{ gap: 6 }}
          onSubmit={(e) => {
            e.preventDefault();
            if (!adding.trim()) return;
            setDraft([...notes, { text: adding.trim(), about: "both", source: "person", evidence: 0 }]);
            setAdding("");
          }}
        >
          <input value={adding} onChange={(e) => setAdding(e.target.value)} placeholder={t("For example: leave out style nits; answer me briefly, in Chinese")} maxLength={300} style={{ flex: "1 1 240px" }} />
          <button className="sm" disabled={!adding.trim()}>
            <Icon name="plus" size={13} />
            {t("Add")}
          </button>
          {changed && (
            <>
              <button type="button" className="ghost sm" onClick={() => setDraft(null)}>
                {t("Cancel")}
              </button>
              <button type="button" className="primary sm" disabled={save.isPending} onClick={() => put(notes.filter((n) => n.text.trim()))}>
                {t("Save")}
              </button>
            </>
          )}
        </form>
        <span className="small muted row" style={{ gap: 8, flexWrap: "wrap" }}>
          <span>
            {t("Review drafts you edited before posting")}: {share(v.progress.recent)} {t("in the last 30 days")} · {share(v.progress.before)} {t("the 30 before")}
          </span>
          {v.learning && v.new_signals > 0 && (
            <button className="link-button" style={{ color: "var(--accent)" }} disabled={learn.isPending} onClick={() => learn.mutate(undefined, { onSuccess: () => toast({ title: t("Learning started"), icon: "sparkles" }) })}>
              {t("Learn now from")} {v.new_signals} {v.new_signals === 1 ? t("new signal") : t("new signals")}
            </button>
          )}
        </span>
        <ErrorBox error={save.error || learn.error} />
      </div>
    </Section>
  );
}
