import { PUBLISHED, api, clock, el, followJob, jobProgress, roundedLength, verdictNodes } from "/static/ui.js";

const videoId = decodeURIComponent(location.pathname.split("/").pop());
let video = document.getElementById("video");
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

const YOUTUBE_PLAYING = 1;

function loadYoutubeApi() {
  return new Promise((resolve, reject) => {
    window.onYouTubeIframeAPIReady = resolve;
    const script = el("script", { src: "https://www.youtube.com/iframe_api" });
    script.onerror = () => reject(new Error("YouTube's player couldn't be loaded."));
    document.head.append(script);
  });
}

// Plays a video that was never downloaded in YouTube's embedded player, behind the few
// parts of a <video> element this page uses, so the rest of the page treats both alike.
async function youtubePlayer(start) {
  const page = new URL(data.url);
  if (!page.hostname.endsWith("youtube.com")) {
    throw new Error("This video was downloaded, so it only plays on the Mac that has it.");
  }
  await loadYoutubeApi();
  document.getElementById("video").hidden = true;
  document.getElementById("youtube").hidden = false;
  const player = await new Promise((resolve, reject) => {
    const created = new YT.Player("youtube", {
      videoId: page.searchParams.get("v"),
      playerVars: { playsinline: 1, rel: 0, start: Math.floor(start), origin: location.origin },
      events: {
        onReady: () => resolve(created),
        onError: (event) =>
          reject(
            new Error(
              `YouTube won't play this video here (error ${event.data}). On YouTube, the Samuriser extension jumps over the cuts.`,
            ),
          ),
      },
    });
  });
  return {
    seeking: false,
    get currentTime() {
      return player.getCurrentTime() || 0;
    },
    set currentTime(time) {
      player.seekTo(time, true);
    },
    get paused() {
      return player.getPlayerState() !== YOUTUBE_PLAYING;
    },
    set playbackRate(speed) {
      player.setPlaybackRate(speed);
    },
    pause: () => player.pauseVideo(),
  };
}

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
  if (finished || PUBLISHED) return;
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
  if (data.playable) chooseLength(mode);
  renderSections();
}

function jumpTo(section) {
  if (document.getElementById("player").hidden) {
    // Without a player here, the only place to see this moment is its own site.
    window.open(`${data.url}&t=${Math.floor(section.start)}s`, "_blank", "noopener");
    return;
  }
  if (!section.keep && mode !== FULL) chooseLength(FULL);
  seek(section.start);
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
        onclick: () => jumpTo(section),
      },
      el("span", { class: "muted" }, clock(section.start)),
      el("span", {}, section.note),
    ),
    el("span", { class: "tag" }, label),
    el(
      "button",
      { class: "small local-only", onclick: () => changeSection(section, !section.keep) },
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
  document.getElementById("ideas-label").textContent = `Key ideas (${data.ideas.length})`;
  document.getElementById("ideas").replaceChildren(...data.ideas.map(ideaItem));
  document.getElementById("verdict").replaceChildren(...verdictNodes(data));
}

function startPosition() {
  const requested = new URLSearchParams(location.search).get("t");
  if (requested !== null) return Number(requested);
  return Number(readStored("position")) || ranges[0][0];
}

async function showPlayer() {
  const savedLength = readStored("length");
  chooseLength(savedLength && rangesFor(savedLength)?.length ? savedLength : "3");
  const start = startPosition();
  if (!isInRanges(start) && new URLSearchParams(location.search).has("t")) chooseLength(FULL);
  document.getElementById("player").hidden = false;
  document.getElementById("sections-column").hidden = false;
  if (data.has_video && !PUBLISHED) {
    video.src = `/media/${encodeURIComponent(videoId)}`;
    video.addEventListener(
      "loadedmetadata",
      () => {
        seek(start);
        drawBars();
      },
      { once: true },
    );
  } else {
    try {
      video = await youtubePlayer(start);
    } catch (error) {
      showPlaysElsewhere(error.message);
      return;
    }
  }
  setSpeed(Number(readStored("speed")) || 1);
  renderSections();
  renderWatchedButton();
  attachBar(fullBar);
  attachBar(shortBar);

  document.getElementById("watched").addEventListener("click", () => setWatched(!data.watched));
  setInterval(jumpOverCuts, JUMP_CHECK_MS);
  setInterval(drawBars, BAR_REFRESH_MS);
  setInterval(() => {
    if (!video.paused) store("position", video.currentTime);
  }, SAVE_POSITION_MS);
}

function showPlaysElsewhere(problem) {
  document.getElementById("player").hidden = true;
  document.getElementById("not-ready").hidden = false;
  document.getElementById("shorten").hidden = true;
  document.getElementById("sections-column").hidden = false;
  const kept = data.tiers["3"].reduce((total, [start, end]) => total + (end - start), 0);
  document.getElementById("not-ready-message").replaceChildren(
    `Shortened from ${roundedLength(data.duration)} to ${roundedLength(kept)}. `,
    `${problem} `,
    el("a", { href: data.url, target: "_blank", rel: "noopener" }, "Open the original"),
  );
  renderSections();
}

function showNotReady() {
  document.getElementById("not-ready").hidden = false;
  const message = document.getElementById("not-ready-message");
  const button = document.getElementById("shorten");
  if (!data.url) {
    message.textContent = "This video is still being processed.";
    button.hidden = true;
    return;
  }
  if (PUBLISHED) {
    message.textContent = "This video hasn't been shortened yet. Shorten it on your Mac to watch it here.";
    return;
  }
  const show = (job) => {
    const running = job !== null && job.error === null;
    button.hidden = running;
    if (running) message.textContent = jobProgress(job);
    else if (job) message.textContent = `That didn't finish: ${job.error}`;
    else message.textContent = "This video hasn't been shortened yet.";
  };
  const follow = () => followJob(data.url, (item) => item.playable, show);
  button.addEventListener("click", async () => {
    button.hidden = true;
    await api("/api/jobs", { urls: [data.url], kind: "shorten" });
    follow();
  });
  follow();
}

async function load() {
  data = await api(`/api/videos/${encodeURIComponent(videoId)}`);
  renderNotes();
  if (data.playable) await showPlayer();
  else showNotReady();
}

load().catch((error) => {
  document.getElementById("title").textContent = error.message;
});
