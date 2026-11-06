// Opening the live view of an agent's job from anywhere: it is a ``?job=`` parameter on
// the current page, which the job panel in the shell reads.

/** Open the live view of a job, by its id or by the request event that starts it. */
export function openJob(id: string) {
  const url = new URL(window.location.href);
  url.searchParams.set("job", id);
  window.history.pushState(null, "", url.toString());
  window.dispatchEvent(new PopStateEvent("popstate"));
}
