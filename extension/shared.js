// Shared by the popup and the line on YouTube.
const SITE = "http://127.0.0.1:8765";
const POLL_MS = 2000;

// Everything shown can come from a page's title or text, so it is always set as text.
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "class") node.className = value;
    else if (value !== undefined && value !== null && value !== false) node.setAttribute(key, value);
  }
  node.append(...children.filter((c) => c !== null && c !== undefined));
  return node;
}

function ask(message) {
  return chrome.runtime.sendMessage(message);
}

function roundedLength(seconds) {
  const minutes = Math.max(Math.round(seconds / 60), 1);
  if (minutes < 60) return `${minutes} min`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

function itemLink(item, label) {
  const href = `${SITE}/watch/${encodeURIComponent(item.id)}`;
  return el("a", { href, target: "_blank", rel: "noopener" }, label);
}

// Shows whether one link is in Summariser and offers to add it. The verdict and summary
// are left to Summariser's own pages. `getPage` supplies the page's HTML when adding,
// and `extras` adds controls for a saved item.
function createCard(root, { url, getPage = async () => null, extras = () => [] }) {
  let timer = null;
  let stopped = false;
  let reply = null;
  let shown = null;
  let busy = false;
  let notice = null;

  async function refresh() {
    clearTimeout(timer);
    let latest;
    try {
      latest = await ask({ type: "lookup", url });
    } catch {
      latest = { ok: false, message: "The extension was updated. Reload this page to reconnect." };
    }
    if (stopped) return;
    reply = latest;
    // Redrawing only on a change keeps the line still while a job is polled.
    const text = JSON.stringify(reply);
    if (text !== shown) {
      shown = text;
      redraw();
    }
    const job = reply.ok ? reply.data.job : null;
    if (job && job.error === null) timer = setTimeout(refresh, POLL_MS);
  }

  async function add() {
    busy = true;
    notice = null;
    redraw();
    try {
      const result = await ask({ type: "job", url, kind: "check", page: await getPage() });
      if (!result.ok && !result.offline) notice = result.message;
    } finally {
      busy = false;
      shown = null;
      await refresh();
    }
  }

  function addButton(label) {
    return el("button", { class: "vs-primary", disabled: busy, onclick: add }, label);
  }

  function body() {
    if (!reply) return [el("span", { class: "vs-muted" }, "Checking…")];
    if (!reply.ok && reply.offline) {
      return [
        el("span", {}, "Summariser isn't running. Start it with “uv run summariser”."),
        el("button", { onclick: refresh }, "Try again"),
      ];
    }
    if (!reply.ok) return [el("span", { class: "vs-error" }, reply.message)];
    const { item, job } = reply.data;
    if (job && job.error === null) {
      return [el("span", { class: "vs-muted", role: "status" }, `Adding: ${job.stage.toLowerCase()}…`)];
    }
    const problem = notice || (job && job.error);
    if (problem) return [el("span", { class: "vs-error" }, problem), addButton("Try again")];
    if (!item) return [addButton("Add to Summariser")];
    return [el("span", {}, "Added ✓"), itemLink(item, "Open in Summariser"), ...extras(item)];
  }

  function redraw() {
    root.replaceChildren(el("strong", {}, "Summariser"), ...body());
  }

  function stop() {
    stopped = true;
    clearTimeout(timer);
  }

  redraw();
  refresh();
  return { refresh, redraw, stop };
}
