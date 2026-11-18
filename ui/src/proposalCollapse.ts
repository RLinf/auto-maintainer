// Pure collapse state rules for the Approvals list and its proposal cards. Kept free of
// JSX and package imports so unit tests run it directly in Node.

export interface Identifiable {
  id: string;
}

/** Toggle one proposal's collapsed state by id. */
export function toggleProposalCollapsed(collapsed: ReadonlySet<string>, id: string): Set<string> {
  const next = new Set(collapsed);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

/** Collapse every proposal in ``items``, keeping the state of proposals outside ``items``. */
export function collapseProposals(collapsed: ReadonlySet<string>, items: readonly Identifiable[]): Set<string> {
  const next = new Set(collapsed);
  for (const item of items) next.add(item.id);
  return next;
}

/** Expand every proposal in ``items``, keeping the state of proposals outside ``items``. */
export function expandProposals(collapsed: ReadonlySet<string>, items: readonly Identifiable[]): Set<string> {
  const next = new Set(collapsed);
  for (const item of items) next.delete(item.id);
  return next;
}

/** Whether every proposal in ``items`` is already collapsed or already expanded. */
export function proposalCollapseState(
  collapsed: ReadonlySet<string>,
  items: readonly Identifiable[],
): { allCollapsed: boolean; allExpanded: boolean } {
  return {
    allCollapsed: items.every((item) => collapsed.has(item.id)),
    allExpanded: items.every((item) => !collapsed.has(item.id)),
  };
}
