// The signed-in person's own test machines, beside their own agents and model APIs.
// The reviews, reproductions, and Fix CI runs they ask for use these first, on any
// repository they work on; nobody else's work does. The service reaches each one
// over SSH with a key it made for this person: they add its public half to the
// machine's authorized_keys.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, ApiError } from "./api";
import { Empty, ErrorBox, Section, useAct } from "./components";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { Skeleton, useToast } from "./ui";

interface MyMachine {
  name: string;
  id: string;
  host: string | null;
  user: string | null;
  port: number | null;
  description: string;
  workdir: string;
  setup: string;
  timeout_seconds: number;
}

interface MyMachines {
  public_key: string | null;
  key_problem: string | null;
  machines: MyMachine[];
}

interface Checked {
  ok: boolean;
  error?: string;
  system?: string;
  cpus?: string;
  memory_gb?: string;
  gpus?: { index: number; idle: boolean }[];
}

const KEYS = [["my-machines"], ["labs"], ["repo-machines"]];

export function MyMachinesSection() {
  const t = useT();
  const q = useQuery({ queryKey: ["my-machines"], queryFn: () => api.get<MyMachines>("/api/me/machines"), retry: false });
  const [adding, setAdding] = useState(false);
  // Without a repository here, or on a host that keeps nothing personal, there is nothing to show.
  if (q.error instanceof ApiError && (q.error.status === 403 || q.error.status === 409)) return null;
  return (
    <Section
      title={t("My test machines")}
      hint={t("Yours alone: the reviews, reproductions, and CI fixes you ask for use them first, on any repository you work on. Nobody else's work runs on them.")}
      aside={
        !adding && (
          <button className="primary sm" onClick={() => setAdding(true)}>
            <Icon name="plus" size={14} />
            {t("Add machine")}
          </button>
        )
      }
    >
      {q.isLoading ? (
        <Skeleton rows={2} />
      ) : !q.data ? (
        <ErrorBox error={q.error} />
      ) : (
        <div className="stack" style={{ gap: 14 }}>
          <KeyBox data={q.data} />
          {adding && <MachineForm onDone={() => setAdding(false)} />}
          {q.data.machines.length === 0 && !adding ? (
            <Empty icon="server">{t("No machines of your own yet.")}</Empty>
          ) : (
            <div className="list">
              {q.data.machines.map((m) => (
                <MachineRow key={m.name} m={m} />
              ))}
            </div>
          )}
        </div>
      )}
    </Section>
  );
}

function KeyBox({ data }: { data: MyMachines }) {
  const t = useT();
  const toast = useToast();
  if (!data.public_key) return <span className="small muted">{data.key_problem || t("No SSH key yet.")}</span>;
  return (
    <div className="stack" style={{ gap: 6 }}>
      <span className="small muted">{t("The service reaches your machines with this key of yours. Add it as one line to ~/.ssh/authorized_keys of the login you give for each machine; its private half never leaves this host.")}</span>
      <div className="row" style={{ gap: 8, alignItems: "flex-start" }}>
        <code className="mono small" style={{ wordBreak: "break-all", flex: 1, minWidth: 0 }}>
          {data.public_key}
        </code>
        <button
          className="sm"
          onClick={() => {
            void navigator.clipboard?.writeText(data.public_key ?? "");
            toast({ title: t("Copied"), icon: "check" });
          }}
        >
          {t("Copy")}
        </button>
      </div>
    </div>
  );
}

