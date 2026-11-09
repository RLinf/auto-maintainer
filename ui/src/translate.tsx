// Reading a review in the dashboard's language. A review is written, and posted, in
// its repository's public language; a reader who reads another one asks for a
// translation into the language the dashboard shows. It is for reading here only:
// the draft and what is posted stay as they are.

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "./api";
import { ErrorBox } from "./components";
import { useLang, useT } from "./i18n";
import { Icon } from "./icons";
import { Markdown } from "./markdown";

interface Translation {
  body: string;
  comments: string[];
  to: string;
  cached: boolean;
}

const LANGUAGE_NAME: Record<string, string> = { en: "English", zh: "中文" };

/** A Translate button for a review (a draft or a posted one, by its action id) and,
 *  once asked, its translation below it: the main comment, then each inline comment
 *  under the place it is posted on (``places``). */
export function TranslateReview({ actionId, places = [] }: { actionId: string; places?: string[] }) {
  const t = useT();
  const { lang } = useLang();
  const [shown, setShown] = useState(false);
  const run = useMutation({
    mutationFn: () => api.post<Translation>(`/api/actions/${actionId}/translate`, { to: lang }),
  });
  const data = run.data && run.data.to === lang ? run.data : null;
  return (
    <>
      <button
        type="button"
        className="ghost sm"
        style={{ alignSelf: "flex-start" }}
        disabled={run.isPending}
        title={t("Translated for reading here; the review is posted as written.")}
        onClick={() => {
          if (data) setShown(!shown);
          else {
            setShown(true);
            run.mutate();
          }
        }}
      >
        <Icon name="globe" size={13} />
        {run.isPending ? t("Translating…") : data && shown ? t("Hide the translation") : `${t("Translate to")} ${LANGUAGE_NAME[lang] ?? lang}`}
      </button>
      {shown && data && (
        <div className="translation stack" style={{ gap: 10 }}>
          <span className="small muted">{t("Translated for reading here; the review is posted as written.")}</span>
          <Markdown text={data.body} className="posted" />
          {data.comments.map((c, i) => (
            <div key={i} className="stack" style={{ gap: 4 }}>
              <span className="small muted mono">{places[i] ?? `#${i + 1}`}</span>
              <Markdown text={c} className="posted" />
            </div>
          ))}
        </div>
      )}
      <ErrorBox error={run.error} />
    </>
  );
}
