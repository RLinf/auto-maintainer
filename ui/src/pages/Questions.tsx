// Agents' questions: permission prompts and questions agents asked while they ran. A
// reviewer sees exactly what the agent wants and answers; an unanswered question is
// denied when it expires.

import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type AgentQuestion, type InputQuestion, type QuestionOption } from "../api";
import { CaughtUp, ErrorBox, PageHead, Pre, prPath, relative, Status, timeOf, useAct, When } from "../components";
import { useMe, useQuestions } from "../data";
import { useT } from "../i18n";
import { Icon } from "../icons";
import { DataTable, ListToolbar, useList, type Column } from "../list";
import { Skeleton, Tabs, useTab } from "../ui";

export { useQuestions } from "../data";

const KEYS = [["questions"], ["me"], ["audit"]];

export default function QuestionsPage() {
  const t = useT();
  const pending = useQuestions("pending");
  const answered = useQuestions("answered");
  const expired = useQuestions("expired");
  const [notify, setNotify] = useState(typeof Notification !== "undefined" ? Notification.permission : "denied");
  const tabs = [
    { id: "pending", label: t("Waiting"), count: pending.data?.length },
    { id: "answered", label: t("Answered"), count: answered.data?.length },
    { id: "expired", label: t("Expired"), count: expired.data?.length },
  ];
  const [tab, setTab] = useTab(tabs);
  const waiting = useList<AgentQuestion>({
    rows: pending.data ?? [],
    key: (q) => q.id,
    search: (q) => `${q.title} ${q.detail} ${q.tool} ${q.runtime} ${q.repo ?? ""} ${q.paths.join(" ")}`,
    facets: [
      { id: "runtime", label: t("Agent"), values: (q) => q.runtime },
      { id: "repo", label: t("Repository"), values: (q) => q.repo ?? t("service") },
      { id: "kind", label: t("Kind"), values: (q) => (q.kind === "input" ? t("Question") : t("Permission")) },
    ],
    sorts: [
      { id: "expires", label: t("Expires soonest"), value: (q) => timeOf(q.expires_at) },
      { id: "new", label: t("Newest"), value: (q) => timeOf(q.created_at), descFirst: true },
    ],
    defaultSort: "expires",
    param: "w",
  });
  const history = tab === "expired" ? expired.data ?? [] : answered.data ?? [];
  const past = useList<AgentQuestion>({
    rows: history,
    key: (q) => q.id,
    search: (q) => `${q.title} ${q.runtime} ${q.repo ?? ""} ${q.answer?.by ?? ""} ${q.answer?.message ?? ""}`,
    facets: [
      { id: "answer", label: t("Answer"), values: (q) => (q.answer?.allow ? t("Allowed") : t("Denied")) },
      { id: "by", label: t("Answered by"), values: (q) => (q.answer?.automatic ? t("automatic") : q.answer?.by ?? "—") },
      { id: "runtime", label: t("Agent"), values: (q) => q.runtime },
    ],
    sorts: [
      { id: "at", label: t("Answered"), value: (q) => timeOf(q.answer?.at ?? q.created_at), descFirst: true },
      { id: "title", label: t("Question"), value: (q) => q.title },
    ],
    defaultSort: "-at",
    param: "h",
  });
  const columns: Column<AgentQuestion>[] = [
    { id: "title", primary: true, header: t("Question"), sort: "title", cell: (q) => <span style={{ fontWeight: 550 }}>{q.title}</span> },
    {
      id: "answer",
      header: t("Answer"),
      cell: (q) => <Status value={q.answer?.allow ? "allowed" : "denied"} label={q.answer ? (q.answer.allow ? t("allowed") : t("denied")) : t("expired")} />,
    },
    { id: "by", header: t("By"), cell: (q) => (q.answer?.automatic ? <span className="muted">{t("automatic")}</span> : q.answer?.by ?? "—") },
    { id: "where", header: t("Where"), cell: (q) => <Where q={q} />, className: "small" },
    { id: "runtime", header: t("Agent"), cell: (q) => <span className="mono">{q.runtime}</span>, className: "hide-narrow" },
    { id: "at", header: t("When"), sort: "at", cell: (q) => <When at={q.answer?.at ?? q.created_at} /> },
    { id: "msg", header: t("Message"), cell: (q) => <span className="small muted">{q.answer?.message}</span>, className: "hide-narrow" },
  ];

  return (
    <>
      <PageHead
        title={t("Agent questions")}
        sub={t("Agents run in their CLI's auto mode. Anything they would ask a person waits here, and an unanswered question is denied when it expires.")}
        actions={
          notify === "default" ? (
            <button onClick={() => void Notification.requestPermission().then(setNotify)}>
              <Icon name="bell" />
              {t("Alert me in the browser")}
            </button>
          ) : notify === "granted" ? (
            <span className="small muted row" style={{ gap: 4 }}>
              <Icon name="bell" size={14} />
              {t("Browser alerts are on")}
            </span>
          ) : null
        }
      />
      <Tabs tabs={tabs} active={tab} onChange={setTab} />
      {tab === "pending" ? (
        <>
          <ListToolbar list={waiting} placeholder={t("Search questions")} />
          <ErrorBox error={pending.error} />
          {pending.isLoading ? (
            <div className="card">
              <Skeleton rows={5} />
            </div>
          ) : !waiting.count ? (
            <div className="card">
              <CaughtUp>{t("No agent is waiting for an answer.")}</CaughtUp>
            </div>
          ) : (
            <div className="stack">
              {waiting.visible.map((q) => (
                <QuestionCard key={q.id} question={q} />
              ))}
            </div>
          )}
        </>
      ) : (
        <>
          <ListToolbar list={past} placeholder={t("Search past questions")} />
          <ErrorBox error={answered.error || expired.error} />
          <DataTable list={past} columns={columns} empty={t("Nothing yet.")} />
        </>
      )}
    </>
  );
}

