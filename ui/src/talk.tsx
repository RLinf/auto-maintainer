// A conversation with an agent that a repository's reviewers share: about its People
// record (``people.tsx``) or one of its lessons files (``lessons.tsx``). Everyone sees
// the same messages; a message sent while the agent works goes to it at work, or
// stops it and starts again; the latest exchange shows and earlier ones open on
// request; questions the agent asks wait here for the person who wrote; and what the
// agent drafts waits on the tab for a maintainer.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useEffect, useRef, useState, type ReactNode } from "react";
import { AgentPicker, agentName } from "./agentPicker";
import { api, type ChatMessage, type JobStep } from "./api";
import { ErrorBox, GrowingTextarea, Section, StopButton } from "./components";
import { useQuestions } from "./data";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { Steps } from "./jobs";
import { Markdown } from "./markdown";
import { QuestionCard } from "./pages/Questions";

const MESSAGES_SHOWN = 2;
const QUESTIONS_SHOWN = 3;
const CLAMP_CHARS = 700;

/** A long answer, cut to a few lines until the reader opens it. */
export function Clamped({ text }: { text: string }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const long = text.length > CLAMP_CHARS || text.split("\n").length > 12;
  return (
    <>
      <div className={long && !open ? "clamped" : undefined}>
        <Markdown text={text} className="chat-md" />
      </div>
      {long && (
        <button className="ghost sm" style={{ marginTop: 4 }} onClick={() => setOpen(!open)}>
          {open ? t("Show less") : t("Show more")}
        </button>
      )}
    </>
  );
}

type TalkState = {
  thread: string | null;
  agent?: string | null;
  runtime?: string | null;
  live_run: string | null;
  listening: boolean;
  messages: ChatMessage[];
};
type TurnSteps = { run: string | null; live: boolean; steps: JobStep[] };

/** The newest seed each conversation (by its ``base``) has taken. */
const consumed = new Map<string, number>();

/** A message to put in the box (``n`` changes for each new one), or to send. */
export interface Seed {
  text: string;
  n: number;
  send?: boolean;
}

