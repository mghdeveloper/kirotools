from flask import Flask, request, Response, jsonify
from playwright.sync_api import sync_playwright
from ddgs import DDGS
import requests
import threading
from urllib.parse import urlparse

app = Flask(__name__)

playwright = None
browser = None
context = None
cookies = {}
session = requests.Session()
lock = threading.Lock()

# =========================
# PLAYWRIGHT START
# =========================

def start_browser():
    global playwright, browser, context

    if context is not None:
        return

    print("Starting Playwright browser...")

    playwright = sync_playwright().start()

    browser = playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu"
        ]
    )

    context = browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        locale="en-US"
    )

    refresh_session()

    print("Playwright ready")


# =========================
# COOKIE REFRESH
# =========================

def refresh_session():
    global cookies, context

    try:
        print("Refreshing cookies...")

        page = context.new_page()

        # load main site
        page.goto("https://comix.to/", wait_until="domcontentloaded")

        # load static domain
        page.goto("https://static.comix.to/", wait_until="domcontentloaded")

        cookies_list = context.cookies()

        cookies = {c["name"]: c["value"] for c in cookies_list}

        print(f"Cookies loaded: {len(cookies)}")

        page.close()

    except Exception as e:
        print("Cookie refresh error:", str(e))


# =========================
# FAST FETCH
# =========================

def fast_fetch(url):
    try:

        parsed = urlparse(url)

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": f"https://{parsed.netloc}/",
            "Origin": f"https://{parsed.netloc}"
        }

        r = session.get(
            url,
            headers=headers,
            cookies=cookies,
            timeout=10
        )

        if r.status_code == 200:
            return r

        print("Fetch failed status:", r.status_code, url)

        return None

    except Exception as e:
        print("Fast fetch error:", str(e))
        return None


# =========================
# PROXY ROUTE
# =========================

@app.route("/proxy")
def proxy():
    global context

    url = request.args.get("url")

    if not url:
        return "Missing url parameter", 400

    try:

        # lazy start browser
        if context is None:
            with lock:
                if context is None:
                    start_browser()

        r = fast_fetch(url)

        if r:
            return Response(
                r.content,
                status=r.status_code,
                content_type=r.headers.get("content-type", "image/webp")
            )

        print("Retry after refreshing cookies:", url)

        refresh_session()

        r = fast_fetch(url)

        if r:
            return Response(
                r.content,
                status=r.status_code,
                content_type=r.headers.get("content-type", "image/webp")
            )

        print("Proxy failed completely:", url)

        return "Proxy failed", 500

    except Exception as e:
        print("Proxy error:", str(e))
        return "Internal error", 500


# =========================
# DUCKDUCKGO SEARCH
# =========================

def ddg_search(query, max_results=5):
    try:
        with DDGS() as ddgs:
            results = ddgs.text(query, max_results=max_results)
            return list(results)
    except Exception as e:
        print("Search error:", str(e))
        return []


@app.route("/search")
def search():

    query = request.args.get("q", "")
    max_results = int(request.args.get("max_results", 5))

    if not query:
        return jsonify({"error": "No query provided"}), 400

    results = ddg_search(query, max_results)

    simplified = [
        {
            "title": r.get("title", "No title"),
            "url": r.get("href", "No link"),
            "description": r.get("body", "No description")
        }
        for r in results
    ]

    return jsonify({"results": simplified})


# =========================
# HOME
# =========================

@app.route("/")
def home():
    return """
    <h2>Unified API</h2>
    <p>/proxy?url=IMAGE_URL</p>
    <p>/search?q=QUERY</p>
    """


# =========================
# RUN
# =========================

if __name__ == "__main__":
    start_browser()
    app.run(host="0.0.0.0", port=5000, threaded=True)
