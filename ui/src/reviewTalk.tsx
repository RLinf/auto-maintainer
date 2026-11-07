// The conversation about a review, on the review's live view. Reviewers ask the agent
// why it found something, push back, have it revise the pending draft, or have it check
// more; questions the agent asks while it answers wait here for an answer. Everyone who
// may see the repository shares the conversation. A revision is saved on the draft as
// the edit of the reviewer who asked; nothing is posted until someone posts the draft.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { api, type AgentJob, type ChatMessage, type JobStep, type ReviewTalkState } from "./api";
import { ErrorBox } from "./components";
import { useQuestions } from "./data";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { Steps } from "./jobs";
import { Markdown } from "./markdown";
import { QuestionCard } from "./pages/Questions";
import { ChangeById } from "./settingsChange";

type TurnSteps = { run: string | null; live: boolean; steps: JobStep[] };

export function ReviewTalk({ job, me }: { job: AgentJob; me: string }) {
  const t = useT();
  const client = useQueryClient();
  const [text, setText] = useState("");
  const [pendingText, setPendingText] = useState("");
  const talk = useQuery({
    queryKey: ["job", job.id, "talk"],
    queryFn: () => api.get<ReviewTalkState>(`/api/jobs/${job.id}/talk`),
  });
  const send = useMutation({
    mutationFn: (said: string) => api.post<ChatMessage>(`/api/jobs/${job.id}/talk`, { text: said }),
    onSettled: () => {
      setPendingText("");
      // A revision changes the draft; a lesson adds a memory change.
      for (const queryKey of [["job", job.id, "talk"], ["proposals"], ["pr"], ["memory"]]) void client.invalidateQueries({ queryKey });
    },
  });
  const busy = send.isPending || !!talk.data?.live_run;
  const live = useQuery({
    queryKey: ["job", job.id, "talk", "live"],
    queryFn: () => api.get<TurnSteps>(`/api/jobs/${job.id}/talk/steps`),
    enabled: busy,
    refetchInterval: busy ? 1500 : false,
  });
  const questions = useQuestions("pending");
  const messages = talk.data?.messages ?? [];
  const runs = new Set([...messages.map((m) => m.run_id), live.data?.run, talk.data?.live_run].filter((x): x is string => !!x));
  const waiting = (questions.data ?? []).filter((q) => q.job_id && runs.has(q.job_id));
  const draft = talk.data?.draft ?? null;
  // The service keeps the message as soon as the turn starts; show it once.
  const last = messages[messages.length - 1];
  const showPending = !!pendingText && !(last?.role === "user" && last.text === pendingText);

  const submit = (said: string) => {
    const clean = said.trim();
    if (!clean || busy || !draft) return;
    setPendingText(clean);
    setText("");
    send.mutate(clean);
  };

  return (
    <section className="stack review-talk" style={{ gap: 10 }} aria-label={t("Talk with the agent about this review")}>
      <span className="kind-chip">
        <Icon name="message" size={13} /> {t("Talk with the agent about this review")}
      </span>
      {!messages.length && !pendingText && (
        <span className="small muted">
          {draft
            ? t("Ask why it found something, disagree with a finding, have it change the draft, or ask it to check more. Changes are saved on the draft; nothing is posted until someone posts it.")
            : t("The conversation opens once the review has a draft.")}
        </span>
      )}
      {messages.map((m) => (
        <Fragment key={m.id}>
          {m.role === "user" ? (
            <div className="bubble user">
              <span className="small" style={{ display: "block", opacity: 0.75 }}>
                {m.author === me ? t("you") : m.author}
              </span>
              {m.text}
            </div>
          ) : (
            <div className="bubble assistant">
              <Markdown text={m.text} className="chat-md" />
            </div>
          )}
          {m.role !== "user" && draft && m.proposals.includes(draft) && (
            <span className="small" style={{ color: "var(--ok)" }}>
              <Icon name="check" size={13} /> {t("Revised the draft above")}
            </span>
          )}
          {(m.changes ?? []).map((id) => (
            <ChangeById key={id} id={id} />
          ))}
          {m.run_id && m.role !== "user" && <TurnStepsLink jobId={job.id} run={m.run_id} />}
        </Fragment>
      ))}
      {showPending && <div className="bubble user">{pendingText}</div>}
      {busy &&
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
      {draft && !talk.data?.draft_pending && (
        <span className="small muted">{t("This draft was already decided: the agent still answers, but its revisions are not saved.")}</span>
      )}
      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          submit(text);
        }}
      >
        <textarea
          value={text}
          rows={Math.min(6, Math.max(2, text.split("\n").length))}
          placeholder={t("Ask about the review, or ask for a change…")}
          aria-label={t("Message")}
          disabled={!draft}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit(text);
            }
          }}
        />
        <button className="primary icon" disabled={!text.trim() || busy || !draft} aria-label={t("Send")}>
          <Icon name="send" size={15} />
        </button>
      </form>
      <ErrorBox error={send.error || talk.error} />
    </section>
  );
}

/** What the agent did for one answer, read when the person opens it. */
function TurnStepsLink({ jobId, run }: { jobId: string; run: string }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const steps = useQuery({
    queryKey: ["job", jobId, "talk", "steps", run],
    queryFn: () => api.get<TurnSteps>(`/api/jobs/${jobId}/talk/steps?run=${encodeURIComponent(run)}`),
    enabled: open,
  });
  return (
    <details className="run-steps" onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}>
      <summary className="small muted">{t("What the agent did")}</summary>
      {steps.data ? (
        steps.data.steps.length ? (
          <Steps steps={steps.data.steps} active={false} />
        ) : (
          <span className="small muted">{t("Its steps are no longer kept.")}</span>
        )
      ) : (
        <span className="small muted">…</span>
      )}
    </details>
  );
}
