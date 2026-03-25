from flask import Flask, request, Response, jsonify
from playwright.sync_api import sync_playwright
from ddgs import DDGS
import requests
import threading
from io import BytesIO
from PIL import Image
import tempfile
import os
import uuid
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

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
jobs = {}
jobs_lock = threading.Lock()

MAX_WORKERS = 5
MAX_RETRIES = 2
PDF_SEMAPHORE = threading.Semaphore(2)

# =========================
# PLAYWRIGHT INIT
# =========================
def start_browser():
    global playwright, browser, context
    if context is not None:
        return

    print("🚀 Starting Playwright...")

    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
    )

    context = browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        locale="en-US"
    )

    refresh_session()

# =========================
# SESSION / COOKIES
# =========================
def refresh_session():
    global cookies, headers

    print("🔄 Refreshing session...")

    page = context.new_page()

    try:
        page.goto("https://comix.to/", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(4000)

        cookies_list = context.cookies()
        cookies = {c["name"]: c["value"] for c in cookies_list}

        headers = {
            "User-Agent": page.evaluate("() => navigator.userAgent"),
            "Referer": "https://comix.to/",
            "Origin": "https://comix.to",
            "Accept": "application/json, text/plain, */*",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin"
        }

        print("✅ Session ready")

    except Exception as e:
        print("❌ Session error:", e)

    finally:
        page.close()

# =========================
# FETCH HELPERS
# =========================
def is_cf_blocked(text):
    return (
        "Just a moment" in text or
        "cf_chl" in text or
        "<html" in text.lower()
    )


def fetch_comix_api(url):
    try:
        r = session.get(url, headers=headers, cookies=cookies, timeout=15)

        if r.status_code != 200:
            return None

        if is_cf_blocked(r.text):
            return None

        return r.text

    except Exception as e:
        print("Fetch error:", e)
        return None


def fast_fetch(url):
    try:
        r = session.get(url, headers=headers, cookies=cookies, timeout=10)

        if r.status_code == 200 and not is_cf_blocked(r.text):
            return r

        return None

    except Exception as e:
        print("Fast fetch error:", e)
        return None

# =========================
# DIRECT MANGA ROUTE
# =========================
@app.route("/manga")
def manga_search():
    global context

    query = request.args.get("q", "").strip()

    if not query:
        return jsonify({"success": False})

    url = f"https://comix.to/api/v2/manga?keyword={query}&limit=28"

    if context is None:
        with lock:
            if context is None:
                start_browser()

    # try fast twice
    for i in range(2):
        print(f"⚡ Fast attempt {i+1}")
        data = fetch_comix_api(url)

        if data:
            return Response(data, content_type="application/json")

        time.sleep(1)

    # fallback
    print("🛡️ Using Playwright fallback...")

    try:
        page = context.new_page()

        page.goto("https://comix.to/", wait_until="networkidle", timeout=60000)

        result = page.evaluate(f"""
            async () => {{
                const res = await fetch("{url}");
                return await res.text();
            }}
        """)

        page.close()

        if result and not is_cf_blocked(result):
            return Response(result, content_type="application/json")

    except Exception as e:
        print("Fallback error:", e)

    return jsonify({
        "success": False,
        "error": "Cloudflare blocked + fallback failed"
    }), 500

# =========================
# PROXY
# =========================
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
# DOWNLOAD + CONVERT
# =========================
def download_and_convert(url, index):
    for attempt in range(MAX_RETRIES):
        try:
            proxy_url = f"https://kiroflix.site/backend/mangaposterproxy.php?url={url}"
            r = session.get(proxy_url, timeout=15)

            if r.status_code != 200:
                continue

            img = Image.open(BytesIO(r.content)).convert("RGB")
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            img.save(temp_file.name, "JPEG", quality=100, subsampling=0)

            return index, temp_file.name

        except Exception as e:
            print(f"❌ [{index}] Error:", e)

    return index, None

# =========================
# SPLIT IMAGE
# =========================
def split_image_if_needed(image_path):
    MAX_HEIGHT = 2000
    img = Image.open(image_path)
    width, height = img.size

    if height <= MAX_HEIGHT:
        return [image_path]

    parts = []
    y = 0

    while y < height:
        box = (0, y, width, min(y + MAX_HEIGHT, height))
        part = img.crop(box)

        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        part.save(temp_file.name, "JPEG", quality=100, subsampling=0)

        parts.append(temp_file.name)
        y += MAX_HEIGHT

    try:
        os.unlink(image_path)
    except:
        pass

    return parts

# =========================
# PDF WORKER
# =========================
def pdf_worker(job_id, image_urls):
    from reportlab.pdfgen import canvas

    with PDF_SEMAPHORE:
        try:
            total = len(image_urls)

            with jobs_lock:
                jobs[job_id] = {"status": "processing", "progress": 0}

            results = [None] * total
            completed = 0

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = [executor.submit(download_and_convert, url, i) for i, url in enumerate(image_urls)]

                for future in as_completed(futures):
                    index, path = future.result()
                    if path:
                        results[index] = path

                    completed += 1

                    with jobs_lock:
                        jobs[job_id]["progress"] = int((completed / total) * 60)

            images = [p for p in results if p]

            processed_images = []
            for i, img_path in enumerate(images):
                processed_images.extend(split_image_if_needed(img_path))

                with jobs_lock:
                    jobs[job_id]["progress"] = 60 + int((i / len(images)) * 20)

            pdf_path = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf").name
            c = None

            for idx, img_path in enumerate(processed_images):
                img = Image.open(img_path)
                w, h = img.size

                if idx == 0:
                    c = canvas.Canvas(pdf_path, pagesize=(w, h))
                else:
                    c.setPageSize((w, h))
                    c.showPage()

                c.drawInlineImage(img_path, 0, 0, width=w, height=h)

            if c:
                c.save()

            for p in processed_images:
                try: os.unlink(p)
                except: pass

            with jobs_lock:
                jobs[job_id] = {
                    "status": "done",
                    "progress": 100,
                    "file": pdf_path
                }

        except Exception as e:
            print("❌ PDF ERROR:", e)
            with jobs_lock:
                jobs[job_id] = {"status": "error", "error": str(e)}

# =========================
# ROUTES
# =========================
@app.route("/build_pdf_async", methods=["POST"])
def build_pdf_async():
    data = request.json
    image_urls = data.get("images", [])[:120]

    if not image_urls:
        return jsonify({"error": "No images"}), 400

    job_id = str(uuid.uuid4())

    with jobs_lock:
        jobs[job_id] = {"status": "queued", "progress": 0}

    threading.Thread(target=pdf_worker, args=(job_id, image_urls), daemon=True).start()

    return jsonify({"jobId": job_id})

@app.route("/pdf_status")
def pdf_status():
    job_id = request.args.get("jobId")

    if not job_id or job_id not in jobs:
        return jsonify({"error": "Invalid jobId"}), 404

    return jsonify(jobs[job_id])

@app.route("/pdf_download")
def pdf_download():
    job_id = request.args.get("jobId")

    if not job_id or job_id not in jobs:
        return "Invalid jobId", 404

    job = jobs[job_id]

    if job["status"] != "done":
        return "Not ready", 400

    def generate():
        with open(job["file"], "rb") as f:
            while chunk := f.read(8192):
                yield chunk

        os.unlink(job["file"])

        with jobs_lock:
            del jobs[job_id]

    return Response(generate(), content_type="application/pdf")

# =========================
# SEARCH
# =========================
@app.route("/search")
def search():
    query = request.args.get("q", "")

    if not query:
        return jsonify({"error": "No query"}), 400

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
    except:
        results = []

    return jsonify({
        "results": [
            {
                "title": r.get("title"),
                "url": r.get("href"),
                "description": r.get("body")
            }
            for r in results
        ]
    })

@app.route("/")
def home():
    return """
    <h2>Unified API</h2>
    <p>/manga?q=QUERY</p>
    <p>/proxy?url=...</p>
    <p>/build_pdf_async</p>
    """

if __name__ == "__main__":
    start_browser()
    app.run(host="0.0.0.0", port=5000, threaded=True)
