// Everything shown here can come from a video's title or transcript, so it is always set as text.
export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "class") node.className = value;
    else if (value !== undefined && value !== null && value !== false) node.setAttribute(key, value);
  }
  node.append(...children.filter((c) => c !== null && c !== undefined));
  return node;
}

export function clock(seconds) {
  const whole = Math.max(Math.floor(seconds || 0), 0);
  const hours = Math.floor(whole / 3600);
  const minutes = String(Math.floor((whole % 3600) / 60)).padStart(2, "0");
  return `${hours}:${minutes}:${String(whole % 60).padStart(2, "0")}`;
}

export function roundedLength(seconds) {
  const minutes = Math.max(Math.round(seconds / 60), 1);
  if (minutes < 60) return `${minutes} min`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

// A coarse label, because the estimate isn't precise enough to show as a figure.
// Each step has a label and a level from 1 to 4 that sets how strong its badge looks.
function paddingStep(padding) {
  if (padding >= 65) return [4, "Mostly padding"];
  if (padding >= 40) return [3, "About half padding"];
  if (padding >= 20) return [2, "Some padding"];
  return [1, "Dense"];
}

export function paddingBadge(item) {
  if (item.padding === null || item.padding === undefined) return null;
  const title = `Claude's estimate: about ${item.padding}% teaches nothing`;
  const [level, label] = paddingStep(item.padding);
  return el("span", { class: `badge padding-${level}`, title }, label);
}

// The verdict, how padded it is, and the reason, for the top of an item's own page.
export function verdictNodes(item) {
  return [
    item.verdict
      ? el("span", { class: `badge ${item.verdict}` }, verdictLabel(item.kind, item.verdict))
      : null,
    paddingBadge(item),
    item.verdict ? el("span", { class: "reason" }, ` ${item.reason}`) : null,
  ].filter((node) => node !== null);
}

const JOB_NAMES = { check: "Checking", shorten: "Shortening", watched: "Adding" };
const JOB_POLL_MS = 2000;

// What a job is doing, such as "Shortening: deciding what to cut".
export function jobStage(job) {
  if (job.stage === "Waiting") return `${JOB_NAMES[job.kind]}: waiting for another job to finish`;
  return `${JOB_NAMES[job.kind]}: ${job.stage.toLowerCase()}`;
}

// The same with how far through it is. The percentage is an estimate from the stages done.
export function jobProgress(job) {
  return job.stage === "Waiting" ? jobStage(job) : `${jobStage(job)} · ${job.progress}%`;
}

export function jobBar(job) {
  const label = `${job.progress}% done`;
  return el("progress", { max: "100", value: String(job.progress), "aria-label": label, title: label });
}

// Keeps `show` up to date with the job working on a link, and reloads the page once
// `isDone` says the result is ready. `show` gets null when no job is running.
export async function followJob(url, isDone, show) {
  const { item, job } = await api(`/api/lookup?url=${encodeURIComponent(url)}`);
  if (item && isDone(item)) {
    location.reload();
    return;
  }
  show(job);
  if (job && job.error === null) setTimeout(() => followJob(url, isDone, show), JOB_POLL_MS);
}

export async function api(path, body) {
  const options = body === undefined
    ? {}
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const response = await fetch(path, options);
  if (!response.ok) {
    const problem = await response.json().catch(() => ({}));
    throw new Error(problem.detail || `The server said ${response.status}`);
  }
  return response.json();
}

export const VERDICT_LABELS = { skip: "Skip it", parts: "Watch parts", watch: "Worth watching" };
const ARTICLE_VERDICT_LABELS = { skip: "Skip it", parts: "Read parts", watch: "Worth reading" };

export function verdictLabel(kind, verdict) {
  return (kind === "article" ? ARTICLE_VERDICT_LABELS : VERDICT_LABELS)[verdict];
}
