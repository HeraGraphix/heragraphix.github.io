import os
import sys
import re
import json
import html
import subprocess
from datetime import datetime, timezone, timedelta
import xml.etree.ElementTree as ET
import requests

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# Sri Lanka Timezone (UTC + 05:30)
SL_TIMEZONE = timezone(timedelta(hours=5, minutes=30))

PRIMARY_RSS_URL = "https://sinhala.adaderana.lk/rss.php"
FALLBACK_RSS_URL = "https://sinhala.adaderana.lk/rss.xml"
NEWS_JSON_FILE = "news.json"

MAX_AGE_DAYS = 3
MAX_ITEMS_TO_KEEP = 30

def get_now_sl():
    return datetime.now(SL_TIMEZONE)

def parse_published_at(date_str):
    if not date_str:
        return None
    try:
        dt = datetime.strptime(date_str.strip(), "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=SL_TIMEZONE)
    except Exception:
        return None

def clean_text(text):
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'^Share\s*', '', text)
    text = re.sub(r'Listen to this Audio\s*(?:AI Generated)?', '', text, flags=re.I)
    text = text.replace('\xa0', ' ').strip()
    return text

def http_get(url):
    """
    Robust HTTP GET that works inside GitHub Actions runners.
    Tries curl command line first (bypasses Python TLS fingerprint blocking),
    then falls back to requests.
    """
    # 1. Try system curl (100% works on GitHub Actions Ubuntu runners)
    try:
        cmd = [
            "curl", "-s", "-L",
            "--compressed",
            "--max-time", "15",
            "-H", "User-Agent: curl/7.88.1",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            url
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
        if result.returncode == 0 and result.stdout and len(result.stdout) > 50:
            return result.stdout
    except Exception as e:
        print(f"curl attempt failed for {url}: {e}")

    # 2. Fallback to requests
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36',
            'Accept': '*/*'
        }
        resp = requests.get(url, headers=headers, timeout=12)
        if resp.status_code == 200 and resp.text:
            return resp.text
    except Exception as e:
        print(f"requests attempt failed for {url}: {e}")

    return None

def fetch_rss_feed():
    for url in [PRIMARY_RSS_URL, FALLBACK_RSS_URL]:
        print(f"Fetching RSS feed from: {url}...")
        content = http_get(url)
        if content and ("<rss" in content or "<channel" in content):
            print("--> RSS Feed successfully downloaded!")
            return content
        else:
            print(f"Warning: Could not get valid RSS XML from {url}")
    return None

def load_existing_news():
    if os.path.exists(NEWS_JSON_FILE):
        try:
            with open(NEWS_JSON_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception as e:
            print(f"Warning: Could not read {NEWS_JSON_FILE}: {e}")
    return []

def purge_old_news(articles):
    now = get_now_sl()
    cutoff_time = now - timedelta(days=MAX_AGE_DAYS)
    fresh_articles = []

    for item in articles:
        item_id = str(item.get("id", "")).strip()
        title = item.get("title", "").strip()
        if not item_id or not item_id.isdigit() or len(title) < 5:
            continue

        pub_dt = parse_published_at(item.get("published_at"))
        if pub_dt is None:
            pub_dt = now
            item["published_at"] = now.strftime("%Y-%m-%d %H:%M:%S")

        if pub_dt >= cutoff_time:
            fresh_articles.append(item)

    fresh_articles.sort(
        key=lambda x: int(x["id"]) if str(x.get("id", "")).isdigit() else 0,
        reverse=True
    )
    return fresh_articles[:MAX_ITEMS_TO_KEEP]

def send_push_notification(article):
    firebase_creds_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not firebase_creds_json:
        print("Note: FIREBASE_SERVICE_ACCOUNT secret not found. Skipping push notification.")
        return

    try:
        import firebase_admin
        from firebase_admin import credentials, messaging

        if not firebase_admin._apps:
            cred_dict = json.loads(firebase_creds_json)
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred)

        body_text = article.get("summary") or article.get("title", "")
        if len(body_text) > 120:
            body_text = body_text[:117] + "..."

        notification = messaging.Notification(
            title=f"🔴 {article['title']}",
            body=body_text,
            image=article.get("image_url") or None
        )

        message = messaging.Message(
            notification=notification,
            data={
                "news_id": str(article["id"]),
                "category": article.get("category", "දේශීය පුවත්"),
                "click_action": "FLUTTER_NOTIFICATION_CLICK"
            },
            topic="news"
        )

        response = messaging.send(message)
        print(f"🔥 FCM PUSH NOTIFICATION SENT SUCCESSFULLY! ID: {response}")

    except Exception as e:
        print(f"Error sending Firebase notification: {e}")

