// The conversation about a review, on the review's live view. While the agent writes
// the review, a message reaches it at work and the review it finishes answers it. Once
// there is a draft, reviewers ask the agent why it found something, push back, have it
// revise the draft, or have it check more; questions the agent asks while it answers
// wait here for an answer. Everyone who may see the repository shares the conversation.
// A revision is saved on a pending draft as the edit of the reviewer who asked; for a
// review already posted it becomes a draft that edits the posted review once whoever
// posted it approves. Nothing is posted until someone decides.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { api, type AgentJob, type ChatMessage, type JobStep, type ReviewTalkState } from "./api";
import { ErrorBox, GrowingTextarea, StopButton } from "./components";
import { useProposals, useQuestions } from "./data";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { Steps } from "./jobs";
import { Markdown } from "./markdown";
import { QuestionCard } from "./pages/Questions";
import { ProposalCard } from "./proposal";
import { openJob } from "./openJob";
import { ChangeById } from "./settingsChange";

type TurnSteps = { run: string | null; live: boolean; steps: JobStep[] };

export function ReviewTalk({ job, me, active }: { job: AgentJob; me: string; active: boolean }) {
  const t = useT();
  const client = useQueryClient();
  const [text, setText] = useState("");
  const [pendingText, setPendingText] = useState("");
  const talk = useQuery({
    queryKey: ["job", job.id, "talk"],
    queryFn: () => api.get<ReviewTalkState>(`/api/jobs/${job.id}/talk`),
    // While the review is written, whether its agent takes messages changes step by step.
    refetchInterval: active ? 3000 : false,
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
  const listening = !!talk.data?.listening;
  const canSend = !!draft || listening;
  const proposals = useProposals();
  const revision = talk.data?.revision ? (proposals.data ?? []).find((p) => p.action_id === talk.data?.revision) : undefined;
  // The service keeps the message as soon as the turn starts; show it once.
  const last = messages[messages.length - 1];
  const showPending = !!pendingText && !(last?.role === "user" && last.text === pendingText);

  // While the agent answers, a new message interrupts it (or, while it writes the
  // review, goes to it at work).
  const submit = (said: string) => {
    const clean = said.trim();
    if (!clean || !canSend) return;
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
          {listening
            ? t("The agent is writing the review. Tell it what to check or what to leave out: your message reaches it at work, and the review it finishes answers it.")
            : draft
              ? talk.data?.posted
                ? t("Ask why it found something, disagree with a finding, or ask it to check more. This review is posted: a change becomes a draft that edits the posted review once it is approved.")
                : t("Ask why it found something, disagree with a finding, have it change the draft, or ask it to check more. Changes are saved on the draft; nothing is posted until someone posts it.")
              : active
                ? t("You can talk to the agent once it starts writing the review.")
                : t("The conversation opens once the review has a draft.")}
        </span>
      )}
      {messages.map((m) => (
        <Fragment key={m.id}>
          {m.role === "user" ? (
            <div className="bubble user">
              <span className="small" style={{ display: "block", opacity: 0.75 }}>
                {m.author === me ? t("you") : m.author}
                {m.while_running && ` · ${t("sent while it reviewed")}`}
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
          {m.role !== "user" && m.proposals.some((id) => id !== draft) && (
            <span className="small" style={{ color: "var(--ok)" }}>
              <Icon name="check" size={13} /> {t("Drafted an edit of the posted review")}
            </span>
          )}
          {(m.changes ?? []).map((id) => (
            <ChangeById key={id} id={id} />
          ))}
          {m.role !== "user" &&
            (m.jobs ?? []).map((id) => (
              <button key={id} className="ghost sm" style={{ alignSelf: "flex-start" }} onClick={() => openJob(id)}>
                <Icon name="server" size={13} /> {t("Watch the reproduction")}
              </button>
            ))}
          {m.run_id && m.role !== "user" && <TurnStepsLink jobId={job.id} run={m.run_id} />}
        </Fragment>
      ))}
      {showPending && <div className="bubble user">{pendingText}</div>}
      {last?.role === "user" && last.while_running && listening && !send.isPending && (
        <span className="small muted">{t("The agent has your message; the review it finishes answers it.")}</span>
      )}
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
      {revision && (
        <section className="stack" style={{ gap: 8 }}>
          <span className="kind-chip">{t("Edit of the posted review, waiting for a decision")}</span>
          <ProposalCard proposal={revision} compact />
        </section>
      )}
      {draft && !talk.data?.draft_pending && !talk.data?.posted && (
        <span className="small muted">{t("This draft was already decided: the agent still answers, but its revisions are not saved.")}</span>
      )}
      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          submit(text);
        }}
      >
        <GrowingTextarea
          value={text}
          minRows={2}
          placeholder={listening ? t("Tell the agent what to check while it reviews…") : t("Ask about the review, or ask for a change…")}
          aria-label={t("Message")}
          disabled={!canSend}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit(text);
            }
          }}
        />
        {busy && !listening && <StopButton url={`/api/jobs/${job.id}/talk/stop`} />}
        <button className="primary icon" disabled={!text.trim() || !canSend} aria-label={t("Send")} title={busy && !listening ? t("Stop the agent and send this instead") : undefined}>
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
