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

const JOB_NAMES = { check: "Checking", shorten: "Shortening", watched: "Adding" };
const JOB_POLL_MS = 2000;

// One line on how a job is getting on, such as "Shortening: deciding what to cut · 3 min so far".
export function jobProgress(job) {
  if (job.stage === "Waiting") return `${JOB_NAMES[job.kind]}: waiting for another job to finish`;
  const taken = job.seconds < 60 ? `${job.seconds} s` : roundedLength(job.seconds);
  return `${JOB_NAMES[job.kind]}: ${job.stage.toLowerCase()} · ${taken} so far`;
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
