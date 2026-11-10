// The only part that talks to the local Summariser. Pages on other sites can't call it
// directly, so the popup and the YouTube panel send their requests through here.
const SERVER = "http://127.0.0.1:8765";
const MENU_ID = "add-to-summariser";
const BADGE_MS = 4000;

async function call(path, body) {
  const options = body === undefined
    ? {}
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const response = await fetch(`${SERVER}${path}`, options);
  if (!response.ok) {
    const problem = await response.json().catch(() => ({}));
    throw new Error(String(problem.detail || `Summariser said ${response.status}`));
  }
  return response.json();
}

function handle(message) {
  if (message.type === "add") return addLink(message.url);
  if (message.type === "lookup") return call(`/api/lookup?url=${encodeURIComponent(message.url)}`);
  if (message.type === "job") {
    return call("/api/jobs", { urls: [message.url], kind: message.kind, page: message.page ?? null });
  }
  if (message.type === "watched") {
    return call(`/api/videos/${encodeURIComponent(message.id)}/watched`, { watched: message.watched });
  }
  return Promise.reject(new Error("Unknown request"));
}

function flashBadge(text, color, title) {
  chrome.action.setBadgeBackgroundColor({ color });
  chrome.action.setBadgeText({ text });
  chrome.action.setTitle({ title });
  setTimeout(() => chrome.action.setBadgeText({ text: "" }), BADGE_MS);
}

// Adds a link from a menu, and queues the "worth my time?" check so the verdict is
// waiting in Summariser. A link that's already there is left alone. Returns what happened.
async function addLink(url) {
  if (!/^https?:/.test(url || "")) return "failed";
  try {
    const { item, job } = await call(`/api/lookup?url=${encodeURIComponent(url)}`);
    const known = item !== null || (job !== null && job.error === null);
    if (!known) await call("/api/jobs", { urls: [url], kind: "check", page: null });
    flashBadge("✓", "#1baf7a", known ? "Already in Summariser" : "Added to Summariser");
    return known ? "known" : "added";
  } catch (error) {
    const offline = error instanceof TypeError;
    flashBadge("!", "#e66767", offline ? "Summariser isn't running" : `Couldn't add: ${error.message}`);
    return offline ? "offline" : "failed";
  }
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: MENU_ID,
      title: "Add to Summariser",
      contexts: ["link", "page", "video"],
    });
  });
});

chrome.contextMenus.onClicked.addListener((info) => {
  if (info.menuItemId === MENU_ID) addLink(info.linkUrl || info.pageUrl);
});

chrome.runtime.onMessage.addListener((message, _sender, respond) => {
  handle(message).then(
    (data) => respond({ ok: true, data }),
    // fetch reports an unreachable server as a TypeError.
    (error) => respond({ ok: false, offline: error instanceof TypeError, message: error.message }),
  );
  return true;
});
