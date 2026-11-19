// When a repository's reviews start on their own (``review.trigger``, ``review.scan``):
// only when asked, periodically, or on every update, and whether new commits get a
// follow-up. They run on the service's agents and test machines, so only the service's
// owner changes it (``web/api.py:set_review_automation``); everyone else reads it here.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, type Automation } from "./api";
import { ErrorBox, Section, useAct } from "./components";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { useToast } from "./ui";

const EVERY = [1, 2, 3, 4, 6, 8, 12, 24, 168];

export function ReviewAutomationSection({ repo }: { repo: string }) {
  const t = useT();
  const q = useQuery({ queryKey: ["automation", repo], queryFn: () => api.get<Automation>(`/api/repos/${repo}/review-automation`) });
  const a = q.data;
  return (
    <Section title={t("When reviews run")} hint={a && !a.can_configure ? a.reason : t("only the service's owner changes it")}>
      {!a ? (
        <ErrorBox error={q.error} />
      ) : a.can_configure ? (
        <AutomationForm a={a} />
      ) : (
        <span className="row small" style={{ gap: 6 }}>
          <Icon name="clock" size={13} />
          {a.trigger === "scan"
            ? `${t("Periodically")}: ${t("every")} ${a.every_hours === 24 ? t("day") : a.every_hours === 168 ? t("week") : `${a.every_hours ?? 6} ${t("hours")}`}, ${t("up to")} ${a.max_prs}`
            : a.trigger === "events"
              ? t("On every update")
              : t("When asked")}
          {a.trigger !== "manual" && a.followups ? ` · ${t("re-review on new commits")}` : ""}
        </span>
      )}
    </Section>
  );
}

function AutomationForm({ a }: { a: Automation }) {
  const t = useT();
  const toast = useToast();
  const client = useQueryClient();
  const [trigger, setTrigger] = useState(a.trigger);
  const [every, setEvery] = useState(a.every_hours ?? 6);
  const [maxPrs, setMaxPrs] = useState(a.max_prs);
  const [followups, setFollowups] = useState(a.followups);
  useEffect(() => {
    setTrigger(a.trigger);
    setEvery(a.every_hours ?? 6);
    setMaxPrs(a.max_prs);
    setFollowups(a.followups);
  }, [a]);
  const save = useAct(
    () => api.put<{ applied: boolean; reason: string }>(`/api/repos/${a.repo}/review-automation`, { trigger, every_hours: every, max_prs: maxPrs, followups }),
    [["automation"], ["memory"], ["profile"]],
  );
  const changed = trigger !== a.trigger || (trigger === "scan" && (every !== (a.every_hours ?? 6) || maxPrs !== a.max_prs)) || followups !== a.followups;
  const modes: { id: Automation["trigger"]; label: string }[] = [
    { id: "manual", label: t("When asked") },
    { id: "scan", label: t("Periodically") },
    { id: "events", label: t("On every update") },
  ];
  return (
    <div className="stack" style={{ gap: 8 }}>
      <div className="row">
        <Icon name="repo" size={14} />
        <strong className="small">{a.repo}</strong>
      </div>
      <div className="segmented" role="radiogroup" aria-label={t("When reviews run")}>
        {modes.map((m) => (
          <button key={m.id} role="radio" aria-checked={trigger === m.id} className={trigger === m.id ? "on" : ""} onClick={() => setTrigger(m.id)}>
            {m.label}
          </button>
        ))}
      </div>
      {trigger === "scan" && (
        <div className="row small">
          {t("Every")}
          <select value={every} onChange={(e) => setEvery(Number(e.target.value))} style={{ height: 30 }}>
            {EVERY.map((h) => (
              <option key={h} value={h}>
                {h === 168 ? t("week") : h === 24 ? t("day") : `${h} ${t("hours")}`}
              </option>
            ))}
          </select>
          {t("up to")}
          <input type="number" min={1} max={50} value={maxPrs} onChange={(e) => setMaxPrs(Number(e.target.value) || 1)} style={{ width: 64, height: 30 }} />
          {t("PRs, highest priority first")}
        </div>
      )}
      {trigger !== "manual" && (
        <label className="row small" style={{ gap: 6 }}>
          <input type="checkbox" checked={followups} onChange={(e) => setFollowups(e.target.checked)} />
          {t("Re-review PRs that get new commits")}
        </label>
      )}
      {changed && (
        <div className="row">
          <button
            className="primary sm"
            disabled={save.isPending}
            onClick={() =>
              save.mutate(undefined, {
                onSuccess: (r) => {
                  void client.invalidateQueries({ queryKey: ["automation"] });
                  toast({
                    title: r.applied ? t("Saved") : t("Change proposed"),
                    body: r.applied ? undefined : `${t("A maintainer of the repository applies it.")} ${r.reason}`,
                    icon: r.applied ? "check" : "inbox",
                  });
                },
              })
            }
          >
            {t("Save")}
          </button>
        </div>
      )}
      <ErrorBox error={save.error} />
    </div>
  );
}
