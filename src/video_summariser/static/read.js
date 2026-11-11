import { api, el, followJob, jobProgress, roundedLength, verdictNodes } from "/static/ui.js";

const articleId = decodeURIComponent(location.pathname.split("/").pop());
const apiPath = `/api/videos/${encodeURIComponent(articleId)}`;
const FULL = "full";
const LENGTH_NAMES = { 1: "Essentials", 2: "Standard", 3: "Everything kept", [FULL]: "Full article" };
const SPEEDS = [1, 1.25, 1.5, 1.75, 2];
const LENGTH_KEY = `${articleId}:length`;
const POSITION_KEY = `${articleId}:position`;
const SPEED_KEY = "reader:speed";
const VOICE_KEY = "reader:voice";
const DEFAULT_LOCAL_VOICE = "af_sky";
const READ_AHEAD = 2;
// macOS ships novelty and very old voices that are unpleasant for a whole article.
const POOR_VOICES =
  /^(Albert|Bad News|Bahh|Bells|Boing|Bubbles|Cellos|Eddy|Flo|Fred|Good News|Grandma|Grandpa|Jester|Junior|Kathy|Organ|Ralph|Reed|Rocko|Sandy|Shelley|Superstar|Trinoids|Whisper|Wobble|Zarvox)\b/;

let data = null;
let mode = FULL;
let listening = false;
let utterance = null;
let position = 0;
let speed = 1;
let voices = [];
let voice = null;
// Kokoro's voices, used in preference to the browser's when it's running.
let localVoices = [];
let audio = null;

function readStored(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function store(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Remembering settings is only a convenience.
  }
}

function isShownAt(sentence, choice) {
  return choice === FULL || (sentence.tier !== null && sentence.tier <= Number(choice));
}

function allSentences() {
  return data.blocks.flatMap((block) => block.sentences);
}

function secondsAt(choice) {
  return allSentences()
    .filter((sentence) => isShownAt(sentence, choice))
    .reduce((total, sentence) => total + (sentence.end - sentence.start), 0);
}

function chooseLength(choice) {
  mode = choice;
  store(LENGTH_KEY, choice);
  renderLengths();
  renderArticle();
}

function renderLengths() {
  // Hide settings that would show exactly the same text as a shorter one.
  const seen = new Set();
  const options = (data.shortened ? ["1", "2", "3", FULL] : [FULL]).filter((choice) => {
    const seconds = Math.round(secondsAt(choice));
    if (seconds === 0 || seen.has(seconds)) return false;
    seen.add(seconds);
    return true;
  });
  document.getElementById("lengths").replaceChildren(
    ...options.map((choice) =>
      el(
        "button",
        { class: choice === mode ? "on" : "", onclick: () => chooseLength(choice) },
        `${LENGTH_NAMES[choice]} · ${roundedLength(secondsAt(choice))}`,
      ),
    ),
  );
}

function sectionHasText(headingIndex) {
  for (const block of data.blocks.slice(headingIndex + 1)) {
    if (block.heading) return false;
    if (block.sentences.some((sentence) => isShownAt(sentence, mode))) return true;
  }
  return false;
}

function shownBlocks() {
  return data.blocks.map((block, index) => ({
    heading: block.heading,
    sentences: block.heading
      ? sectionHasText(index) ? block.sentences : []
      : block.sentences.filter((sentence) => isShownAt(sentence, mode)),
  }));
}

function sentenceNode(sentence) {
  return el("span", { "data-start": sentence.start }, `${sentence.text} `);
}

function highlight(start) {
  for (const previous of document.querySelectorAll("#article .current")) {
    previous.classList.remove("current");
  }
  const nodes = [...document.querySelectorAll("#article [data-start]")];
  const node = nodes.find((candidate) => Number(candidate.dataset.start) === start);
  node?.classList.add("current");
  return node;
}

function renderArticle() {
  const nodes = shownBlocks()
    .filter((block) => block.sentences.length)
    .map((block) => el(block.heading ? "h2" : "p", {}, ...block.sentences.map(sentenceNode)));
  document.getElementById("article").replaceChildren(...nodes);
  if (listening) highlight(position);
}

function keepInView(node) {
  const box = node.getBoundingClientRect();
  if (box.top < 0 || box.bottom > window.innerHeight) node.scrollIntoView({ block: "center" });
}

function stopSpeaking() {
  // Cleared first, because cancelling makes the old sentence report that it ended.
  utterance = null;
  audio?.pause();
  audio = null;
  if ("speechSynthesis" in window) speechSynthesis.cancel();
}

function setListening(active) {
  listening = active;
  renderListenButton();
}

