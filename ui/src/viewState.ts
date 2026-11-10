// Remembered list views for Home (/) and Pull requests (/prs), plus the breadcrumb and
// navigation hrefs that return to them. Kept free of JSX and package imports so unit
// tests run this module directly in Node.

export type ViewId = "home" | "prs";

export const HOME_VIEW_KEY = "am.view.home";
export const PRS_VIEW_KEY = "am.view.prs";
export const PRS_PAGE_REPO_KEY = "am.view.prs.repo";
export const REPO_KEY = "am.repo";
export const HOME_ORDER_KEY = "am.home.order";

export const HOME_ORDERS = ["priority", "newest", "oldest", "updated"] as const;
export type HomeOrder = (typeof HOME_ORDERS)[number];

const HOME_ORDER_SET: ReadonlySet<string> = new Set(HOME_ORDERS);
const HOME_PARAM_KEYS: ReadonlySet<string> = new Set(["rq", "only", "staffed", "rorder", "rall", "show", "order"]);
const PRS_PARAM_KEYS: ReadonlySet<string> = new Set(["tab", "q", "sort", "group", "page"]);

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem?(key: string): void;
}

function defaultStorage(): StorageLike | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

function getStored(key: string, storage: StorageLike | null = defaultStorage()): string | null {
  if (!storage) return null;
  try {
    return storage.getItem(key);
  } catch {
    return null;
  }
}

function setStored(key: string, value: string, storage: StorageLike | null = defaultStorage()): void {
  if (!storage) return;
  try {
    storage.setItem(key, value);
  } catch {
    // storage may be unavailable or full; the URL still holds the view
  }
}

function removeStored(key: string, storage: StorageLike | null = defaultStorage()): void {
  if (!storage) return;
  try {
    if (storage.removeItem) storage.removeItem(key);
    else storage.setItem(key, "");
  } catch {
    // ignore storage errors
  }
}

function storageKey(view: ViewId): string {
  return view === "home" ? HOME_VIEW_KEY : PRS_VIEW_KEY;
}

function toParams(search: string | URLSearchParams): URLSearchParams {
  if (typeof search !== "string") return new URLSearchParams(search);
  return new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
}

