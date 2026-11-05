// Agents' questions: permission prompts and questions that agents asked while they ran.
// A reviewer sees exactly what the agent wants and answers; an unanswered question is
// denied when it expires.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type AgentQuestion, type InputQuestion, type QuestionOption } from "../api";
import { Empty, ErrorBox, Loading, Pre, Status, useAct, When, prPath } from "../components";
import { useT } from "../i18n";

const KEYS = [["questions"], ["me"], ["audit"]];

export function useQuestions(state: string) {
  return useQuery({
    queryKey: ["questions", state],
    queryFn: () => api.get<AgentQuestion[]>(`/api/questions?state=${state}`),
  });
}

export default function QuestionsPage() {
  const t = useT();
  const pending = useQuestions("pending");
  const answered = useQuestions("answered");
  const canNotify = typeof Notification !== "undefined" && Notification.permission === "default";

  if (pending.isLoading) return <Loading />;
  return (
    <>
      <h1>{t("Agent questions")}</h1>
      <p className="muted">
        Agents run in their CLI&apos;s auto mode. What they would ask a person waits here; an unanswered
        question is denied when it expires.
      </p>
      {canNotify && (
        <button onClick={() => void Notification.requestPermission()}>{t("Alert me in the browser")}</button>
      )}
      <ErrorBox error={pending.error} />
      <section className="stack">
        <h2>{t("Waiting for an answer")}</h2>
        {(pending.data ?? []).length === 0 && <Empty>{t("No agent is waiting for an answer.")}</Empty>}
        {(pending.data ?? []).map((q) => (
          <QuestionCard key={q.id} question={q} />
        ))}
      </section>
      <section className="stack">
        <h2>{t("Recently answered")}</h2>
        {(answered.data ?? []).slice(0, 20).map((q) => (
          <div key={q.id} className="card">
            <div className="row">
              <Status value={q.answer?.allow ? "allowed" : "denied"} />
              <strong>{q.title}</strong>
              <span className="muted small">
                {q.runtime} · {q.answer?.by} · <When at={q.answer?.at} />
              </span>
            </div>
            {q.answer?.message && <div className="muted small">{q.answer.message}</div>}
          </div>
        ))}
      </section>
    </>
  );
}

function Where({ q }: { q: AgentQuestion }) {
  if (!q.repo) return <span className="muted small">service</span>;
  if (q.pr) return <Link to={prPath(q.repo, q.pr)}>{`${q.repo}#${q.pr}`}</Link>;
  return <Link to={`/repos/${q.repo}`}>{q.repo}</Link>;
}

function QuestionCard({ question: q }: { question: AgentQuestion }) {
  const t = useT();
  const [message, setMessage] = useState("");
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const answer = useAct(
    (body: { allow: boolean; option_id?: string; answers?: Record<string, string[]>; message?: string }) =>
      api.post(`/api/questions/${q.id}/answer`, body),
    KEYS,
  );
  const pick = (option: QuestionOption) =>
    answer.mutate({
      allow: option.kind === "allow" || option.kind === "allow_session",
      option_id: option.id,
      message,
    });
  const options: QuestionOption[] = q.options.length
    ? q.options
    : [
        { id: "allow", label: "Allow", kind: "allow" },
        { id: "deny", label: t("Deny"), kind: "deny" },
      ];

  return (
    <article className="card stack">
      <header className="card-head">
        <h3>{q.title}</h3>
        <span className="muted small">
          {q.runtime} · {q.task_kind} · <Where q={q} />
        </span>
      </header>
      {q.detail && <Pre text={q.detail} />}
      {q.paths.length > 0 && (
        <ul className="small">
          {q.paths.map((p) => (
            <li key={p}>
              <code>{p}</code>
            </li>
          ))}
        </ul>
      )}
      <div className="muted small">
        {t("Expires")}: <When at={q.expires_at} />
      </div>
      {q.kind === "input" ? (
        <InputForm
          inputs={q.inputs}
          values={answers}
          onChange={setAnswers}
          onSubmit={() =>
            answer.mutate({
              allow: true,
              answers: Object.fromEntries(Object.entries(answers).map(([k, v]) => [k, [v]])),
              message,
            })
          }
          onDeny={() => answer.mutate({ allow: false, message })}
          busy={answer.isPending}
        />
      ) : (
        <>
          <textarea
            aria-label={t("Message to the agent (optional)")}
            placeholder={t("Message to the agent (optional)")}
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            rows={2}
          />
          <div className="row">
            {options.map((o) => (
              <button
                key={o.id}
                className={o.kind === "allow" || o.kind === "allow_session" ? "primary" : ""}
                disabled={answer.isPending}
                onClick={() => pick(o)}
              >
                {o.label}
              </button>
            ))}
          </div>
        </>
      )}
      <ErrorBox error={answer.error} />
    </article>
  );
}

function InputForm({
  inputs,
  values,
  onChange,
  onSubmit,
  onDeny,
  busy,
}: {
  inputs: InputQuestion[];
  values: Record<string, string>;
  onChange: (v: Record<string, string>) => void;
  onSubmit: () => void;
  onDeny: () => void;
  busy: boolean;
}) {
  const t = useT();
  const complete = inputs.every((i) => (values[i.id] ?? "").trim());
  return (
    <div className="stack">
      {inputs.map((i) => (
        <fieldset key={i.id} className="stack">
          <legend>
            {i.header && <strong>{i.header}: </strong>}
            {i.question}
          </legend>
          <div className="row">
            {i.options.map((label) => (
              <label key={label} className="row">
                <input
                  type="radio"
                  name={i.id}
                  checked={values[i.id] === label}
                  onChange={() => onChange({ ...values, [i.id]: label })}
                />
                {label}
              </label>
            ))}
          </div>
          {i.free_text && (
            <input
              type="text"
              aria-label={i.question}
              value={i.options.includes(values[i.id] ?? "") ? "" : (values[i.id] ?? "")}
              onChange={(e) => onChange({ ...values, [i.id]: e.target.value })}
            />
          )}
        </fieldset>
      ))}
      <div className="row">
        <button className="primary" disabled={busy || !complete} onClick={onSubmit}>
          {t("Send answers")}
        </button>
        <button disabled={busy} onClick={onDeny}>
          {t("Deny")}
        </button>
      </div>
    </div>
  );
}