function finishListening() {
  setListening(false);
  position = 0;
  store(POSITION_KEY, 0);
  highlight(null);
}

function speechUrl(sentence) {
  const id = encodeURIComponent(articleId);
  return `/speech/${id}/${sentence.id}?voice=${encodeURIComponent(voice)}`;
}

function fallBackToBrowserVoices() {
  localVoices = [];
  renderVoices();
  speakFrom(position);
}

function playWithKokoro(sentence, upcoming) {
  const player = new Audio(speechUrl(sentence));
  // Loading resets the rate to this default, so setting playbackRate alone isn't enough.
  player.defaultPlaybackRate = speed;
  player.playbackRate = speed;
  player.onended = () => {
    if (audio === player) speakFrom(sentence.end);
  };
  player.onerror = () => {
    if (audio === player) fallBackToBrowserVoices();
  };
  audio = player;
  player.play().catch(() => {
    if (audio === player) setListening(false);
  });
  // Generating a sentence takes a couple of seconds, so stay ahead of the reader.
  for (const later of upcoming) fetch(speechUrl(later)).catch(() => {});
}

function playWithBrowser(sentence) {
  // Spoken a sentence at a time, because Chrome can stall on one very long piece of text.
  const spoken = new SpeechSynthesisUtterance(sentence.text);
  spoken.rate = speed;
  if (voice) spoken.voice = voice;
  spoken.onend = () => {
    if (utterance === spoken) speakFrom(sentence.end);
  };
  spoken.onerror = () => {
    if (utterance === spoken) setListening(false);
  };
  utterance = spoken;
  speechSynthesis.speak(spoken);
}

function speakFrom(start) {
  const remaining = shownBlocks()
    .flatMap((block) => block.sentences)
    .filter((sentence) => sentence.start >= start);
  const next = remaining[0];
  if (!next) {
    finishListening();
    return;
  }
  position = next.start;
  store(POSITION_KEY, position);
  const node = highlight(position);
  if (node) keepInView(node);
  if (localVoices.length) playWithKokoro(next, remaining.slice(1, READ_AHEAD + 1));
  else playWithBrowser(next);
}

function toggleListening() {
  setListening(!listening);
  if (listening) speakFrom(position);
  else stopSpeaking();
}

function listenFrom(start) {
  position = start;
  if (!listening) return;
  stopSpeaking();
  speakFrom(start);
}

function renderListenButton() {
  document.getElementById("listen-button").textContent = listening
    ? "Pause"
    : position > 0 ? "Resume listening" : "Listen";
}

function renderSpeeds() {
  document.getElementById("speeds").replaceChildren(
    ...SPEEDS.map((option) =>
      el(
        "button",
        {
          class: option === speed ? "on" : "",
          onclick: () => {
            speed = option;
            store(SPEED_KEY, option);
            renderSpeeds();
            if (audio) audio.playbackRate = option;
            else if (listening) listenFrom(position);
          },
        },
        `${option}×`,
      ),
    ),
  );
}

function usableVoices() {
  const all = speechSynthesis.getVoices();
  const language = navigator.language.split("-")[0];
  const matching = all.filter((option) => option.lang.startsWith(language));
  // On-device voices keep the article on this computer and don't cut out mid-sentence.
  const onDevice = matching.filter((option) => option.localService);
  const candidates = onDevice.length ? onDevice : matching.length ? matching : all;
  const listenable = candidates.filter((option) => !POOR_VOICES.test(option.name));
  return listenable.length ? listenable : candidates;
}

function bestVoice() {
  const stored = voices.find((option) => option.name === readStored(VOICE_KEY));
  return (
    stored ||
    voices.find((option) => /premium|enhanced/i.test(option.name)) ||
    voices.find((option) => option.lang === navigator.language) ||
    voices.find((option) => option.default) ||
    voices[0] ||
    null
  );
}

function localVoiceLabel(name) {
  const accent = name.startsWith("b") ? "British" : "American";
  const person = name.split("_")[1];
  return `${person[0].toUpperCase()}${person.slice(1)} (${accent})`;
}

function renderLocalVoices() {
  const stored = readStored(VOICE_KEY);
  voice = localVoices.includes(stored)
    ? stored
    : localVoices.includes(DEFAULT_LOCAL_VOICE)
      ? DEFAULT_LOCAL_VOICE
      : localVoices[0];
  const select = document.getElementById("voice");
  select.hidden = false;
  select.replaceChildren(
    ...localVoices.map((name) =>
      el("option", { value: name, selected: name === voice }, localVoiceLabel(name)),
    ),
  );
}

