import { PUBLISHED, api, clock, el, jobBar, jobStage, paddingBadge, roundedLength, verdictLabel } from "/static/ui.js";

const ACTIVE_REFRESH_MS = 2000;
const IDLE_REFRESH_MS = 10000;
const SEARCH_DELAY_MS = 250;

let view = "all";
let platform = null;
let library = [];
let shownLibrary = "";

function jobRow(job) {
  const failed = job.error !== null;
  return el(
    "div",
    { class: failed ? "job failed" : "job" },
    el("span", { class: "job-url" }, job.title || job.url),
    el("span", { class: "muted job-stage" }, failed ? job.error : jobStage(job)),
    failed ? null : jobBar(job),
    failed ? null : el("span", { class: "muted job-percent" }, `${job.progress}%`),
  );
}

function lengths(video) {
  if (!video.duration) return null;
  const format = video.kind === "article" ? (s) => `${roundedLength(s)} read` : clock;
  const full = format(video.duration);
  return video.short_duration ? `${full} → ${format(video.short_duration)}` : full;
}

function watchedLabel(video) {
  const word = video.kind === "article" ? "Read" : "Watched";
  if (!video.watched_at) return word;
  return `${word} ${new Date(video.watched_at).toLocaleDateString()}`;
}

function card(video) {
  const id = encodeURIComponent(video.id);
  const href = `/watch/${id}`;
  return el(
    "article",
    { class: "video" },
    // The title is the same link, so keyboards and screen readers only need that one.
    el(
      "a",
      { class: "thumb", href, tabindex: "-1", "aria-hidden": "true" },
      video.thumbnail ? el("img", { src: `/thumbnail/${id}`, alt: "" }) : null,
      el("span", { class: "kind" }, video.kind === "article" ? "Article" : "Video"),
    ),
    el(
      "div",
      {},
      el(
        "div",
        { class: "video-head" },
        el("a", { class: "video-title", href }, video.title),
        el(
          "button",
          {
            class: "remove local-only",
            title: "Remove",
            "aria-label": `Remove ${video.title}`,
            onclick: () => remove(video),
          },
          "×",
        ),
      ),
      el(
        "div",
        { class: "video-meta" },
        video.watched ? el("span", { class: "badge seen" }, watchedLabel(video)) : null,
        video.verdict
          ? el("span", { class: `badge ${video.verdict}` }, verdictLabel(video.kind, video.verdict))
          : null,
        paddingBadge(video),
        // The label on the picture is hidden from screen readers along with its link.
        el("span", { class: "visually-hidden" }, video.kind === "article" ? "Article" : "Video"),
        video.platform ? el("span", { class: "muted" }, video.platform) : null,
        el("span", { class: "muted" }, lengths(video) || ""),
      ),
      video.reason ? el("p", { class: "reason" }, video.reason) : null,
      video.summary ? el("p", { class: "muted clamp" }, video.summary) : null,
    ),
  );
}

async function remove(video) {
  const what = video.kind === "article" ? "article" : "video";
  const consequence = video.watched ? " It will also leave your history." : "";
  const question = `Remove “${video.title}”? Its summary and any downloaded ${what} are deleted.${consequence}`;
  if (!confirm(question)) return;
  try {
    await api(`/api/videos/${encodeURIComponent(video.id)}/remove`, {});
  } catch (error) {
    document.getElementById("add-message").textContent = error.message;
  }
  refresh();
}

function showList(container, nodes) {
  container.replaceChildren(...nodes);
}

function toggle(label, active, onclick) {
  return el("button", { class: active ? "on" : "", onclick }, label);
}

function historyVideos() {
  return library
    .filter((video) => video.watched)
    .sort((a, b) => (b.watched_at || "").localeCompare(a.watched_at || ""));
}

function setView(nextView, nextPlatform = null) {
  view = nextView;
  platform = nextPlatform;
  renderLibrary();
}

function renderLibrary() {
  const history = historyVideos();
  document.getElementById("views").replaceChildren(
    toggle(`All ${library.length}`, view === "all", () => setView("all")),
    toggle(`History ${history.length}`, view === "history", () => setView("history")),
  );

  const platforms = [...new Set(history.map((video) => video.platform).filter(Boolean))].sort();
  document.getElementById("platforms").replaceChildren(
    ...(view === "history" && platforms.length > 1
      ? [
          toggle("All platforms", platform === null, () => setView("history")),
          ...platforms.map((name) =>
            toggle(name, platform === name, () => setView("history", name)),
          ),
        ]
      : []),
  );

  const shown = view === "all"
    ? library
    : history.filter((video) => platform === null || video.platform === platform);
  const empty = view === "all"
    ? "Paste a link above to get started."
    : "Anything you mark as watched or read will appear here.";
  showList(
    document.getElementById("videos"),
    shown.length ? shown.map(card) : [el("p", { class: "muted" }, empty)],
  );
}

async function refresh() {
  let delay = IDLE_REFRESH_MS;
  try {
    const { jobs, videos } = await api("/api/videos");
    showList(document.getElementById("jobs"), jobs.map(jobRow));
    const latest = JSON.stringify(videos);
    // Rebuilding the cards on every refresh would make the pictures flicker.
    if (latest !== shownLibrary) {
      shownLibrary = latest;
      library = videos;
      renderLibrary();
    }
    if (jobs.some((job) => job.error === null)) delay = ACTIVE_REFRESH_MS;
  } catch (error) {
    document.getElementById("add-message").textContent = error.message;
  }
  if (!PUBLISHED) setTimeout(refresh, delay);
}

async function addLinks(kind) {
  const box = document.getElementById("links");
  const message = document.getElementById("add-message");
  try {
    const { added } = await api("/api/jobs", { urls: box.value.split("\n"), kind });
    box.value = "";
    message.textContent = added ? `Added ${added} to the queue.` : "Already in the queue.";
    refresh();
  } catch (error) {
    message.textContent = error.message;
  }
}

function searchResult(hit) {
  const href = `/watch/${encodeURIComponent(hit.video_id)}?t=${Math.floor(hit.start)}`;
  return el(
    "a",
    { class: "result", href },
    el("span", { class: "muted" }, `${hit.title} · ${clock(hit.start)}`),
    el("span", {}, hit.text),
  );
}

let searchTimer;
async function runSearch(query) {
  const results = document.getElementById("results");
  if (!query.trim()) {
    results.replaceChildren();
    return;
  }
  const hits = await api(`/api/search?q=${encodeURIComponent(query)}`);
  const nodes = hits.map(searchResult);
  showList(results, nodes.length ? nodes : [el("p", { class: "muted" }, "Nothing found.")]);
}

for (const button of document.querySelectorAll("button[data-kind]")) {
  button.addEventListener("click", () => addLinks(button.dataset.kind));
}
document.getElementById("search").addEventListener("input", (event) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => runSearch(event.target.value), SEARCH_DELAY_MS);
});
refresh();