export function SharedTalk({
  base,
  queryKey,
  invalidate,
  title,
  hint,
  intro,
  placeholder,
  enabled,
  task,
  questions = [],
  seed,
  actions,
}: {
  /** The conversation's API: ``GET`` it, ``POST`` a message, ``/steps``, ``/stop``. */
  base: string;
  queryKey: string[];
  /** What a turn may change, reloaded after it. */
  invalidate: string[][];
  title: string;
  hint?: string;
  /** Shown before the first message. */
  intro: ReactNode;
  placeholder: string;
  enabled: boolean;
  /** The task kind whose agents the picker offers. */
  task: string;
  questions?: string[];
  seed?: Seed | null;
  /** Buttons beside the agent picker, given a way to send a message and whether the agent is busy. */
  actions?: (send: (text: string) => void, busy: boolean) => ReactNode;
}) {
  const t = useT();
  const client = useQueryClient();
  const box = useRef<HTMLTextAreaElement>(null);
  const [text, setText] = useState("");
  const [pendingText, setPendingText] = useState("");
  const [pickedAgent, setPickedAgent] = useState<string | undefined>(undefined);
  const [allMessages, setAllMessages] = useState(false);
  const [allQuestions, setAllQuestions] = useState(false);
  const log = useRef<HTMLDivElement>(null);
  const talk = useQuery({ queryKey, queryFn: () => api.get<TalkState>(base) });
  const listening = !!talk.data?.listening;
  const agent = pickedAgent ?? talk.data?.agent ?? talk.data?.runtime ?? "";
  const send = useMutation({
    mutationFn: ({ said, chosen }: { said: string; chosen: string | undefined }) =>
      api.post<ChatMessage>(base, {
        text: said,
        ...(chosen !== undefined && !listening ? { agent: chosen } : {}),
      }),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey });
      setPickedAgent(undefined);
    },
    onSettled: () => {
      setPendingText("");
      for (const key of invalidate) void client.invalidateQueries({ queryKey: key });
    },
  });
  const running = send.isPending || !!talk.data?.live_run;
  // While a turn runs, others' messages and whether the agent takes one move too.
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => void client.invalidateQueries({ queryKey, exact: true }), 2000);
    return () => window.clearInterval(timer);
  }, [running, client, queryKey]);
  const live = useQuery({
    queryKey: [...queryKey, "live"],
    queryFn: () => api.get<TurnSteps>(`${base}/steps`),
    enabled: running,
    refetchInterval: running ? 1500 : false,
  });
  const busy = running && !listening;
  const pendingQuestions = useQuestions("pending");
  const messages = talk.data?.messages ?? [];
  const runs = new Set([...messages.map((m) => m.run_id), live.data?.run, talk.data?.live_run].filter((x): x is string => !!x));
  const waiting = (pendingQuestions.data ?? []).filter((q) => q.job_id && runs.has(q.job_id));
  const last = messages[messages.length - 1];
  // The latest exchange shows; earlier messages open on request, in a box of their own.
  const latest = messages.length - Math.max(0, messages.map((m) => m.role).lastIndexOf("user"));
  const earlier = Math.max(0, messages.length - Math.max(MESSAGES_SHOWN, latest));
  useEffect(() => {
    if (allMessages && log.current) log.current.scrollTop = log.current.scrollHeight;
  }, [allMessages, messages.length]);
  const showPending = !!pendingText && !(last?.role === "user" && last.text === pendingText);

  // While the agent answers, a new message interrupts it (or goes to it at work,
  // when it takes messages).
  const submit = (said: string) => {
    const clean = said.trim();
    if (!clean || !enabled) return;
    if (!listening) setPendingText(clean);
    setText("");
    send.mutate({ said: clean, chosen: pickedAgent });
  };
  // A message started elsewhere on the page: put in the box, or sent at once; each
  // once, even when the conversation opens again.
  useEffect(() => {
    if (!seed || consumed.get(base) === seed.n) return;
    consumed.set(base, seed.n);
    if (seed.send) {
      submit(seed.text);
      return;
    }
    setText(seed.text);
    window.setTimeout(() => {
      box.current?.focus();
      box.current?.setSelectionRange(seed.text.length, seed.text.length);
    }, 50);
  }, [seed?.n]);
  const answer = (q: string) => {
    setText(`${q}\n→ `);
    box.current?.focus();
  };

  return (
    <Section title={title} hint={hint}>
      <div className="stack review-talk" style={{ gap: 10 }}>
        {!messages.length && !pendingText && <span className="small muted">{intro}</span>}
        {earlier > 0 && (
          <button className="ghost sm" style={{ alignSelf: "flex-start" }} onClick={() => setAllMessages(!allMessages)}>
            <Icon name={allMessages ? "chevronDown" : "chevronRight"} size={13} />
            {allMessages ? t("Show only the latest") : `${t("Show earlier messages")} (${earlier})`}
          </button>
        )}
        <div ref={log} className={`stack talk-log ${allMessages ? "all" : ""}`} style={{ gap: 10 }}>
          {(allMessages ? messages : messages.slice(earlier)).map((m) => (
            <Fragment key={m.id}>
              {m.role === "user" ? (
                <div className="bubble user">
                  <span className="small" style={{ display: "block", opacity: 0.75 }}>
                    {m.author}
                    {m.while_running && ` · ${t("sent while it worked")}`}
                  </span>
                  {m.text}
                </div>
              ) : (
                <div className="bubble assistant">
                  {m.runtime && (
                    <span className="small muted" style={{ display: "block", marginBottom: 2 }}>
                      {agentName(m.runtime)}
                    </span>
                  )}
                  <Clamped text={m.text} />
                </div>
              )}
              {m.role !== "user" && (m.changes ?? []).length > 0 && (
                <span className="small" style={{ color: "var(--ok)" }}>
                  <Icon name="check" size={13} /> {t("Drafted a revision; it waits below for a maintainer")}
                </span>
              )}
            </Fragment>
          ))}
        </div>
        {showPending && <div className="bubble user">{pendingText}</div>}
        {running &&
          (live.data?.steps.length ? (
            <div className="agent-live" aria-live="polite">
              <Steps steps={live.data.steps} active />
            </div>
          ) : (
            <div className="bubble assistant thinking" aria-live="polite">
              <span className="typing" aria-label={t("The agent is working")}>
                <span />
                <span />
                <span />
              </span>
            </div>
          ))}
        {waiting.length > 0 && (
          <section className="stack" style={{ gap: 8 }}>
            <span className="kind-chip warn">{t("The agent is waiting for an answer")}</span>
            {waiting.map((q) => (
              <QuestionCard key={q.id} question={q} compact />
            ))}
          </section>
        )}
        {questions.length > 0 && (
          <div className="stack open-questions" style={{ gap: 6 }}>
            <span className="kind-chip">{t("The agent asks")}</span>
            {(allQuestions ? questions : questions.slice(0, QUESTIONS_SHOWN)).map((q) => (
              <div key={q} className="row" style={{ gap: 8, alignItems: "flex-start" }}>
                <span className="small" style={{ flex: 1, minWidth: 0 }}>
                  {q}
                </span>
                <button className="sm" onClick={() => answer(q)}>
                  {t("Answer")}
                </button>
              </div>
            ))}
            {questions.length > QUESTIONS_SHOWN && (
              <button className="ghost sm" style={{ alignSelf: "flex-start" }} onClick={() => setAllQuestions(!allQuestions)}>
                {allQuestions ? t("Show fewer questions") : `${t("Show all questions")} (${questions.length})`}
              </button>
            )}
          </div>
        )}
        <form
          className="composer"
          onSubmit={(e) => {
            e.preventDefault();
            submit(text);
          }}
        >
          <GrowingTextarea
            ref={box}
            value={text}
            minRows={2}
            placeholder={listening ? t("Tell the agent something while it works…") : placeholder}
            aria-label={t("Message")}
            disabled={!enabled}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                submit(text);
              }
            }}
          />
          {busy && <StopButton url={`${base}/stop`} />}
          <button className="primary icon" disabled={!text.trim() || !enabled} aria-label={t("Send")} title={busy ? t("Stop the agent and send this instead") : undefined}>
            <Icon name="send" size={15} />
          </button>
        </form>
        <div className="row" style={{ gap: 8, flexWrap: "wrap", justifyContent: "space-between" }}>
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            {actions?.(submit, busy)}
          </div>
          <AgentPicker compact purpose="answer" task={task} value={agent} onChange={setPickedAgent} disabled={listening || !enabled} />
        </div>
        <ErrorBox error={send.error || talk.error} />
      </div>
    </Section>
  );
}