function renderBrowserVoices() {
  voices = usableVoices();
  voice = bestVoice();
  const select = document.getElementById("voice");
  select.hidden = !voices.length;
  select.replaceChildren(
    ...voices.map((option) =>
      el("option", { value: option.name, selected: option === voice }, option.name),
    ),
  );
}

function renderVoices() {
  if (localVoices.length) renderLocalVoices();
  else if ("speechSynthesis" in window) renderBrowserVoices();
}

function chooseVoice(event) {
  voice = localVoices.length
    ? event.target.value
    : voices.find((option) => option.name === event.target.value) || null;
  store(VOICE_KEY, event.target.value);
  if (listening) listenFrom(position);
}

async function setUpListening() {
  localVoices = await api("/api/speech/voices").catch(() => []);
  if (!localVoices.length && !("speechSynthesis" in window)) return;
  document.getElementById("listen").hidden = false;
  speed = Number(readStored(SPEED_KEY)) || 1;
  position = Number(readStored(POSITION_KEY)) || 0;
  renderListenButton();
  renderSpeeds();
  renderVoices();
  if ("speechSynthesis" in window) speechSynthesis.addEventListener("voiceschanged", renderVoices);
  document.getElementById("listen-button").addEventListener("click", toggleListening);
  document.getElementById("voice").addEventListener("change", chooseVoice);
  document.getElementById("article").addEventListener("click", (event) => {
    const sentence = event.target.closest("[data-start]");
    if (sentence && listening) listenFrom(Number(sentence.dataset.start));
  });
  window.addEventListener("pagehide", stopSpeaking);
}

function jumpTo(target) {
  const found = allSentences().find((sentence) => sentence.start >= target);
  if (!found) return;
  if (!isShownAt(found, mode)) chooseLength(FULL);
  highlight(found.start)?.scrollIntoView({ block: "center" });
  listenFrom(found.start);
  renderListenButton();
}

function ideaItem(idea, number) {
  const overlap = data.overlaps.find((o) => o.idea === number);
  const source = overlap ? data.overlap_titles[overlap.video_id] : null;
  const isNew = data.new_ideas.includes(number);
  return el(
    "li",
    { class: "idea" },
    el("button", { class: "idea-jump", onclick: () => jumpTo(idea.start) }, el("span", {}, idea.text)),
    isNew && data.overlaps.length ? el("span", { class: "tag new" }, "New to you") : null,
    source ? el("span", { class: "tag" }, `Already in: ${source}`) : null,
  );
}

function renderNotes() {
  document.title = data.title;
  document.getElementById("title").textContent = data.title;
  document.getElementById("meta").textContent = `${data.platform} · ${roundedLength(data.duration)} read`;
  document.getElementById("summary").textContent = data.summary || "No summary yet.";
  document.getElementById("ideas-label").textContent = `Key ideas (${data.ideas.length})`;
  document.getElementById("ideas").replaceChildren(...data.ideas.map(ideaItem));
  document.getElementById("verdict").replaceChildren(...verdictNodes(data));
  document.getElementById("watched").textContent = data.watched ? "Read ✓" : "Mark as read";
}

async function toggleRead() {
  await api(`${apiPath}/watched`, { watched: !data.watched });
  const latest = await api(apiPath);
  Object.assign(data, {
    watched: latest.watched,
    verdict: latest.verdict,
    reason: latest.reason,
  });
  renderNotes();
}

function showNotReady() {
  const message = document.getElementById("not-ready-message");
  const button = document.getElementById("shorten");
  document.getElementById("not-ready").hidden = false;
  const show = (job) => {
    const running = job !== null && job.error === null;
    button.hidden = running;
    if (running) message.textContent = jobProgress(job);
    else if (job) message.textContent = `That didn't finish: ${job.error}`;
    else message.textContent = "This article hasn't been shortened yet.";
  };
  const follow = () => followJob(data.url, (item) => item.shortened, show);
  button.addEventListener("click", async () => {
    button.hidden = true;
    await api("/api/jobs", { urls: [data.url], kind: "shorten" });
    follow();
  });
  follow();
}

async function load() {
  data = await api(apiPath);
  renderNotes();
  const stored = readStored(LENGTH_KEY);
  if (!data.shortened) showNotReady();
  chooseLength(data.shortened ? stored || "3" : FULL);
  await setUpListening();
  document.getElementById("watched").addEventListener("click", toggleRead);
  const requested = new URLSearchParams(location.search).get("t");
  if (requested !== null) jumpTo(Number(requested));
}

load().catch((error) => {
  document.getElementById("title").textContent = error.message;
});
