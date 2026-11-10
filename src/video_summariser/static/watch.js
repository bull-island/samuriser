import { VERDICT_LABELS, api, clock, el, roundedLength } from "/static/ui.js";

const videoId = decodeURIComponent(location.pathname.split("/").pop());
const video = document.getElementById("video");
const fullBar = document.getElementById("full-bar");
const shortBar = document.getElementById("short-bar");
const tip = document.getElementById("tip");

const SPEEDS = [1, 1.25, 1.5, 1.75, 2];
const FULL = "full";
const TIER_NAMES = { 1: "Core", 2: "Depth", 3: "Extra" };
const LENGTH_NAMES = { 1: "Essentials", 2: "Standard", 3: "Everything kept", [FULL]: "Full video" };
const COLOURS = {
  surface: "#1a1a19",
  playhead: "#f0efec",
  accent: "#3987e5",
  accentCut: "#234a78",
  kept: ["#96958c", "#6b6a64"],
  cut: ["#45443f", "#36352f"],
};
const BAR_HEIGHT = 10;
const CHAPTER_GAP = 2;
const JUMP_CHECK_MS = 30;
const BAR_REFRESH_MS = 100;
const SAVE_POSITION_MS = 5000;
const EDGE_TOLERANCE = 0.01;

let data = null;
let mode = "3";
let ranges = [];
let shortLength = 0;
let finished = false;

function readStored(key) {
  try {
    return localStorage.getItem(`${videoId}:${key}`);
  } catch {
    return null;
  }
}

function store(key, value) {
  try {
    localStorage.setItem(`${videoId}:${key}`, value);
  } catch {
    // Remembering position and settings is only a convenience.
  }
}

function toShort(time) {
  let position = 0;
  for (const [start, end] of ranges) {
    if (time < end) return position + Math.max(time - start, 0);
    position += end - start;
  }
  return position;
}

function fromShort(position) {
  let elapsed = 0;
  for (const [start, end] of ranges) {
    if (position < elapsed + (end - start)) return start + (position - elapsed);
    elapsed += end - start;
  }
  return ranges.length ? ranges[ranges.length - 1][1] : 0;
}

function isInRanges(time) {
  return ranges.some(([start, end]) => time >= start && time < end);
}

function chapterIndexAt(time) {
  let current = 0;
  data.chapters.forEach((chapter, index) => {
    if (chapter.start <= time) current = index;
  });
  return current;
}

function prepareCanvas(canvas) {
  const box = canvas.getBoundingClientRect();
  const scale = window.devicePixelRatio || 1;
  const width = Math.round(box.width * scale);
  const height = Math.round(box.height * scale);
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  const context = canvas.getContext("2d");
  context.setTransform(scale, 0, 0, scale, 0, 0);
  context.clearRect(0, 0, box.width, box.height);
  return { context, width: box.width, height: box.height };
}

function fill({ context, width, height }, start, end, colour) {
  const left = start * width;
  const size = end * width - left;
  if (size <= 0) return;
  context.fillStyle = colour;
  context.fillRect(left, (height - BAR_HEIGHT) / 2, size, BAR_HEIGHT);
}

function drawPlayhead({ context, width, height }, fraction) {
  const x = fraction * width;
  context.fillStyle = COLOURS.surface;
  context.fillRect(x - 3, 0, 6, height);
  context.fillStyle = COLOURS.playhead;
  context.fillRect(x - 1, 0, 2, height);
}

function drawBars() {
  const time = video.currentTime;
  const current = chapterIndexAt(time);
  const full = prepareCanvas(fullBar);
  const short = prepareCanvas(shortBar);
  const fullGap = CHAPTER_GAP / full.width;
  const shortGap = CHAPTER_GAP / short.width;

  data.chapters.forEach((chapter, index) => {
    const tone = index % 2;
    const kept = index === current ? COLOURS.accent : COLOURS.kept[tone];
    const cut = index === current ? COLOURS.accentCut : COLOURS.cut[tone];
    const chapterEnd = chapter.end / data.duration - fullGap;

    fill(full, chapter.start / data.duration, chapterEnd, cut);
    for (const [start, end] of ranges) {
      const from = Math.max(start, chapter.start);
      const to = Math.min(end, chapter.end);
      if (from < to) fill(full, from / data.duration, Math.min(to / data.duration, chapterEnd), kept);
    }
    const shortStart = toShort(chapter.start) / shortLength;
    const shortEnd = toShort(chapter.end) / shortLength;
    fill(short, shortStart, shortEnd - shortGap, kept);
  });

  drawPlayhead(full, time / data.duration);
  drawPlayhead(short, toShort(time) / shortLength);
  document.getElementById("full-time").textContent = `${clock(time)} / ${clock(data.duration)}`;
  document.getElementById("short-time").textContent = `${clock(toShort(time))} / ${clock(shortLength)}`;
  document.getElementById("chapter").textContent = data.chapters[current].title;
  highlightSection(time);
}

