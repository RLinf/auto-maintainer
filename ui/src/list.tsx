// Lists that work the same everywhere: search, faceted filters, sorting, grouping, paging,
// selection, and keyboard navigation, with the view kept in the URL so it can be shared
// and survives the back button.

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { useT } from "./i18n";
import { Icon } from "./icons";
import { Popover } from "./ui";

export interface Facet<T> {
  id: string;
  label: string;
  values: (row: T) => string | string[] | null | undefined;
}

export interface SortDef<T> {
  id: string;
  label: string;
  value: (row: T) => string | number | null | undefined;
  /** Sort descending first (numbers, dates). */
  descFirst?: boolean;
}

export interface GroupDef<T> {
  id: string;
  label: string;
  value: (row: T) => string;
  order?: string[];
}

export interface ListConfig<T> {
  rows: T[];
  key: (row: T) => string;
  search?: (row: T) => string;
  facets?: Facet<T>[];
  sorts?: SortDef<T>[];
  defaultSort?: string;
  /** ``-id`` sorts descending. */
  groups?: GroupDef<T>[];
  pageSize?: number;
  /** Prefix for URL parameters when a page has several lists. */
  param?: string;
}

function asList(v: string | string[] | null | undefined): string[] {
  if (v == null || v === "") return [];
  return Array.isArray(v) ? v : [v];
}

function compare(a: string | number | null | undefined, b: string | number | null | undefined): number {
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
}

export function useList<T>(config: ListConfig<T>) {
  const { rows, key, search, facets = [], sorts = [], groups = [], pageSize = 50, param = "" } = config;
  const p = (name: string) => (param ? `${param}.${name}` : name);
  const [params, setParams] = useSearchParams();
  const query = params.get(p("q")) ?? "";
  const sortParam = params.get(p("sort")) ?? config.defaultSort ?? "";
  const groupId = params.get(p("group")) ?? "";
  const page = Math.max(0, Number(params.get(p("page")) ?? "0") || 0);
  const selectedFacets: Record<string, string[]> = {};
  for (const f of facets) {
    const v = params.get(p(`f.${f.id}`));
    if (v) selectedFacets[f.id] = v.split(",").filter(Boolean);
  }
  const [selection, setSelection] = useState<Set<string>>(new Set());

  const update = (changes: Record<string, string | null>, keepPage = false) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        for (const [k, v] of Object.entries(changes)) {
          if (v === null || v === "") next.delete(p(k));
          else next.set(p(k), v);
        }
        if (!keepPage) next.delete(p("page"));
        return next;
      },
      { replace: true },
    );

  const sortId = sortParam.replace(/^-/, "");
  const sortDesc = sortParam.startsWith("-");
  const sortDef = sorts.find((s) => s.id === sortId);
  const groupDef = groups.find((g) => g.id === groupId);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return rows.filter((row) => {
      if (needle && search && !search(row).toLowerCase().includes(needle)) return false;
      for (const f of facets) {
        const want = selectedFacets[f.id];
        if (!want?.length) continue;
        const have = asList(f.values(row));
        if (!want.some((w) => have.includes(w))) return false;
      }
      return true;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, query, JSON.stringify(selectedFacets)]);

  const sorted = useMemo(() => {
    if (!sortDef) return filtered;
    const out = [...filtered].sort((a, b) => compare(sortDef.value(a), sortDef.value(b)));
    return sortDesc ? out.reverse() : out;
  }, [filtered, sortDef, sortDesc]);

  const grouped = useMemo(() => {
    if (!groupDef) return null;
    const map = new Map<string, T[]>();
    for (const row of sorted) {
      const g = groupDef.value(row) || "—";
      map.set(g, [...(map.get(g) ?? []), row]);
    }
    const order = groupDef.order ?? [];
    return [...map.entries()].sort(([a], [b]) => {
      const ia = order.indexOf(a);
      const ib = order.indexOf(b);
      if (ia !== -1 || ib !== -1) return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
      return compare(a, b);
    });
  }, [sorted, groupDef]);

  const pages = Math.max(1, Math.ceil(sorted.length / pageSize));
  const safePage = Math.min(page, pages - 1);
  const visible = grouped ? sorted : sorted.slice(safePage * pageSize, safePage * pageSize + pageSize);

  const facetCounts = (f: Facet<T>) => {
    const counts = new Map<string, number>();
    for (const row of rows) for (const v of asList(f.values(row))) counts.set(v, (counts.get(v) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => compare(a[0], b[0]));
  };

  return {
    rows,
    all: sorted,
    visible,
    grouped,
    total: rows.length,
    count: sorted.length,
    page: safePage,
    pages,
    pageSize,
    setPage: (n: number) => update({ page: n ? String(n) : null }, true),
    query,
    setQuery: (q: string) => update({ q }),
    facets,
    selectedFacets,
    facetCounts,
    toggleFacet: (id: string, value: string) => {
      const cur = new Set(selectedFacets[id] ?? []);
      if (cur.has(value)) cur.delete(value);
      else cur.add(value);
      update({ [`f.${id}`]: [...cur].join(",") || null });
    },
    clearFacet: (id: string) => update({ [`f.${id}`]: null }),
    clearAll: () => update({ q: null, ...Object.fromEntries(facets.map((f) => [`f.${f.id}`, null])) }),
    filtering: !!query || Object.values(selectedFacets).some((v) => v.length),
    sorts,
    sortId,
    sortDesc,
    setSort: (id: string) => {
      const def = sorts.find((s) => s.id === id);
      const desc = id === sortId ? !sortDesc : !!def?.descFirst;
      update({ sort: `${desc ? "-" : ""}${id}` }, true);
    },
    groups,
    groupId: groupDef ? groupId : "",
    setGroup: (id: string) => update({ group: id || null }),
    key,
    selection,
    isSelected: (row: T) => selection.has(key(row)),
    toggle: (row: T) =>
      setSelection((prev) => {
        const next = new Set(prev);
        const k = key(row);
        if (next.has(k)) next.delete(k);
        else next.add(k);
        return next;
      }),
    selectAll: (rs: T[]) => setSelection(new Set(rs.map(key))),
    clearSelection: () => setSelection(new Set()),
    selected: rows.filter((r) => selection.has(key(r))),
  };
}

