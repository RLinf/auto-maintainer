// Editing a PR's title and description in place, on its page: a pencil beside the
// title, and Edit on the Description card. Saving sets them on the forge as the
// person who saved, with their own sign-in (``POST /api/prs/{repo}/{n}/edit``, a
// ``pr.edit`` run at once as them); the forge decides whether they may, as it does
// for the PR's author and anyone with write access.

import { useState, type ReactNode } from "react";
import { api } from "./api";
import { ErrorBox, GrowingTextarea, Section, useAct } from "./components";
import { ConnectGitHub } from "./connectGitHub";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { Markdown } from "./markdown";
import { useToast } from "./ui";

type Saved = { status: string; error: string | null; reason: string | null };

function useSavePr(repo: string, number: number) {
  const t = useT();
  const toast = useToast();
  const save = useAct((draft: { title: string; body: string }) => api.post<Saved>(`/api/prs/${repo}/${number}/edit`, draft), [["pr"], ["prs"]]);
  return {
    pending: save.isPending,
    error: save.error,
    run: (draft: { title: string; body: string }, done: () => void) =>
      save.mutate(draft, {
        onSuccess: (out) => {
          if (out.status === "executed" || out.status === "queued" || out.status === "intent") {
            done();
            toast(
              out.status === "executed"
                ? { title: t("Saved"), body: t("GitHub has the new title and description."), icon: "check" }
                : { title: t("Saving…"), body: t("The new title and description go to GitHub in a moment."), icon: "clock" },
            );
          } else {
            toast({ title: t("Not saved"), body: out.error || out.reason || out.status, icon: "x" });
          }
        },
      }),
  };
}

function SaveRow({ busy, disabled, canPush, onCancel, onSave }: { busy: boolean; disabled: boolean; canPush: boolean; onCancel: () => void; onSave: () => void }) {
  const t = useT();
  return (
    <div className="row" style={{ gap: 8, justifyContent: "flex-end", flexWrap: "wrap" }}>
      <span className="small muted" style={{ marginRight: "auto" }}>
        {t("Saves on GitHub as you.")}
      </span>
      <button type="button" className="ghost" onClick={onCancel}>
        {t("Cancel")}
      </button>
      <button type="button" className="primary" disabled={busy || disabled || !canPush} onClick={onSave}>
        {busy ? t("Saving…") : t("Save")}
      </button>
    </div>
  );
}

/** The PR's title with a pencil that turns it into a field, for an open PR. */
export function EditableTitle({
  repo,
  number,
  title,
  body,
  editable,
  canPush,
  after,
}: {
  repo: string;
  number: number;
  title: string;
  body: string;
  editable: boolean;
  canPush: boolean;
  /** What follows the title, such as its number and link. */
  after?: ReactNode;
}) {
  const t = useT();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(title);
  const save = useSavePr(repo, number);
  if (!editing) {
    return (
      <>
        {title} {after}
        {editable && (
          <button
            type="button"
            className="ghost icon title-edit"
            aria-label={t("Edit title")}
            title={t("Edit title")}
            onClick={() => {
              setDraft(title);
              setEditing(true);
            }}
          >
            <Icon name="pencil" size={15} />
          </button>
        )}
      </>
    );
  }
  const submit = () => save.run({ title: draft.trim(), body }, () => setEditing(false));
  return (
    <span className="stack title-editor" style={{ gap: 8 }}>
      <input
        autoFocus
        value={draft}
        aria-label={t("Title")}
        maxLength={256}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.nativeEvent.isComposing && draft.trim() && draft.trim() !== title) submit();
          if (e.key === "Escape") setEditing(false);
        }}
      />
      <SaveRow busy={save.pending} disabled={!draft.trim() || draft.trim() === title} canPush={canPush} onCancel={() => setEditing(false)} onSave={submit} />
      {!canPush && <ConnectGitHub why={t("Sign in with GitHub, so the edit comes from you.")} />}
      <ErrorBox error={save.error} />
    </span>
  );
}

/** The Description card: the PR's description, with Edit turning it into a text
 * box with a preview, for an open PR. */
export function DescriptionSection({
  repo,
  number,
  title,
  body,
  editable,
  canPush,
}: {
  repo: string;
  number: number;
  title: string;
  body: string;
  editable: boolean;
  canPush: boolean;
}) {
  const t = useT();
  const [editing, setEditing] = useState(false);
  const [preview, setPreview] = useState(false);
  const [draft, setDraft] = useState(body);
  const save = useSavePr(repo, number);
  const aside =
    editable && !editing ? (
      <button
        type="button"
        className="ghost sm"
        onClick={() => {
          setDraft(body);
          setPreview(false);
          setEditing(true);
        }}
      >
        <Icon name="pencil" size={14} /> {t("Edit")}
      </button>
    ) : editing ? (
      <div className="segmented" role="tablist" aria-label={t("Description")}>
        <button type="button" role="tab" aria-selected={!preview} className={preview ? "" : "on"} onClick={() => setPreview(false)}>
          {t("Write")}
        </button>
        <button type="button" role="tab" aria-selected={preview} className={preview ? "on" : ""} onClick={() => setPreview(true)}>
          {t("Preview")}
        </button>
      </div>
    ) : null;
  return (
    <Section title={t("Description")} aside={aside}>
      {!editing ? (
        body.trim() ? (
          <Markdown text={body} className="pr-body" />
        ) : (
          <span className="muted small">{t("No description.")}</span>
        )
      ) : (
        <div className="stack" style={{ gap: 10 }}>
          {preview ? (
            draft.trim() ? (
              <Markdown text={draft} className="pr-body" />
            ) : (
              <span className="muted small">{t("No description.")}</span>
            )
          ) : (
            <GrowingTextarea autoFocus minRows={10} value={draft} aria-label={t("Description")} onChange={(e) => setDraft(e.target.value)} />
          )}
          <SaveRow
            busy={save.pending}
            disabled={draft === body}
            canPush={canPush}
            onCancel={() => setEditing(false)}
            onSave={() => save.run({ title, body: draft }, () => setEditing(false))}
          />
          {!canPush && <ConnectGitHub why={t("Sign in with GitHub, so the edit comes from you.")} />}
          <ErrorBox error={save.error} />
        </div>
      )}
    </Section>
  );
}