function highlightSection(time) {
  for (const row of document.querySelectorAll(".section")) {
    const inside = time >= Number(row.dataset.start) && time < Number(row.dataset.end);
    row.classList.toggle("current", inside);
  }
}

function seek(time) {
  if (!isInRanges(time) && mode !== FULL) {
    const next = ranges.find(([start]) => start > time);
    video.currentTime = next ? next[0] : time;
    return;
  }
  video.currentTime = time;
}

function jumpOverCuts() {
  if (!ranges.length || video.paused || video.seeking) return;
  const time = video.currentTime;
  const next = ranges.find(([, end]) => time < end - EDGE_TOLERANCE);
  if (!next) {
    video.pause();
    finishVideo();
  } else if (time < next[0] - EDGE_TOLERANCE) {
    video.currentTime = next[0];
  }
}

async function setWatched(watched) {
  const path = `/api/videos/${encodeURIComponent(videoId)}`;
  await api(`${path}/watched`, { watched });
  data.watched = watched;
  renderWatchedButton();
  ({ verdict: data.verdict, reason: data.reason } = await api(path));
  renderNotes();
}

function finishVideo() {
  if (finished) return;
  finished = true;
  setWatched(true);
}

function renderWatchedButton() {
  document.getElementById("watched").textContent = data.watched ? "Watched ✓" : "Mark as watched";
}

function barTime(canvas, event) {
  const box = canvas.getBoundingClientRect();
  const fraction = Math.min(Math.max((event.clientX - box.left) / box.width, 0), 1);
  return canvas === fullBar ? fraction * data.duration : fromShort(fraction * shortLength);
}

function attachBar(canvas) {
  canvas.addEventListener("click", (event) => seek(barTime(canvas, event)));
  canvas.addEventListener("mousemove", (event) => {
    const time = barTime(canvas, event);
    tip.hidden = false;
    tip.textContent = `${clock(time)} · ${data.chapters[chapterIndexAt(time)].title}`;
    tip.style.left = `${event.clientX + 12}px`;
    tip.style.top = `${event.clientY - 34}px`;
  });
  canvas.addEventListener("mouseleave", () => {
    tip.hidden = true;
  });
}

function rangesFor(choice) {
  return choice === FULL ? [[0, data.duration]] : data.tiers[choice];
}

function lengthOf(list) {
  return list.reduce((total, [start, end]) => total + (end - start), 0);
}

function chooseLength(choice) {
  mode = choice;
  ranges = rangesFor(choice);
  shortLength = lengthOf(ranges) || 1;
  store("length", choice);
  renderLengths();
}

function segmentedButton(label, active, onclick) {
  return el("button", { class: active ? "on" : "", onclick }, label);
}

function renderLengths() {
  // Hide settings that would play exactly the same thing as a shorter one.
  const seen = new Set();
  const options = ["1", "2", "3", FULL].filter((choice) => {
    const length = Math.round(lengthOf(rangesFor(choice)));
    if (length === 0 || seen.has(length)) return false;
    seen.add(length);
    return true;
  });
  document.getElementById("lengths").replaceChildren(
    ...options.map((choice) =>
      segmentedButton(
        `${LENGTH_NAMES[choice]} · ${roundedLength(lengthOf(rangesFor(choice)))}`,
        choice === mode,
        () => chooseLength(choice),
      ),
    ),
  );
}

function setSpeed(speed) {
  video.playbackRate = speed;
  store("speed", speed);
  document.getElementById("speeds").replaceChildren(
    ...SPEEDS.map((option) =>
      segmentedButton(`${option}×`, option === speed, () => setSpeed(option)),
    ),
  );
}

async function changeSection(section, keep) {
  data = await api(`/api/videos/${encodeURIComponent(videoId)}/sections/${section.index}`, { keep });
  chooseLength(mode);
  renderSections();
}

