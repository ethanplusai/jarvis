"""
JARVIS Browser — headless Playwright, for JARVIS's own eyes.

`read_page` and `capture_page` (behind the `read_page` / `look_at_page`
tools) open one throwaway headless Chromium per call and always close it.
The old headful search/research class was removed: nothing but its own
integration test reached it.
"""

import logging
from dataclasses import dataclass

log = logging.getLogger("jarvis.browser")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Looking at a page for JARVIS himself
# ---------------------------------------------------------------------------
#
# The user, twice: "when I tell you to open a website it'd be great if we
# could look at things together ... you can understand everything that I'm
# actually seeing visually and/or you get a really quick data back of the
# content that's on the page so you can read it very quick."
#
# HEADLESS, deliberately. These two functions exist so the BRAIN can read or
# see a page in the middle of a spoken turn — for itself, not for the user. Showing the user a page is
# already `open_in_browser`, which uses their own real browser with their own
# logins and extensions. A second, visible, cookie-less Chromium popping up
# and stealing focus every time JARVIS glanced at a URL would be a bug, not a
# feature: this project treats taking the user's focus as something only an
# ACTING tool may do, and reading is not acting.
#
# One throwaway browser per call, always closed, so a glance can never
# leave a window behind or inherit a half-dead context.

# Every one of these must finish WELL inside `jarvis_mcp.TIMEOUT_SEC` (20s).
# A handler that outlives that tells the brain the server is unreachable
# while the work carries on regardless — the exact lie documented at the top
# of jarvis_mcp.py. Launch (~0.5s) + navigate (<=12s) + capture (~1s) leaves
# real headroom, and the caller puts its own hard deadline on top of this.
LOOK_TIMEOUT_MS = 12_000

# 1280x800 is a laptop window: wide enough that a site renders its desktop
# layout rather than its mobile one, small enough that the PNG stays a few
# hundred KB and the image costs the brain on the order of a thousand tokens.
LOOK_VIEWPORT = {"width": 1280, "height": 800}

# What the page evaluator may hand back. This is NOT the brain's budget —
# that is the caller's, and it is far smaller. This only stops a pathological
# page from moving megabytes over the loopback hop before anyone trims it.
PAGE_TEXT_CHARS = 20_000

# A viewport-sized PNG bigger than this is not a screenshot of a web page, it
# is something that will not fit through the tool channel. Refuse it out loud
# rather than sending something the CLI will choke on.
MAX_SHOT_BYTES = 4_000_000


class PageError(Exception):
    """A page JARVIS could not read or see. The message is speakable."""


@dataclass
class PageText:
    title: str
    url: str
    text: str
    char_count: int          # the extracted length BEFORE the caller's budget
    truncated: bool          # PAGE_TEXT_CHARS clipped it


@dataclass
class PageShot:
    title: str
    url: str
    png: bytes


# The same idea as `visit()`, with two differences that matter inside a small
# budget:
#
# 1. the noise is removed from the LIVE document, not from a detached clone.
#    `innerText` is defined in terms of RENDERED text, and a clone that is not
#    in the document has no layout — so `clone.innerText` silently degrades to
#    something textContent-shaped and runs every block together. Measured: a
#    heading and the paragraph under it came back as "ARC REACTOR STATUSOutput
#    is nominal", one unreadable word. Mutating the page is free here: the tab
#    is headless, private to this call, and destroyed a few lines later.
# 2. the bound is passed IN rather than hard-coded in the middle of a JS
#    string, so it cannot drift from the Python constant that documents it.
_EXTRACT_JS = """
(limit) => {
    const main = document.querySelector('main')
        || document.querySelector('article')
        || document.querySelector('[role="main"]')
        || document.body;
    for (const el of main.querySelectorAll(
        'script, style, noscript, nav, header, footer, aside, .sidebar, .menu, .ad, .advertisement, iframe'
    )) {
        el.remove();
    }
    const text = (main.innerText || main.textContent || '')
        .replace(/[ \\t]+/g, ' ')
        .replace(/\\n{3,}/g, '\\n\\n')
        .trim();
    return {title: document.title || '', full: text.length,
            text: text.substring(0, limit)};
}
"""


class _Headless:
    """One throwaway headless Chromium, guaranteed closed.

    `async with _Headless() as page:` — every exit path, exception included,
    shuts the browser and stops Playwright. A leaked Chromium here would
    outlive JARVIS himself.
    """

    def __init__(self):
        self._pw = None
        self._browser = None

    async def __aenter__(self):
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.launch(headless=True)
            context = await self._browser.new_context(
                user_agent=USER_AGENT, viewport=dict(LOOK_VIEWPORT))
            page = await context.new_page()
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        page.set_default_timeout(LOOK_TIMEOUT_MS)
        return page

    async def __aexit__(self, *exc):
        for close in (getattr(self._browser, "close", None),
                      getattr(self._pw, "stop", None)):
            if close is None:
                continue
            try:
                await close()
            except Exception as e:                  # never mask the real error
                log.warning(f"headless teardown: {e}")
        self._pw = None
        self._browser = None
        return False


async def read_page(url: str) -> PageText:
    """The readable text of one page. Raises PageError if it cannot be had."""
    try:
        async with _Headless() as page:
            await page.goto(url, wait_until="domcontentloaded",
                            timeout=LOOK_TIMEOUT_MS)
            data = await page.evaluate(_EXTRACT_JS, PAGE_TEXT_CHARS)
            landed = page.url or url
    except PageError:
        raise
    except Exception as e:
        log.warning(f"read_page failed for {url}: {e}")
        raise PageError("that page wouldn't load") from e

    if not isinstance(data, dict):
        raise PageError("that page had no readable text on it")
    text = str(data.get("text") or "")
    full = int(data.get("full") or len(text))
    if not text.strip():
        raise PageError("that page had no readable text on it")
    return PageText(title=str(data.get("title") or ""), url=str(landed),
                    text=text, char_count=full, truncated=full > len(text))


async def capture_page(url: str) -> PageShot:
    """A viewport-sized PNG of one page. Raises PageError if it cannot be had.

    Viewport, NOT `full_page`: a full-page capture of a long article is a tall
    thin strip that costs the brain a great many tokens and shows it what a
    person would only see by scrolling. What the user means by "look at this
    with me" is the screenful in front of him.
    """
    try:
        async with _Headless() as page:
            await page.goto(url, wait_until="domcontentloaded",
                            timeout=LOOK_TIMEOUT_MS)
            # Let web fonts, images and any late layout settle. A screenshot
            # of a half-painted page is worse than no screenshot at all.
            await page.wait_for_timeout(700)
            title = await page.title()
            png = await page.screenshot(type="png", full_page=False)
            landed = page.url or url
    except PageError:
        raise
    except Exception as e:
        log.warning(f"capture_page failed for {url}: {e}")
        raise PageError("I couldn't get a picture of that page") from e

    if not png:
        raise PageError("I couldn't get a picture of that page")
    if len(png) > MAX_SHOT_BYTES:
        raise PageError("that page's picture came out far too large to send")
    return PageShot(title=str(title or ""), url=str(landed), png=bytes(png))