function formatSearch(params: URLSearchParams): string {
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

/** Whether ``key`` belongs to ``view``'s remembered filter, sort, tab, group, or page state. */
export function isViewParam(view: ViewId, key: string): boolean {
  if (view === "home") return HOME_PARAM_KEYS.has(key);
  return PRS_PARAM_KEYS.has(key) || (key.startsWith("f.") && key.length > 2);
}

/** Whether ``search`` carries any explicit view parameter for ``view`` (excluding ``repo``, ``job``, etc.). */
export function hasViewParams(view: ViewId, search: string | URLSearchParams): boolean {
  for (const [k, v] of toParams(search).entries()) {
    if (isViewParam(view, k) && v !== "") return true;
  }
  return false;
}

/** Copy only ``view``'s non-empty parameters from ``search``, leaving ``repo`` and transient keys out. */
export function extractViewParams(view: ViewId, search: string | URLSearchParams): URLSearchParams {
  const out = new URLSearchParams();
  for (const [k, v] of toParams(search).entries()) {
    if (isViewParam(view, k) && v !== "") out.set(k, v);
  }
  return out;
}

export function isHomeOrder(value: string | null | undefined): value is HomeOrder {
  return !!value && HOME_ORDER_SET.has(value);
}

/** Read Home's remembered PR sort order (``am.home.order``), falling back to ``"priority"``. */
export function readHomeOrder(storage?: StorageLike | null): HomeOrder {
  const saved = getStored(HOME_ORDER_KEY, storage);
  return isHomeOrder(saved) ? saved : "priority";
}

/** Persist Home's PR sort order in ``am.home.order``. */
export function writeHomeOrder(order: string, storage?: StorageLike | null): void {
  if (isHomeOrder(order)) setStored(HOME_ORDER_KEY, order, storage);
}

/** Resolve Home's active PR sort order: ``rorder`` in the URL, else ``am.home.order``, else ``"priority"``. */
export function resolveHomeOrder(search: string | URLSearchParams, storage?: StorageLike | null): HomeOrder {
  const fromUrl = toParams(search).get("rorder");
  return isHomeOrder(fromUrl) ? fromUrl : readHomeOrder(storage);
}

/** Read the selected repository remembered by ``useRepoScope`` (``am.repo``). */
export function readRememberedRepo(storage?: StorageLike | null): string {
  return getStored(REPO_KEY, storage) ?? "";
}

/** Save ``am.repo`` and drop any remembered ``/prs`` page offset recorded for a different repository. */
export function rememberRepo(repo: string, storage?: StorageLike | null): void {
  const prev = readRememberedRepo(storage);
  setStored(REPO_KEY, repo, storage);
  const raw = getStored(PRS_VIEW_KEY, storage) ?? "";
  if (!raw) return;
  const saved = extractViewParams("prs", raw);
  if (!saved.has("page")) return;
  const pageRepo = getStored(PRS_PAGE_REPO_KEY, storage) || prev;
  if (repo && pageRepo !== repo) {
    saved.delete("page");
    setStored(PRS_VIEW_KEY, saved.toString(), storage);
    removeStored(PRS_PAGE_REPO_KEY, storage);
  }
}

export interface ViewOptions {
  repo?: string;
  storage?: StorageLike | null;
}

/** Persist the active view parameters for ``view`` (without freezing ``repo`` inside ``am.view.*``). */
export function saveView(view: ViewId, search: string | URLSearchParams, options?: ViewOptions): string {
  const storage = options?.storage;
  const incoming = toParams(search);
  const extracted = extractViewParams(view, incoming);
  const serialized = extracted.toString();
  setStored(storageKey(view), serialized, storage);
  if (view === "home") {
    const rorder = extracted.get("rorder");
    if (isHomeOrder(rorder)) writeHomeOrder(rorder, storage);
  } else if (extracted.has("page")) {
    const activeRepo = options?.repo || incoming.get("repo") || readRememberedRepo(storage);
    if (activeRepo) setStored(PRS_PAGE_REPO_KEY, activeRepo, storage);
  } else {
    removeStored(PRS_PAGE_REPO_KEY, storage);
  }
  return serialized;
}

/** Read the stored view parameters for ``view``, dropping ``page`` when the target repository changed. */
export function readSavedView(view: ViewId, options?: ViewOptions): URLSearchParams {
  const storage = options?.storage;
  const saved = extractViewParams(view, getStored(storageKey(view), storage) ?? "");
  if (view === "prs" && saved.has("page")) {
    const targetRepo = options?.repo || readRememberedRepo(storage);
    const pageRepo = getStored(PRS_PAGE_REPO_KEY, storage) || "";
    if (targetRepo && pageRepo !== targetRepo) saved.delete("page");
  }
  return saved;
}

/** Pure preview of ``enterView`` without writing to ``storage`` (safe during React render). */
export function previewViewEntry(
  view: ViewId,
  locationSearch: string | URLSearchParams,
  options?: ViewOptions,
): URLSearchParams {
  const storage = options?.storage;
  const incoming = toParams(locationSearch);
  if (hasViewParams(view, incoming)) return incoming;
  const effectiveRepo = options?.repo || incoming.get("repo") || readRememberedRepo(storage);
  const saved = readSavedView(view, { repo: effectiveRepo, storage });
  const merged = new URLSearchParams(incoming);
  for (const [k, v] of saved.entries()) merged.set(k, v);
  return merged;
}

/** Resolve the URL query parameters on entry to ``/`` or ``/prs``.
 *
 *  Explicit view parameters in ``locationSearch`` win and overwrite the stored view.
 *  Bare ``/`` or ``/prs`` (or ``?repo=...`` alone) restores the stored view merged with any
 *  non-view parameters already in ``locationSearch``.
 */
export function enterView(
  view: ViewId,
  locationSearch: string | URLSearchParams,
  options?: ViewOptions,
): { search: string; params: URLSearchParams; restored: boolean } {
  const storage = options?.storage;
  const incoming = toParams(locationSearch);
  const effectiveRepo = options?.repo || incoming.get("repo") || readRememberedRepo(storage);
  if (hasViewParams(view, incoming)) {
    saveView(view, incoming, { repo: effectiveRepo, storage });
    const params = new URLSearchParams(incoming);
    return { search: formatSearch(params), params, restored: false };
  }
  const rawSaved = extractViewParams(view, getStored(storageKey(view), storage) ?? "");
  const saved = readSavedView(view, { repo: effectiveRepo, storage });
  if (view === "prs" && rawSaved.has("page") && !saved.has("page")) {
    saveView("prs", saved, { repo: effectiveRepo, storage });
  }
  const merged = new URLSearchParams(incoming);
  for (const [k, v] of saved.entries()) merged.set(k, v);
  return {
    search: formatSearch(merged),
    params: merged,
    restored: merged.toString() !== incoming.toString(),
  };
}

function buildHref(basePath: "/" | "/prs", view: ViewId, repo?: string, storage?: StorageLike | null): string {
  const params = new URLSearchParams();
  if (repo) params.set("repo", repo);
  for (const [k, v] of readSavedView(view, { repo, storage }).entries()) {
    params.set(k, v);
  }
  const qs = params.toString();
  return qs ? `${basePath}?${qs}` : basePath;
}

/** Href to Home preserving remembered Home filters and sorting (and ``repo`` when given). */
export function homeHref(repo?: string, storage?: StorageLike | null): string {
  return buildHref("/", "home", repo, storage);
}

/** Href to Pull requests preserving remembered filters, sorting, and paging (and ``repo`` when given). */
export function prsHref(repo?: string, storage?: StorageLike | null): string {
  return buildHref("/prs", "prs", repo, storage);
}

/** Href for top-level navigation targets: uses remembered views for ``/`` and ``/prs``. */
export function navHref(
  to: string,
  options?: { pathname?: string; search?: string; storage?: StorageLike | null },
): string {
  if (to === "/") {
    if (options?.pathname === "/" && options.search !== undefined) {
      const current = toParams(options.search);
      const out = new URLSearchParams();
      const repo = current.get("repo");
      if (repo) out.set("repo", repo);
      for (const [k, v] of extractViewParams("home", current).entries()) out.set(k, v);
      const qs = out.toString();
      return qs ? `/?${qs}` : "/";
    }
    return homeHref(undefined, options?.storage);
  }
  if (to === "/prs") {
    if (options?.pathname === "/prs" && options.search !== undefined) {
      const current = toParams(options.search);
      const out = new URLSearchParams();
      const repo = current.get("repo");
      if (repo) out.set("repo", repo);
      for (const [k, v] of extractViewParams("prs", current).entries()) out.set(k, v);
      const qs = out.toString();
      return qs ? `/prs?${qs}` : "/prs";
    }
    return prsHref(undefined, options?.storage);
  }
  return to;
}

/** Breadcrumb targets on ``PrPage`` preserving remembered Home and ``/prs`` views. */
export function prBreadcrumbs(
  repo: string,
  from: unknown,
  labels: { home: string; prs: string },
  storage?: StorageLike | null,
): { to: string; label: string }[] {
  if (from === "home") {
    return [
      { to: homeHref(repo, storage), label: labels.home },
      { to: prsHref(repo, storage), label: repo },
    ];
  }
  return [
    { to: prsHref(undefined, storage), label: labels.prs },
    { to: prsHref(repo, storage), label: repo },
  ];
}