function sectionRow(section) {
  const label = section.keep ? TIER_NAMES[section.tier] : "Cut";
  return el(
    "div",
    {
      class: section.keep ? "section" : "section cut",
      "data-start": section.start,
      "data-end": section.end,
    },
    el(
      "button",
      {
        class: "section-jump",
        title: section.opening,
        onclick: () => {
          if (!section.keep && mode !== FULL) chooseLength(FULL);
          seek(section.start);
        },
      },
      el("span", { class: "muted" }, clock(section.start)),
      el("span", {}, section.note),
    ),
    el("span", { class: "tag" }, label),
    el(
      "button",
      { class: "small", onclick: () => changeSection(section, !section.keep) },
      section.keep ? "Cut" : "Restore",
    ),
  );
}

function renderSections() {
  document.getElementById("sections").replaceChildren(...data.sections.map(sectionRow));
}

function ideaItem(idea, number) {
  const overlap = data.overlaps.find((o) => o.idea === number);
  const source = overlap ? data.overlap_titles[overlap.video_id] : null;
  const isNew = data.new_ideas.includes(number);
  return el(
    "li",
    { class: "idea" },
    el(
      "button",
      {
        class: "idea-jump",
        onclick: () => {
          if (!data.playable) return;
          if (!isInRanges(idea.start) && mode !== FULL) chooseLength(FULL);
          seek(idea.start);
        },
      },
      el("span", { class: "muted" }, clock(idea.start)),
      el("span", {}, idea.text),
    ),
    isNew && data.overlaps.length ? el("span", { class: "tag new" }, "New to you") : null,
    source ? el("span", { class: "tag" }, `Already in: ${source}`) : null,
  );
}

function renderNotes() {
  document.title = data.title;
  document.getElementById("title").textContent = data.title;
  document.getElementById("summary").textContent = data.summary || "No summary yet.";
  document.getElementById("ideas").replaceChildren(...data.ideas.map(ideaItem));
  document.getElementById("verdict").replaceChildren(
    ...(data.verdict
      ? [
          el("span", { class: `badge ${data.verdict}` }, VERDICT_LABELS[data.verdict]),
          el("span", { class: "reason" }, ` ${data.reason}`),
        ]
      : []),
  );
}

function startPosition() {
  const requested = new URLSearchParams(location.search).get("t");
  if (requested !== null) return Number(requested);
  return Number(readStored("position")) || ranges[0][0];
}

function showPlayer() {
  document.getElementById("player").hidden = false;
  document.getElementById("sections-column").hidden = false;
  video.src = `/media/${encodeURIComponent(videoId)}`;
  const savedLength = readStored("length");
  chooseLength(savedLength && rangesFor(savedLength)?.length ? savedLength : "3");
  setSpeed(Number(readStored("speed")) || 1);
  renderSections();
  renderWatchedButton();
  attachBar(fullBar);
  attachBar(shortBar);

  video.addEventListener(
    "loadedmetadata",
    () => {
      const start = startPosition();
      if (!isInRanges(start) && new URLSearchParams(location.search).has("t")) chooseLength(FULL);
      seek(start);
      drawBars();
    },
    { once: true },
  );
  document.getElementById("watched").addEventListener("click", () => setWatched(!data.watched));
  setInterval(jumpOverCuts, JUMP_CHECK_MS);
  setInterval(drawBars, BAR_REFRESH_MS);
  setInterval(() => {
    if (!video.paused) store("position", video.currentTime);
  }, SAVE_POSITION_MS);
}

function showNotReady() {
  document.getElementById("not-ready").hidden = false;
  document.getElementById("not-ready-message").textContent = data.url
    ? "This video hasn't been shortened yet."
    : "This video is still being processed.";
  const button = document.getElementById("shorten");
  button.hidden = !data.url;
  button.addEventListener("click", async () => {
    await api("/api/jobs", { urls: [data.url], kind: "shorten" });
    document.getElementById("not-ready-message").textContent =
      "Added to the queue. You can follow its progress in the library.";
    button.hidden = true;
  });
}

async function load() {
  data = await api(`/api/videos/${encodeURIComponent(videoId)}`);
  renderNotes();
  if (data.playable) showPlayer();
  else showNotReady();
}

load().catch((error) => {
  document.getElementById("title").textContent = error.message;
});