export type ListState<T> = ReturnType<typeof useList<T>>;

// ---- toolbar ---------------------------------------------------------------------------------

export function ListToolbar<T>({
  list,
  placeholder = "Search",
  children,
}: {
  list: ListState<T>;
  placeholder?: string;
  children?: ReactNode;
}) {
  const t = useT();
  const ref = useRef<HTMLInputElement>(null);
  const [draft, setDraft] = useState(list.query);
  useEffect(() => setDraft(list.query), [list.query]);
  useEffect(() => {
    const timer = window.setTimeout(() => draft !== list.query && list.setQuery(draft), 180);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      if (e.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName)) {
        e.preventDefault();
        ref.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const sortLabel = list.sorts.find((s) => s.id === list.sortId)?.label;
  return (
    <div className="toolbar">
      <label className="search">
        <Icon name="search" />
        <input
          ref={ref}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => e.key === "Escape" && (setDraft(""), list.setQuery(""), ref.current?.blur())}
          placeholder={placeholder}
          aria-label={placeholder}
          data-list-search
        />
        <span className="kbd">/</span>
      </label>
      {list.facets.map((f) => {
        const chosen = list.selectedFacets[f.id] ?? [];
        return (
          <Popover
            key={f.id}
            label={f.label}
            trigger={(_, toggle) => (
              <button className={`chip-btn ${chosen.length ? "on" : ""}`} onClick={toggle} aria-haspopup="menu">
                {chosen.length ? <Icon name="filter" size={14} /> : <Icon name="plus" size={14} />}
                {f.label}
                {chosen.length === 1 ? <>: {chosen[0]}</> : chosen.length > 1 ? <span className="n">{chosen.length}</span> : null}
              </button>
            )}
          >
            {() => (
              <>
                <div className="menu-label">{f.label}</div>
                {list.facetCounts(f).map(([value, n]) => (
                  <button key={value} className="menu-item" role="menuitemcheckbox" aria-checked={chosen.includes(value)} onClick={() => list.toggleFacet(f.id, value)}>
                    <span className="menu-check">{chosen.includes(value) ? <Icon name="check" size={14} /> : null}</span>
                    <span className="truncate">{value}</span>
                    <span className="right">{n}</span>
                  </button>
                ))}
                {!list.facetCounts(f).length && <div className="menu-item muted">No values</div>}
                {chosen.length > 0 && (
                  <>
                    <div className="menu-sep" />
                    <button className="menu-item" onClick={() => list.clearFacet(f.id)}>
                      Clear {f.label.toLowerCase()}
                    </button>
                  </>
                )}
              </>
            )}
          </Popover>
        );
      })}
      {list.filtering && (
        <button className="ghost sm" onClick={list.clearAll}>
          {t("Reset")}
        </button>
      )}
      <span className="spacer" />
      {list.groups.length > 0 && (
        <Popover
          align="right"
          label={t("Group by")}
          trigger={(_, toggle) => (
            <button className="ghost" onClick={toggle}>
              <Icon name="list" /> {list.groupId ? `${t("Group")}: ${list.groups.find((g) => g.id === list.groupId)?.label}` : t("Group")}
            </button>
          )}
        >
          {(close) => (
            <>
              <div className="menu-label">{t("Group by")}</div>
              {[{ id: "", label: t("No grouping") }, ...list.groups].map((g) => (
                <button key={g.id} className="menu-item" onClick={() => (list.setGroup(g.id), close())}>
                  <span className="menu-check">{list.groupId === g.id ? <Icon name="check" size={14} /> : null}</span>
                  {g.label}
                </button>
              ))}
            </>
          )}
        </Popover>
      )}
      {list.sorts.length > 0 && (
        <Popover
          align="right"
          label={t("Sort by")}
          trigger={(_, toggle) => (
            <button className="ghost" onClick={toggle} aria-label={t("Sort")}>
              <Icon name={list.sortDesc ? "sortDesc" : "sortAsc"} /> {sortLabel ?? t("Sort")}
            </button>
          )}
        >
          {() => (
            <>
              <div className="menu-label">{t("Sort by")}</div>
              {list.sorts.map((s) => (
                <button key={s.id} className="menu-item" onClick={() => list.setSort(s.id)}>
                  <span className="menu-check">{list.sortId === s.id ? <Icon name={list.sortDesc ? "sortDesc" : "sortAsc"} size={14} /> : null}</span>
                  {s.label}
                </button>
              ))}
            </>
          )}
        </Popover>
      )}
      {children}
    </div>
  );
}

// ---- table ---------------------------------------------------------------------------------

export interface Column<T> {
  id: string;
  header: ReactNode;
  cell: (row: T) => ReactNode;
  /** The sort this column's header toggles. */
  sort?: string;
  className?: string;
  width?: number | string;
}

export function DataTable<T>({
  list,
  columns,
  onOpen,
  selectable,
  empty,
  rowClass,
}: {
  list: ListState<T>;
  columns: Column<T>[];
  onOpen?: (row: T) => void;
  selectable?: (row: T) => boolean;
  empty?: ReactNode;
  rowClass?: (row: T) => string;
}) {
  const [active, setActive] = useState(-1);
  const flat = list.visible;
  useEffect(() => setActive(-1), [list.query, list.page, list.sortId, list.sortDesc]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      if (["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName) || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "j" || e.key === "ArrowDown") {
        if (e.key === "ArrowDown" && active === -1) return;
        e.preventDefault();
        setActive((i) => Math.min(flat.length - 1, i + 1));
      } else if (e.key === "k" || (e.key === "ArrowUp" && active !== -1)) {
        e.preventDefault();
        setActive((i) => Math.max(0, i - 1));
      } else if (e.key === "Enter" && active >= 0 && onOpen) {
        onOpen(flat[active]);
      } else if (e.key === "x" && active >= 0 && selectable?.(flat[active])) {
        list.toggle(flat[active]);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active, flat, onOpen, selectable, list]);

  const selectableRows = selectable ? flat.filter(selectable) : [];
  const allSelected = selectableRows.length > 0 && selectableRows.every(list.isSelected);
  const header = (
    <tr>
      {selectable && (
        <th className="check">
          <input
            type="checkbox"
            aria-label="Select all"
            checked={allSelected}
            onChange={() => (allSelected ? list.clearSelection() : list.selectAll(selectableRows))}
          />
        </th>
      )}
      {columns.map((c) => {
        const sorted = c.sort && list.sortId === c.sort;
        return (
          <th
            key={c.id}
            className={c.className}
            style={c.width ? { width: c.width } : undefined}
            aria-sort={sorted ? (list.sortDesc ? "descending" : "ascending") : undefined}
          >
            {c.sort ? (
              <button className="sorter" onClick={() => list.setSort(c.sort!)}>
                {c.header}
                <Icon name={sorted ? (list.sortDesc ? "sortDesc" : "sortAsc") : "sort"} size={12} />
              </button>
            ) : (
              c.header
            )}
          </th>
        );
      })}
    </tr>
  );
  const renderRow = (row: T, index: number) => (
    <tr
      key={list.key(row)}
      className={`${onOpen ? "clickable" : ""} ${index === active ? "active" : ""} ${rowClass?.(row) ?? ""}`}
      onClick={(e) => {
        const el = e.target as HTMLElement;
        if (onOpen && !el.closest("a,button,input,select,label")) onOpen(row);
      }}
    >
      {selectable && (
        <td className="check">
          {selectable(row) && (
            <input type="checkbox" aria-label={`Select ${list.key(row)}`} checked={list.isSelected(row)} onChange={() => list.toggle(row)} />
          )}
        </td>
      )}
      {columns.map((c) => (
        <td key={c.id} className={c.className}>
          {c.cell(row)}
        </td>
      ))}
    </tr>
  );
  let index = -1;
  return (
    <div className="table-card">
      <div className="table-wrap">
        <table>
          <thead>{header}</thead>
          <tbody>
            {list.grouped
              ? list.grouped.map(([group, rows]) => [
                  <tr key={`g:${group}`} className="group-row">
                    <td colSpan={columns.length + (selectable ? 1 : 0)}>
                      {group} <span className="muted">· {rows.length}</span>
                    </td>
                  </tr>,
                  ...rows.map((row) => renderRow(row, ++index)),
                ])
              : flat.map((row, i) => renderRow(row, i))}
          </tbody>
        </table>
        {!list.count && <div className="empty">{list.filtering ? "Nothing matches these filters." : empty ?? "Nothing here yet."}</div>}
      </div>
      <TableFoot list={list} />
    </div>
  );
}

export function TableFoot<T>({ list }: { list: ListState<T> }) {
  const t = useT();
  if (!list.total) return null;
  const from = list.grouped ? 1 : list.page * list.pageSize + 1;
  const to = list.grouped ? list.count : Math.min(list.count, (list.page + 1) * list.pageSize);
  return (
    <div className="table-foot">
      <span>
        {list.count ? `${from}–${to} ${t("of")} ${list.count}` : "0"}
        {list.count !== list.total ? ` (filtered from ${list.total})` : ""}
      </span>
      <span className="spacer" />
      <span className="hide-narrow">
        <span className="kbd">j</span> <span className="kbd">k</span> {t("move")} · <span className="kbd">↵</span> {t("open")}
      </span>
      {!list.grouped && list.pages > 1 && (
        <span className="row">
          <button className="sm" disabled={list.page === 0} onClick={() => list.setPage(list.page - 1)}>
            Previous
          </button>
          <span>
            {list.page + 1} / {list.pages}
          </span>
          <button className="sm" disabled={list.page >= list.pages - 1} onClick={() => list.setPage(list.page + 1)}>
            Next
          </button>
        </span>
      )}
    </div>
  );
}

/** The bar that floats at the bottom while rows are selected. */
export function BulkBar({ count, onClear, children }: { count: number; onClear: () => void; children: ReactNode }) {
  if (!count) return null;
  return (
    <div className="bulkbar" role="toolbar" aria-label="Selected rows">
      <span>{count} selected</span>
      {children}
      <button className="ghost icon sm" aria-label="Clear selection" onClick={onClear}>
        <Icon name="x" size={14} />
      </button>
    </div>
  );
}
