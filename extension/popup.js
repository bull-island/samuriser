const MAX_PAGE_CHARACTERS = 5_000_000;
// Above this, a page's video is the content rather than a clip inside an article.
const MAIN_VIDEO_SECONDS = 180;

// Runs inside the tab. A video site's page text is mostly comments and menus, so it is
// only sent for pages that aren't built around a long video.
function readPage(mainVideoSeconds) {
  const videos = [...document.querySelectorAll("video")];
  if (videos.some((video) => video.duration > mainVideoSeconds)) return null;
  return document.documentElement.outerHTML;
}

async function pageOf(tabId) {
  try {
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId },
      func: readPage,
      args: [MAIN_VIDEO_SECONDS],
    });
    return result && result.length <= MAX_PAGE_CHARACTERS ? result : null;
  } catch {
    // Some pages can't be read. Summariser then fetches the link itself.
    return null;
  }
}

async function show() {
  const root = document.getElementById("card");
  // A link can be passed in directly, which is how the popup is opened in a full tab.
  const given = new URLSearchParams(location.search).get("url");
  const [tab] = given ? [null] : await chrome.tabs.query({ active: true, currentWindow: true });
  const url = given || tab?.url || "";
  if (!/^https?:/.test(url)) {
    root.replaceChildren(el("p", {}, "This page can't be saved. Open an article or video first."));
    return;
  }
  createCard(root, { url, getPage: async () => (tab ? pageOf(tab.id) : null) });
}

show();