function MachineForm({ initial, onDone }: { initial?: MyMachine; onDone: () => void }) {
  const t = useT();
  const [name, setName] = useState(initial?.name ?? "");
  const [host, setHost] = useState(initial?.host ?? "");
  const [user, setUser] = useState(initial?.user ?? "");
  const [port, setPort] = useState(initial?.port ? String(initial.port) : "");
  const [workdir, setWorkdir] = useState(initial?.workdir ?? "am-labs");
  const [setup, setSetup] = useState(initial?.setup ?? "");
  const [minutes, setMinutes] = useState(String(Math.round((initial?.timeout_seconds ?? 3600) / 60)));
  const [description, setDescription] = useState(initial?.description ?? "");
  const save = useAct(
    () =>
      api.put(`/api/me/machines/${encodeURIComponent(name.trim())}`, {
        host: host.trim(),
        user: user.trim() || null,
        port: port ? Number(port) : null,
        workdir: workdir.trim() || "am-labs",
        setup,
        timeout_seconds: Math.max(1, Number(minutes) || 60) * 60,
        description,
      }),
    KEYS,
  );
  return (
    <div className="stack" style={{ gap: 8, padding: 12, border: "1px solid var(--border)", borderRadius: "var(--r)" }}>
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        <label className="stack small" style={{ gap: 2, flex: "1 1 140px" }}>
          {t("Name")}
          <input value={name} disabled={!!initial} placeholder="gpu-box" onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="stack small" style={{ gap: 2, flex: "2 1 200px" }}>
          {t("Host")}
          <input value={host} placeholder="10.0.0.5" onChange={(e) => setHost(e.target.value)} />
        </label>
        <label className="stack small" style={{ gap: 2, flex: "1 1 110px" }}>
          {t("Login")}
          <input value={user} placeholder="ci" onChange={(e) => setUser(e.target.value)} />
        </label>
        <label className="stack small" style={{ gap: 2, flex: "0 1 90px" }}>
          {t("Port")}
          <input value={port} inputMode="numeric" placeholder="22" onChange={(e) => setPort(e.target.value.replace(/\D/g, ""))} />
        </label>
      </div>
      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
        <label className="stack small" style={{ gap: 2, flex: "2 1 200px" }}>
          {t("Work folder")}
          <input value={workdir} onChange={(e) => setWorkdir(e.target.value)} />
        </label>
        <label className="stack small" style={{ gap: 2, flex: "1 1 120px" }}>
          {t("Minutes per session")}
          <input value={minutes} inputMode="numeric" onChange={(e) => setMinutes(e.target.value.replace(/\D/g, ""))} />
        </label>
      </div>
      <label className="stack small" style={{ gap: 2 }}>
        {t("Setup before each command")}
        <input value={setup} placeholder="source ~/venv/bin/activate" onChange={(e) => setSetup(e.target.value)} />
      </label>
      <label className="stack small" style={{ gap: 2 }}>
        {t("About the machine (the agents read this)")}
        <textarea rows={3} value={description} placeholder={t("Its GPUs, where models and data live, which environment to use, mirrors.")} onChange={(e) => setDescription(e.target.value)} />
      </label>
      <div className="row" style={{ gap: 8 }}>
        <button className="ghost right" onClick={onDone}>
          {t("Cancel")}
        </button>
        <button className="primary" disabled={!name.trim() || !host.trim() || save.isPending} onClick={() => save.mutate(undefined, { onSuccess: onDone })}>
          {save.isPending ? t("Saving…") : t("Save")}
        </button>
      </div>
      <ErrorBox error={save.error} />
    </div>
  );
}

function MachineRow({ m }: { m: MyMachine }) {
  const t = useT();
  const [editing, setEditing] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const remove = useAct(() => api.del(`/api/me/machines/${encodeURIComponent(m.name)}`), KEYS);
  const check = useAct(() => api.post<Checked>(`/api/me/machines/${encodeURIComponent(m.name)}/check`), []);
  if (editing) {
    return (
      <div className="list-row" style={{ display: "block", padding: "12px 0" }}>
        <MachineForm initial={m} onDone={() => setEditing(false)} />
      </div>
    );
  }
  const c = check.data;
  const where = `${m.user ? `${m.user}@` : ""}${m.host ?? ""}${m.port ? `:${m.port}` : ""}`;
  return (
    <div className="list-row" style={{ flexDirection: "column", alignItems: "stretch", gap: 6, padding: "14px 4px" }}>
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <Icon name="server" />
        <strong>{m.name}</strong>
        <span className="mono muted small">{where}</span>
        <span className="spacer" />
        <button className="sm" disabled={check.isPending} onClick={() => check.mutate(undefined)}>
          {check.isPending ? t("Checking…") : t("Check")}
        </button>
        <button className="sm" onClick={() => setEditing(true)}>
          {t("Edit")}
        </button>
        {confirm ? (
          <button className="danger sm" disabled={remove.isPending} onClick={() => remove.mutate(undefined)}>
            {t("Remove it")}
          </button>
        ) : (
          <button className="ghost sm" onClick={() => setConfirm(true)}>
            {t("Remove")}
          </button>
        )}
      </div>
      {m.description && <span className="small muted">{m.description}</span>}
      {c &&
        (c.ok ? (
          <span className="small" style={{ color: "var(--ok)" }}>
            <Icon name="check" size={13} /> {c.system} · {c.cpus} CPUs · {c.memory_gb} GB · {(c.gpus ?? []).length} GPUs ({(c.gpus ?? []).filter((g) => g.idle).length} {t("idle")})
          </span>
        ) : (
          <span className="small" style={{ color: "var(--bad)" }}>
            <Icon name="x" size={13} /> {c.error}
          </span>
        ))}
      <ErrorBox error={check.error || remove.error} />
    </div>
  );
}
