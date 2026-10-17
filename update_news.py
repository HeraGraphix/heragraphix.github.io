import os
import sys
import re
import json
import html
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

RSS_URL = "http://sinhala.adaderana.lk/rss.php"
NEWS_JSON_FILE = "news.json"
MAX_ITEMS_TO_KEEP = 50

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

def clean_text(text):
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'^Share\s*', '', text)
    text = text.replace('\xa0', ' ').strip()
    return text

def fetch_url(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode('utf-8', errors='ignore')

def load_existing_news():
    if os.path.exists(NEWS_JSON_FILE):
        try:
            with open(NEWS_JSON_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Could not read {NEWS_JSON_FILE}: {e}")
    return []

def send_push_notification(article):
    """
    Sends Firebase FCM notification using GitHub Secret: FIREBASE_SERVICE_ACCOUNT
    If not provided, skips gracefully without error.
    """
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

        # Truncate summary if too long for notification drawer
        body_text = article["summary"]
        if len(body_text) > 120:
            body_text = body_text[:117] + "..."

        # Build FCM notification
        notification = messaging.Notification(
            title=f"🔴 {article['title']}",
            body=body_text,
            image=article.get("image_url") or None
        )

        message = messaging.Message(
            notification=notification,
            data={
                "news_id": str(article["id"]),
                "category": article["category"],
                "click_action": "FLUTTER_NOTIFICATION_CLICK"
            },
            topic="news"  # Clients subscribe to topic 'news'
        )

        response = messaging.send(message)
        print(f"Push Notification sent successfully! Response ID: {response}")

    except Exception as e:
        print(f"Error sending Firebase notification: {e}")

def run():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting News Auto-Update...")
    
    existing_news = load_existing_news()
    existing_ids = {str(item["id"]) for item in existing_news}
    print(f"Found {len(existing_news)} existing news items in {NEWS_JSON_FILE}.")

    print("Fetching live RSS feed...")
    try:
        rss_xml = fetch_url(RSS_URL)
        root = ET.fromstring(rss_xml)
    except Exception as e:
        print(f"Error fetching RSS: {e}")
        return

    channel = root.find("channel")
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

    # Process items from newest to oldest
    for item in items:
        link = item.find("link").text.strip() if item.find("link") is not None else ""
        id_match = re.search(r'/news/(\d+)', link)
        news_id = id_match.group(1) if id_match else ""

        if not news_id or news_id in existing_ids:
            continue  # Already exists, skip!

        title = clean_text(item.find("title").text) if item.find("title") is not None else ""
        raw_cat = item.find("category").text.strip() if item.find("category") is not None else "hot-news"
        category = category_map.get(raw_cat, 'දේශීය පුවත්')

        print(f"==> NEW ARTICLE FOUND: [{news_id}] {title}")
        image_url = ""
        summary = ""
        full_paragraphs = []

        try:
            article_html = fetch_url(link)

            # 1. Image
            img_match = re.search(r'property=["\']og:image["\'] content=["\']([^"\']+)["\']', article_html)
            if img_match:
                image_url = img_match.group(1).strip()

            # 2. Summary
            desc_match = re.search(r'property=["\']og:description["\'] content=["\']([^"\']+)["\']', article_html)
            if desc_match:
                summary = clean_text(desc_match.group(1))

            # 3. Clean article body from <article>
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
            "published_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source_name": "Ada Derana",
            "source_url": link
        }

        newly_added.append(article_obj)
        existing_ids.add(news_id)

    if newly_added:
        print(f"Adding {len(newly_added)} new article(s) to {NEWS_JSON_FILE}...")
        updated_list = newly_added + existing_news
        # Keep latest MAX_ITEMS_TO_KEEP
        updated_list = updated_list[:MAX_ITEMS_TO_KEEP]

        with open(NEWS_JSON_FILE, "w", encoding="utf-8") as f:
            json.dump(updated_list, f, ensure_ascii=False, indent=2)

        print(f"Successfully saved {len(updated_list)} total articles into {NEWS_JSON_FILE}.")

        # Send push notification for the newest breaking news item
        latest_article = newly_added[0]
        send_push_notification(latest_article)
    else:
        print("No new articles at this moment. news.json is up to date.")

if __name__ == "__main__":
    run()