def run():
    now_sl = get_now_sl()
    print(f"[{now_sl.strftime('%Y-%m-%d %H:%M:%S')} SLST] Starting News Auto-Update...")

    existing_raw = load_existing_news()
    existing_news = purge_old_news(existing_raw)
    existing_ids = {str(item["id"]) for item in existing_news}
    print(f"Loaded {len(existing_news)} active news items from {NEWS_JSON_FILE}.")

    rss_xml = fetch_rss_feed()
    if not rss_xml:
        print("Error: Could not retrieve live RSS feed. Exiting.")
        return

    try:
        root = ET.fromstring(rss_xml)
    except Exception as e:
        print(f"Error parsing RSS XML: {e}")
        return

    channel = root.find("channel")
    if channel is None:
        print("Error: Invalid RSS feed format (missing channel).")
        return

    items = channel.findall("item")
    print(f"Total items in RSS feed: {len(items)}")

    category_map = {
        'hot-news': 'දේශීය පුවත්',
        'international-news': 'විදේශීය පුවත්',
        'sports-news': 'ක්‍රීඩා පුවත්',
        'feature-news': 'විශේෂාංග',
        'other-news': 'වෙනත් පුවත්'
    }

    newly_added = []

    for item in items:
        link_elem = item.find("link")
        link = link_elem.text.strip() if link_elem is not None and link_elem.text else ""
        id_match = re.search(r'/news/(\d+)', link)
        news_id = id_match.group(1) if id_match else ""

        if not news_id or news_id in existing_ids:
            continue

        title_elem = item.find("title")
        title = clean_text(title_elem.text) if title_elem is not None and title_elem.text else ""
        if not title:
            continue

        cat_elem = item.find("category")
        raw_cat = cat_elem.text.strip() if cat_elem is not None and cat_elem.text else "hot-news"
        category = category_map.get(raw_cat, 'දේශීය පුවත්')

        print(f"==> NEW ARTICLE FOUND: [{news_id}] {title}")
        image_url = ""
        summary = ""
        full_paragraphs = []

        try:
            article_html = http_get(link)
            if article_html:
                img_match = re.search(r'property=["\']og:image["\'] content=["\']([^"\']+)["\']', article_html)
                if img_match:
                    image_url = img_match.group(1).strip()

                desc_match = re.search(r'property=["\']og:description["\'] content=["\']([^"\']+)["\']', article_html)
                if desc_match:
                    summary = clean_text(desc_match.group(1))

                article_match = re.search(r'<article[^>]*>([\s\S]*?)</article>', article_html)
                if article_match:
                    p_tags = re.findall(r'<p[^>]*>([\s\S]*?)</p>', article_match.group(1))
                    for p in p_tags:
                        cleaned_p = clean_text(p)
                        if len(cleaned_p) > 10 and not cleaned_p.lower().startswith('add ada derana'):
                            full_paragraphs.append(cleaned_p)
        except Exception as e:
            print(f"Error parsing article {news_id}: {e}")

        full_content = "\n\n".join(full_paragraphs)
        if not summary and full_paragraphs:
            summary = full_paragraphs[0]
        if not full_content:
            full_content = summary if summary else title

        article_obj = {
            "id": news_id,
            "title": title,
            "category": category,
            "summary": summary,
            "content": full_content,
            "image_url": image_url,
            "published_at": get_now_sl().strftime("%Y-%m-%d %H:%M:%S"),
            "source_name": "Ada Derana",
            "source_url": link
        }

        newly_added.append(article_obj)
        existing_ids.add(news_id)

    combined_list = newly_added + existing_news
    final_list = purge_old_news(combined_list)

    if newly_added or len(final_list) != len(existing_raw):
        print(f"Writing {len(final_list)} articles into {NEWS_JSON_FILE} (Added: {len(newly_added)})...")
        with open(NEWS_JSON_FILE, "w", encoding="utf-8") as f:
            json.dump(final_list, f, ensure_ascii=False, indent=2)

        print(f"Successfully saved {len(final_list)} total articles into {NEWS_JSON_FILE}.")

        # Send push notification for the newest breaking story!
        if newly_added:
            latest_article = newly_added[0]
            send_push_notification(latest_article)
    else:
        print("No changes. news.json is fresh and up-to-date.")

if __name__ == "__main__":
    run()
