// Adds a Summariser line beside a YouTube video, and can play a shortened video in
// YouTube's own player by jumping over the cuts.
const JUMP_CHECK_MS = 100;
const EDGE_TOLERANCE = 0.05;
// If YouTube hasn't resumed this long after a jump, shortened playback isn't usable here.
const STALL_MS = 8000;
const HOST_WAIT_MS = 15000;
const LENGTH_NAMES = { 1: "Essentials", 2: "Standard", 3: "Everything kept" };

let generation = 0;
let root = null;
let card = null;
let playback = null;

function videoUrl() {
  const id = new URLSearchParams(location.search).get("v");
  return location.pathname === "/watch" && id ? `https://www.youtube.com/watch?v=${id}` : null;
}

function player() {
  return document.querySelector("video.html5-main-video") || document.querySelector("video");
}

function adShowing() {
  return document.querySelector(".html5-video-player.ad-showing") !== null;
}

function totalLength(ranges) {
  return ranges.reduce((total, [start, end]) => total + (end - start), 0);
}

function lengthOptions(tiers) {
  // Leave out settings that would play exactly the same thing as a shorter one.
  const seen = new Set();
  return ["1", "2", "3"].filter((tier) => {
    const seconds = Math.round(totalLength(tiers[tier] || []));
    if (seconds === 0 || seen.has(seconds)) return false;
    seen.add(seconds);
    return true;
  });
}

function median(values) {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.floor(sorted.length / 2)];
}

function jumpSummary() {
  const { jumps } = playback;
  if (!jumps.length) return playback.on ? "Cuts will be skipped as you watch." : "";
  const seconds = (ms) => (ms / 1000).toFixed(1);
  const count = `${jumps.length} ${jumps.length === 1 ? "jump" : "jumps"}`;
  return `${count} · typically ${seconds(median(jumps))} s · slowest ${seconds(Math.max(...jumps))} s`;
}

function showJumpSummary() {
  if (playback.stats) playback.stats.textContent = jumpSummary();
}

function stopShortened({ failed = false } = {}) {
  clearInterval(playback.timer);
  Object.assign(playback, { on: false, timer: null, pending: null, failed });
  card.redraw();
}

async function finishShortened() {
  player()?.pause();
  const { item } = playback;
  stopShortened();
  if (!item.watched) await ask({ type: "watched", id: item.id, watched: true });
  card.refresh();
}

function settleJump(video) {
  const { started, target } = playback.pending;
  const waited = performance.now() - started;
  if (waited > STALL_MS) {
    stopShortened({ failed: true });
  } else if (!video.seeking && video.readyState >= 3) {
    // A player that snaps back instead of jumping would otherwise be sent forward forever.
    if (video.currentTime < target - EDGE_TOLERANCE) {
      stopShortened({ failed: true });
      return;
    }
    playback.jumps.push(waited);
    playback.pending = null;
    showJumpSummary();
  }
}

function skipCuts() {
  const video = player();
  if (!video || adShowing()) return;
  if (playback.pending !== null) {
    settleJump(video);
    return;
  }
  if (video.paused) return;
  const time = video.currentTime;
  const next = playback.item.tiers[playback.tier].find(([, end]) => time < end - EDGE_TOLERANCE);
  if (!next) {
    finishShortened();
  } else if (time < next[0] - EDGE_TOLERANCE) {
    playback.pending = { started: performance.now(), target: next[0] };
    video.currentTime = next[0];
  }
}

function toggleShortened() {
  if (playback.on) {
    stopShortened();
    return;
  }
  playback.on = true;
  playback.timer = setInterval(skipCuts, JUMP_CHECK_MS);
  card.redraw();
}

function playbackControls(item) {
  if (item.kind !== "video" || !item.playable) return [];
  playback.item = item;
  if (playback.failed) {
    return [
      el(
        "span",
        { class: "vs-error vs-wide" },
        "YouTube couldn't jump over the cuts smoothly, so shortened playback is off for this video. ",
        itemLink(item, "Play the shortened version in Summariser"),
      ),
    ];
  }
  const options = lengthOptions(item.tiers);
  if (!options.includes(playback.tier)) playback.tier = options[options.length - 1];
  playback.stats = el("span", { class: "vs-muted vs-wide", role: "status" }, jumpSummary());
  return [
    el(
      "button",
      { class: playback.on ? "vs-primary" : "", "aria-pressed": String(playback.on), onclick: toggleShortened },
      playback.on ? "Playing shortened ✓" : "Play shortened here",
    ),
    el(
      "select",
      {
        "aria-label": "Length",
        onchange: (event) => {
          playback.tier = event.target.value;
        },
      },
      ...options.map((tier) =>
        el(
          "option",
          { value: tier, selected: tier === playback.tier },
          `${LENGTH_NAMES[tier]} · ${roundedLength(totalLength(item.tiers[tier]))}`,
        ),
      ),
    ),
    playback.stats,
  ];
}

async function panelHost() {
  const deadline = performance.now() + HOST_WAIT_MS;
  while (performance.now() < deadline) {
    const host = document.querySelector("#secondary-inner") || document.querySelector("#secondary");
    if (host) return host;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return null;
}

function unmount() {
  if (playback) clearInterval(playback.timer);
  card?.stop();
  root?.remove();
  root = null;
  card = null;
  playback = null;
}

// YouTube changes videos without loading a new page, so the panel is rebuilt on each one.
async function mount() {
  const mine = ++generation;
  unmount();
  const url = videoUrl();
  if (!url) return;
  const host = await panelHost();
  if (mine !== generation) return;
  playback = { on: false, tier: "3", timer: null, pending: null, jumps: [], failed: false, item: null, stats: null };
  root = el("section", { class: host ? "vs-panel" : "vs-panel vs-floating", "aria-label": "Summariser" });
  (host || document.body).prepend(root);
  card = createCard(root, { url, extras: playbackControls });
}

document.addEventListener("yt-navigate-finish", mount);
// A video may be shortened or added elsewhere while this tab sits in the background.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) card?.refresh();
});
mount();
