// Viewport placement for popovers and menus: keeps an open panel inside the visible
// browser area horizontally (and clear of the open desktop agent panel when there is
// room) and flips above the trigger when space below is tight.

export const POP_MARGIN = 12;
export const POP_GAP = 6;
export const POP_MAX_HEIGHT = 600;
export const POP_FLIP_BELOW = 260;

export interface TriggerBox {
  top: number;
  bottom: number;
  left: number;
  right: number;
}

export interface PopoverInput {
  trigger: TriggerBox;
  panelWidth: number;
  panelHeight: number;
  naturalHeight?: number;
  align: "left" | "right";
  viewportWidth: number;
  viewportHeight: number;
  agentLeft?: number | null;
}

export interface PopoverPlacement {
  left: number;
  top: number;
  maxWidth: number;
  maxHeight: number;
  above: boolean;
}

export function placePopover({
  trigger,
  panelWidth,
  panelHeight,
  naturalHeight,
  align,
  viewportWidth,
  viewportHeight,
  agentLeft,
}: PopoverInput): PopoverPlacement {
  const edgeOffset = POP_GAP + POP_MARGIN;
  const spaceBelow = viewportHeight - trigger.bottom - edgeOffset;
  const spaceAbove = trigger.top - edgeOffset;
  const contentHeight = naturalHeight ?? panelHeight;
  const above = (spaceBelow < POP_FLIP_BELOW || contentHeight > spaceBelow) && spaceAbove > spaceBelow;
  const availableSpace = above ? spaceAbove : spaceBelow;
  const maxHeight = Math.max(
    0,
    Math.min(POP_MAX_HEIGHT, viewportHeight - 2 * POP_MARGIN, availableSpace),
  );
  const effectiveHeight = Math.min(panelHeight, maxHeight);
  const rawTop = above ? trigger.top - POP_GAP - effectiveHeight : trigger.bottom + POP_GAP;
  const top = Math.max(
    POP_MARGIN,
    Math.min(rawTop, viewportHeight - POP_MARGIN - effectiveHeight),
  );

  const maxWidth = Math.max(0, viewportWidth - 2 * POP_MARGIN);
  const effectiveWidth = Math.min(panelWidth, maxWidth);
  const preferredLeft = align === "right" ? trigger.right - effectiveWidth : trigger.left;
  let visibleRight = viewportWidth - POP_MARGIN;
  if (
    agentLeft !== null &&
    agentLeft !== undefined &&
    viewportWidth >= 560 &&
    trigger.left < agentLeft
  ) {
    visibleRight = Math.min(visibleRight, agentLeft - POP_MARGIN);
  }
  const clampedRight = Math.min(preferredLeft, visibleRight - effectiveWidth);
  const left = Math.max(
    POP_MARGIN,
    Math.min(clampedRight, viewportWidth - POP_MARGIN - effectiveWidth),
  );

  return { left, top, maxWidth, maxHeight, above };
}

export function readAgentLeft(viewportWidth: number, innerWidth: number = window.innerWidth): number | null {
  if (!document.body.classList.contains("agent-open") || innerWidth < 560) {
    return null;
  }
  const agentEl = document.querySelector<HTMLElement>(".agent-panel.open");
  if (agentEl && agentEl.offsetWidth > 0) {
    const left = viewportWidth - agentEl.offsetWidth - 12;
    return left > POP_MARGIN ? left : null;
  }
  if (innerWidth > 560) {
    return viewportWidth - Math.min(440, viewportWidth - 24) - 12;
  }
  return null;
}

export function applyPopoverLayout(
  anchor: HTMLElement,
  panel: HTMLElement,
  align: "left" | "right",
  measureWidth = true,
): PopoverPlacement {
  const vw = document.documentElement.clientWidth || window.innerWidth;
  const vh = document.documentElement.clientHeight || window.innerHeight;
  const triggerRect = anchor.getBoundingClientRect();
  const agentLeft = readAgentLeft(vw, window.innerWidth);
  const maxWidth = Math.max(0, vw - 2 * POP_MARGIN);
  panel.style.maxWidth = `${maxWidth}px`;
  panel.style.overflowY = "auto";
  if (measureWidth) panel.style.left = "0px";
  const naturalHeight = panel.scrollHeight + (panel.offsetHeight - panel.clientHeight);
  const pre = placePopover({
    trigger: triggerRect,
    panelWidth: 0,
    panelHeight: naturalHeight,
    naturalHeight,
    align,
    viewportWidth: vw,
    viewportHeight: vh,
    agentLeft,
  });
  panel.style.maxHeight = `${pre.maxHeight}px`;
  const panelRect = panel.getBoundingClientRect();
  const pos = placePopover({
    trigger: triggerRect,
    panelWidth: panelRect.width,
    panelHeight: panelRect.height,
    naturalHeight,
    align,
    viewportWidth: vw,
    viewportHeight: vh,
    agentLeft,
  });
  panel.style.left = `${pos.left}px`;
  panel.style.top = `${pos.top}px`;
  panel.classList.toggle("pop-above", pos.above);
  return pos;
}
