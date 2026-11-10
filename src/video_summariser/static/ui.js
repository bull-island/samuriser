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
