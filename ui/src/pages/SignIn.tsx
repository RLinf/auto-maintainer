// Sign-in: GitHub OAuth web flow, or the device flow when the browser cannot reach the callback.

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ErrorBox } from "../components";
import { useT } from "../i18n";
import { Logo } from "../logo";

interface DeviceStart {
  flow_id: string;
  user_code: string;
  verification_uri: string;
  interval: number;
}

export default function SignIn() {
  const t = useT();
  const client = useQueryClient();
  const [device, setDevice] = useState<DeviceStart | null>(null);
  const [error, setError] = useState<unknown>(null);
  const timer = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (timer.current) window.clearTimeout(timer.current);
    },
    [],
  );

  const start = async () => {
    setError(null);
    try {
      const res = await fetch("/auth/device/start", { method: "POST", credentials: "same-origin" });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? res.statusText);
      const data = (await res.json()) as DeviceStart;
      setDevice(data);
      poll(data, data.interval);
    } catch (e) {
      setError(e);
    }
  };

  const poll = (flow: DeviceStart, interval: number) => {
    timer.current = window.setTimeout(async () => {
      try {
        const res = await fetch("/auth/device/poll", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ flow_id: flow.flow_id }),
        });
        if (res.status === 200) {
          await client.invalidateQueries();
          return;
        }
        if (res.status === 202) {
          const body = await res.json().catch(() => ({}));
          poll(flow, body.error === "slow_down" ? interval + 5 : interval);
          return;
        }
        throw new Error((await res.json().catch(() => ({}))).detail ?? res.statusText);
      } catch (e) {
        setError(e);
        setDevice(null);
      }
    }, Math.max(interval, 3) * 1000);
  };

  return (
    <div className="signin card" style={{ padding: 28 }}>
      <div className="row" style={{ gap: 10 }}>
        <Logo size={40} />
        <h1>auto-maintainer</h1>
      </div>
      <p className="muted" style={{ margin: 0 }}>
        {t("Sign in with a GitHub account that has write access to a repository this service maintains.")}
      </p>
      <a className="button primary" href="/auth/login" style={{ height: 40, textDecoration: "none" }}>
        {t("Sign in with GitHub")}
      </a>
      {device ? (
        <div className="stack" style={{ alignItems: "center", textAlign: "center" }}>
          <span>
            {t("Open")}{" "}
            <a href={device.verification_uri} target="_blank" rel="noreferrer">
              {device.verification_uri}
            </a>{" "}
            {t("and enter")}:
          </span>
          <span className="code">{device.user_code}</span>
          <span className="muted small">{t("Waiting for GitHub…")}</span>
        </div>
      ) : (
        <button className="ghost" onClick={start}>
          {t("Sign in with a device code")}
        </button>
      )}
      <ErrorBox error={error} />
    </div>
  );
}