function Where({ q }: { q: AgentQuestion }) {
  const t = useT();
  if (!q.repo) return <span className="muted">{t("service")}</span>;
  if (q.pr) return <Link to={prPath(q.repo, q.pr)}>{`${q.repo}#${q.pr}`}</Link>;
  return <Link to={`/repos/${q.repo}`}>{q.repo}</Link>;
}

export function QuestionCard({ question: q, compact = false }: { question: AgentQuestion; compact?: boolean }) {
  const t = useT();
  const me = useMe().data;
  const [message, setMessage] = useState("");
  const [showDetail, setShowDetail] = useState(!compact);
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
        { id: "allow", label: t("Allow"), kind: "allow" },
        { id: "deny", label: t("Deny"), kind: "deny" },
      ];
  const expires = q.expires_at ? relative(q.expires_at) : null;
  const done = answer.isSuccess;

  return (
    <article className="card stack" style={{ gap: 10 }}>
      <div className="row">
        <Icon name={q.kind === "input" ? "question" : "shield"} />
        <strong className="q-title" title={q.title}>
          {q.title}
        </strong>
        {q.asked_for && (
          <span className={`pill ${me && q.asked_for.toLowerCase() === me.login.toLowerCase() ? "status-warn" : ""}`}>
            {me && q.asked_for.toLowerCase() === me.login.toLowerCase() ? t("asks you") : `${t("for")} ${q.asked_for}`}
          </span>
        )}
        <span className="muted small right row" style={{ gap: 6 }}>
          <span className="mono">{q.runtime}</span> · {q.task_kind} · <Where q={q} />
        </span>
      </div>
      {q.detail &&
        (showDetail ? (
          <Pre text={q.detail} />
        ) : (
          <button className="ghost sm" style={{ alignSelf: "flex-start" }} onClick={() => setShowDetail(true)}>
            <Icon name="chevronRight" size={14} />
            {t("Show what the agent wants to run")}
          </button>
        ))}
      {q.paths.length > 0 && (
        <div className="labels">
          {q.paths.map((p) => (
            <code key={p}>{p}</code>
          ))}
        </div>
      )}
      {done ? (
        <span className="small" style={{ color: "var(--ok)" }}>
          <Icon name="check" size={13} /> {t("Answered. The agent continues.")}
        </span>
      ) : q.kind === "input" ? (
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
          <input
            aria-label={t("Message to the agent (optional)")}
            placeholder={t("Message to the agent (optional)")}
            value={message}
            onChange={(e) => setMessage(e.target.value)}
          />
          <div className="row">
            {options.map((o) => (
              <button
                key={o.id}
                className={o.kind === "allow" ? "primary" : o.kind === "deny" || o.kind === "cancel" ? "danger" : ""}
                disabled={answer.isPending}
                onClick={() => pick(o)}
              >
                {o.label}
              </button>
            ))}
            {expires && (
              <span className="small muted right" title={q.expires_at ?? ""}>
                <Icon name="clock" size={12} /> {t("denied")} {expires}
              </span>
            )}
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
        <fieldset key={i.id} className="stack" style={{ border: "1px solid var(--border)", borderRadius: 10, padding: "8px 12px" }}>
          <legend className="small">
            {i.header && <strong>{i.header}: </strong>}
            {i.question}
          </legend>
          <div className="row">
            {i.options.map((label) => (
              <label key={label} className="row" style={{ gap: 6 }}>
                <input type="radio" name={i.id} checked={values[i.id] === label} onChange={() => onChange({ ...values, [i.id]: label })} />
                {label}
              </label>
            ))}
          </div>
          {i.free_text && (
            <input
              type="text"
              aria-label={i.question}
              placeholder={t("Or type an answer")}
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
        <button className="danger" disabled={busy} onClick={onDeny}>
          {t("Deny")}
        </button>
      </div>
    </div>
  );
}
