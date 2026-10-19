"""Read pages in the shared logged-in browser (claude-browser, CDP 127.0.0.1:9222).

Used by the watch check (orchestrator.step5w_watches) for product pages that
only show a member price when signed in (Amazon, Whole Foods). Each read
opens its own tab in the persistent profile and closes that tab; the browser
itself is left running. Read only: no clicks, no forms.
"""

CDP_ENDPOINT = "http://127.0.0.1:9222"
TEXT_MAX = 6000
NAV_TIMEOUT_MS = 30000


class BrowserUnavailable(Exception):
    pass


def read_pages(urls, endpoint=CDP_ENDPOINT):
    """{url: {"text": str, "title": str} | {"error": str}} for each url.
    Raises BrowserUnavailable when the shared browser cannot be reached."""
    from playwright.sync_api import sync_playwright
    out = {}
    try:
        pw = sync_playwright().start()
    except Exception as exc:
        raise BrowserUnavailable(str(exc).splitlines()[0][:200])
    try:
        try:
            browser = pw.chromium.connect_over_cdp(endpoint, timeout=15000)
        except Exception as exc:
            raise BrowserUnavailable(str(exc).splitlines()[0][:200])
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        for url in urls:
            page = context.new_page()
            try:
                page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
                page.wait_for_timeout(2000)   # let price widgets render
                text = page.inner_text("body", timeout=5000)
                out[url] = {"text": " ".join(text.split())[:TEXT_MAX],
                            "title": page.title()[:200]}
            except Exception as exc:
                out[url] = {"error": str(exc).splitlines()[0][:200]}
            finally:
                page.close()
    finally:
        pw.stop()
    return out
