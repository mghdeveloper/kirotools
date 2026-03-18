from flask import Flask, request, Response, jsonify
from playwright.sync_api import sync_playwright
from ddgs import DDGS
import requests
import threading
from reportlab.platypus import SimpleDocTemplate, Image
from io import BytesIO
from PIL import Image as PILImage
import tempfile
import os

app = Flask(__name__)

# =========================
# GLOBALS
# =========================

playwright = None
browser = None
context = None
cookies = {}
headers = {}
session = requests.Session()
lock = threading.Lock()

# limit heavy PDF builds
from threading import Semaphore
pdf_semaphore = Semaphore(2)

# =========================
# PLAYWRIGHT IMAGE FETCHER
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


def refresh_session():
    global cookies, headers, context

    print("Refreshing cookies...")

    page = context.new_page()
    page.goto("https://comix.to/", wait_until="domcontentloaded")

    cookies_list = context.cookies()
    cookies = {c["name"]: c["value"] for c in cookies_list}

    headers = {
        "User-Agent": page.evaluate("() => navigator.userAgent"),
        "Referer": "https://comix.to/",
        "Origin": "https://comix.to"
    }

    page.close()
    print("Cookies refreshed")


def fast_fetch(url):
    try:
        r = session.get(
            url,
            headers=headers,
            cookies=cookies,
            timeout=10
        )
        if r.status_code == 200:
            return r
        return None
    except Exception as e:
        print("Fast fetch error:", e)
        return None


@app.route("/proxy")
def proxy():
    global context

    url = request.args.get("url")
    if not url:
        return "Missing url parameter", 400

    if context is None:
        with lock:
            if context is None:
                start_browser()

    r = fast_fetch(url)
    if r:
        return Response(r.content, status=200, content_type=r.headers.get("content-type"))

    refresh_session()

    r = fast_fetch(url)
    if r:
        return Response(r.content, status=200, content_type=r.headers.get("content-type"))

    return "Failed", 500


# =========================
# 🚀 PDF BUILDER (NEW)
# =========================

@app.route("/build_pdf", methods=["POST"])
def build_pdf():
    if not pdf_semaphore.acquire(blocking=False):
        return jsonify({"error": "Server busy"}), 429

    try:
        data = request.json
        image_urls = data.get("images", [])[:120]

        if not image_urls:
            return jsonify({"error": "No images"}), 400

        temp_pdf = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        doc = SimpleDocTemplate(temp_pdf.name)

        elements = []

        for url in image_urls:
            try:
                # 🔥 USE YOUR EXTERNAL PROXY
                proxy_url = f"https://kiroflix.site/backend/mangaposterproxy.php?url={url}"

                r = requests.get(proxy_url, timeout=20)

                if r.status_code != 200:
                    continue

                img = PILImage.open(BytesIO(r.content)).convert("RGB")
                img.thumbnail((1200, 2000))

                img_temp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
                img.save(img_temp.name, "JPEG", quality=80)

                elements.append(Image(img_temp.name))

            except Exception as e:
                print("Image error:", e)

        if not elements:
            return jsonify({"error": "PDF build failed"}), 500

        doc.build(elements)

        def generate():
            with open(temp_pdf.name, "rb") as f:
                while chunk := f.read(8192):
                    yield chunk

            os.unlink(temp_pdf.name)

        return Response(generate(), content_type="application/pdf")

    finally:
        pdf_semaphore.release()


# =========================
# SEARCH
# =========================

def ddg_search(query, max_results=5):
    try:
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results))
    except Exception as e:
        print("Search error:", e)
        return []


@app.route("/search")
def search():
    query = request.args.get("q", "")
    max_results = int(request.args.get("max_results", 5))

    if not query:
        return jsonify({"error": "No query provided"}), 400

    results = ddg_search(query, max_results)

    return jsonify({
        "results": [
            {
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "description": r.get("body", "")
            }
            for r in results
        ]
    })


@app.route("/")
def home():
    return """
    <h2>Unified API</h2>
    <p>/proxy?url=IMAGE_URL</p>
    <p>/build_pdf (POST)</p>
    <p>/search?q=QUERY</p>
    """


if __name__ == "__main__":
    start_browser()
    app.run(host="0.0.0.0", port=5000, threaded=True)
