// How a panel beside the page shows (the live view of a job, the agent): as wide as
// the person dragged its left edge, or flat over the whole page for reading. Each
// panel keeps its last choice in this browser under its own key, and that is how it
// opens next time.

import { useState } from "react";
import { useT } from "./i18n";
import { Icon } from "./icons";

export interface PanelLayout {
  mode: "panel" | "flat";
  width: number;
}

function read(key: string, width: number, min: number): PanelLayout {
  try {
    const kept = JSON.parse(localStorage.getItem(key) ?? "null") as Partial<PanelLayout> | null;
    return { mode: kept?.mode === "flat" ? "flat" : "panel", width: Math.max(min, Number(kept?.width) || width) };
  } catch {
    return { mode: "panel", width };
  }
}

function keep(key: string, layout: PanelLayout) {
  try {
    localStorage.setItem(key, JSON.stringify(layout));
  } catch {
    // A private window: the layout lasts for this page only.
  }
}

/** A panel's layout, kept under ``key``: ``width`` wide at first and never narrower
 * than ``min``. ``startResize`` goes on the panel's left edge. */
export function usePanelLayout(key: string, width: number, min: number) {
  const [layout, setLayout] = useState<PanelLayout>(() => read(key, width, min));
  const change = (next: PanelLayout) => {
    setLayout(next);
    keep(key, next);
  };
  // Dragging the left edge sets the width, up to all but a strip of the page.
  const startResize = (e: React.PointerEvent) => {
    e.preventDefault();
    const handle = e.currentTarget as HTMLElement;
    handle.setPointerCapture(e.pointerId);
    let w = layout.width;
    const move = (ev: PointerEvent) => {
      w = Math.round(Math.min(window.innerWidth - 48, Math.max(min, window.innerWidth - 12 - ev.clientX)));
      setLayout((l) => ({ ...l, width: w }));
    };
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      keep(key, { mode: "panel", width: w });
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
  };
  const flat = layout.mode === "flat";
  return {
    layout,
    flat,
    /** The panel's inline style: its width, as ``--panel-w`` so the stylesheet still
     * decides where it applies (not when flat, nor on a phone). */
    style: { "--panel-w": `${layout.width}px` } as React.CSSProperties,
    toggleFlat: () => change({ ...layout, mode: flat ? "panel" : "flat" }),
    startResize,
    reset: () => change({ ...layout, width }),
  };
}

/** The left edge of a panel beside the page: dragged to resize it, double-clicked to
 * give it back its first width. */
export function ResizeEdge({ onStart, onReset }: { onStart: (e: React.PointerEvent) => void; onReset: () => void }) {
  const t = useT();
  return <div className="panel-resize" role="separator" aria-orientation="vertical" aria-label={t("Drag to resize")} title={t("Drag to resize")} onPointerDown={onStart} onDoubleClick={onReset} />;
}

/** The button that lays a panel flat over the page, or back beside it. */
export function FlatButton({ flat, onClick, className = "ghost icon" }: { flat: boolean; onClick: () => void; className?: string }) {
  const t = useT();
  const label = flat ? t("Show beside the page") : t("Show over the whole page");
  return (
    <button className={className} onClick={onClick} aria-pressed={flat} aria-label={label} title={label}>
      <Icon name={flat ? "shrink" : "expand"} size={15} />
    </button>
  );
}
