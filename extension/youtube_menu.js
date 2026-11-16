// Adds "Add to Samuriser" to the three-dot menu on YouTube's video thumbnails.
// YouTube has no supported way to extend that menu, so this relies on its element names
// and will need updating when they change. The right-click menu doesn't depend on any of it.
const VIDEO_ITEMS = [
  "yt-lockup-view-model",
  "ytd-rich-item-renderer",
  "ytd-video-renderer",
  "ytd-compact-video-renderer",
  "ytd-playlist-video-renderer",
  "ytd-grid-video-renderer",
].join(", ");
const VIDEO_LINKS = "a[href*='/watch?v='], a[href*='/shorts/']";
// The list inside an open menu, most specific first. YouTube has an older and a newer style.
const MENU_LISTS = ["tp-yt-paper-listbox", "yt-list-view-model", "[role='listbox']", "[role='menu']"];
// Anything that could be the three-dot button, whichever way YouTube builds it.
const BUTTONS = "button, [role='button'], yt-icon-button, button-view-model";
// Parts of the page that are never a pop-up menu.
const PAGE_AREAS = "#content, #masthead-container, #guide, #secondary";
const MENU_WAIT_MS = 2500;
const MENU_POLL_MS = 50;
const CONFIRM_MS = 900;
const RESULTS = {
  added: "Added ✓",
  known: "Already added ✓",
  offline: "Samuriser isn't running",
  failed: "Couldn't add",
};

let clicks = 0;

function isShown(node) {
  return node.getClientRects().length > 0;
}

function openMenuList() {
  for (const list of MENU_LISTS) {
    const found = document.querySelectorAll(`ytd-popup-container ${list}, tp-yt-iron-dropdown ${list}`);
    const visible = [...found].find(isShown);
    if (visible) return visible;
  }
  // Fall back to anything menu-like that floats outside the page's own areas.
  for (const list of MENU_LISTS) {
    const visible = [...document.querySelectorAll(list)].find(
      (candidate) => isShown(candidate) && !candidate.closest(PAGE_AREAS),
    );
    if (visible) return visible;
  }
  return null;
}

function outline(node, depth = 0) {
  if (depth > 5) return "";
  const role = node.getAttribute("role");
  const line = `${"  ".repeat(depth)}${node.tagName.toLowerCase()}${role ? `[role=${role}]` : ""}`;
  const children = [...node.children].filter(isShown).slice(0, 4);
  return [line, ...children.map((child) => outline(child, depth + 1))].filter(Boolean).join("\n");
}

// Printed when a video's menu was opened but not recognised, to show how YouTube built it.
function reportUnknownMenu() {
  const popups = [...document.querySelectorAll("ytd-popup-container > *, tp-yt-iron-dropdown, [role='menu'], [role='dialog']")]
    .filter((node) => isShown(node) && !node.closest(PAGE_AREAS));
  const found = popups.map((node) => outline(node)).join("\n---\n") || "(no pop-up found)";
  console.info(`[Samuriser] Couldn't find this video's menu. Its structure:\n${found}`);
}

// YouTube fixes the menu's height as it opens, before this item exists, so without
// this the item sits below the cut-off and can only be reached by scrolling.
function makeRoomFor(entry) {
  for (let node = entry.parentElement; node && node.tagName !== "TP-YT-IRON-DROPDOWN"; node = node.parentElement) {
    const limit = parseFloat(node.style.maxHeight);
    if (!Number.isNaN(limit)) node.style.maxHeight = `${limit + entry.offsetHeight}px`;
  }
}

function closeMenu() {
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", keyCode: 27, bubbles: true }));
  document.body.click();
}

function menuItem(url) {
  const label = el("span", {}, "Add to Samuriser");
  const item = el(
    "div",
    { class: "vs-menu-item", role: "menuitem", tabindex: "0" },
    el("span", { class: "vs-menu-icon", "aria-hidden": "true" }, "+"),
    label,
  );
  const add = async (event) => {
    // Kept from YouTube, which would otherwise treat this as a click on the video.
    event.stopPropagation();
    event.preventDefault();
    const reply = await ask({ type: "add", url }).catch(() => null);
    label.textContent = RESULTS[reply?.ok ? reply.data : "failed"];
    setTimeout(closeMenu, CONFIRM_MS);
  };
  item.addEventListener("click", add);
  item.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") add(event);
  });
  return item;
}

document.addEventListener(
  "click",
  (event) => {
    if (event.target.closest?.(".vs-menu-item")) return;
    const mine = ++clicks;
    // YouTube reuses one menu for everything, so an item from an earlier video must go.
    document.querySelector(".vs-menu-item")?.remove();
    const item = event.target.closest?.(VIDEO_ITEMS);
    const pressed = event.target.closest?.(BUTTONS);
    const link = item && pressed && item.contains(pressed) ? item.querySelector(VIDEO_LINKS) : null;
    if (!link) return;
    const url = link.href;
    const deadline = performance.now() + MENU_WAIT_MS;
    let added = false;
    // The menu appears a moment after the click, and YouTube may redraw it once.
    const timer = setInterval(() => {
      if (mine !== clicks || performance.now() > deadline) {
        clearInterval(timer);
        if (mine === clicks && !added) reportUnknownMenu();
        return;
      }
      const list = openMenuList();
      if (list && !list.querySelector(".vs-menu-item")) {
        const entry = menuItem(url);
        list.append(entry);
        makeRoomFor(entry);
        added = true;
      }
    }, MENU_POLL_MS);
  },
  true,
);
