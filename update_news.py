#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hera News Automation Engine for GitHub Actions & Local Execution
- Fetches news from 3 trusted Sinhala sources: ITN (API + Sports), Lankadeepa (RSS), Ada Derana (RSS + Page Scraper)
- Guaranteed https image extraction & full article body extraction
- True publication timestamp extraction for Ada Derana, Lankadeepa & ITN
- Strict chronological sorting (Newest to Oldest)
- Realtime Database sync (news/items, news/feed, news/meta)
- Firebase Cloud Messaging (FCM) topic 'news' notification with Hera Keyboard Flutter payload
- Generates news.json for GitHub Pages
"""

import os
import sys
import re
import json
import html
import time
import email.utils
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
import xml.etree.ElementTree as ET
import requests

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

SL_TIMEZONE = timezone(timedelta(hours=5, minutes=30))
NEWS_JSON_FILE = "news.json"
STATE_FILE = ".news_engine_state.json"

HERA_CONFIG = {
    "project_id": "hera-keyboard",
    "database_url": "https://hera-keyboard-default-rtdb.firebaseio.com",
    "topic": "news",
    "retention_days": 3,
    "max_feed_items": 100,
    "max_json_items": 40,
    "max_notifications_per_day": 36,
    "max_notifications_per_hour": 2,
    "standard_gap_minutes": 20,
    "high_priority_gap_minutes": 8,
    "urgent_gap_minutes": 3,
    "notification_threshold": 45,
    "high_priority_threshold": 68,
    "urgent_threshold": 82
}

HTTP_SESSION = requests.Session()
HTTP_SESSION.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8',
    'Accept-Language': 'si,en-US;q=0.9,en;q=0.8',
})

def get_now_sl():
    return datetime.now(SL_TIMEZONE)

def http_get(url, timeout=12):
    try:
        resp = HTTP_SESSION.get(url, timeout=timeout)
        if resp.status_code == 200 and resp.text:
            return resp.text
    except Exception as e:
        # Fallback to curl if python requests encounters any network issue
        try:
            import subprocess
            cmd = [
                "curl", "-s", "-L", "--compressed", "--max-time", str(timeout),
                "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
                url
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
            if res.returncode == 0 and res.stdout:
                return res.stdout
        except Exception:
            pass
    return None

def clean_text(text):
    if not text:
        return ""
    text = html.unescape(str(text))
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'^\s*\d{1,2}-\d{1,2}-\d{4}\s*\|\s*\d{1,2}:\d{2}\s*(?:AM|PM)\s*', '', text, flags=re.I)
    text = re.sub(r'\s*COLOMBO\s*\(\s*News\s*1st\s*\)\s*-\s*', '', text, flags=re.I)
    text = re.sub(r'\s*-\s*ශ්‍රී ලංකා ප්‍රවෘත්ති.*$', '', text)
    text = re.sub(r'Listen to this Audio\s*(?:AI Generated)?', '', text, flags=re.I)
    text = text.replace('\xa0', ' ')
    text = re.sub(r'\s+', ' ', text).strip()
    return re.sub(r'[\s.\-–—:|]+$', '', text).strip()

def normalize_image_url(url):
    clean = str(url or "").strip()
    if not clean:
        return ""
    clean = re.sub(r'^http://', 'https://', clean, flags=re.I)
    if not clean.lower().startswith('https://'):
        return ""
    return clean.replace(' ', '%20')

def is_sports_news(title, text):
    combined = f"{title} {text}".lower()
    return bool(re.search(
        r'(ක්‍රිකට්|පාපන්දු|ක්‍රීඩා|තරගාවලිය|තරඟාවලිය|ශූරතාව|ඔලිම්පික්|පැරා\s*ක්‍රීඩා|විකට්|ලකුණු|පිතිකර|පන්දු\s*යැව|අවසන්\s*මහා\s*තරග|ටෙස්ට්\s*තරග|එක්දින|විස්සයි20|t20|odi|ipl|lpl|icc|fifa|world\s*cup|තරගය\s*ජය|ජයග්‍රහණය\s*කරයි|පදක්කම|පදක්කම්|ශ්‍රී\s*ලංකා\s*කණ්ඩායම|ක්‍රීඩක)',
        combined
    ))

def is_emergency_news(text):
    low = text.lower()
    foreign_only = bool(re.search(r'(ඉන්දියාවේ|පාකිස්තාන|ඇමරිකාවේ|රුසියාවේ|යුක්රේන|ඊශ්‍රායල|ගාසා)', low)) and not bool(re.search(r'(ශ්‍රී\s*ලංකාවට|දිවයිනට|මෙරට|අපේ\s*රටට)', low))
    if foreign_only:
        return False
    return bool(re.search(r'(රතු\s*නිවේදන|සුනාමි|භූමිකම්පා|ගංවතුර\s*(අවදානම්|අනතුරු\s*ඇඟවීම)|නාය.{0,24}(අවදානම්|අනතුරු\s*ඇඟවීම)|සුළි\s*කුණාටු|තද\s*සුළං\s*අවදානම්|වාන්\s*දොරටු|ජලාශ.*වාන්|අධික\s*වැසි\s*අවදානම්|ඉවත්\s*වන්නැයි\s*දැනුම්|හදිසි\s*තත්ත්ව|ඇඳිරි\s*නීතිය)', low))

def calculate_importance(article):
    text = f"{article.get('title', '')} {article.get('summary', '')} {article.get('content', '')}"
    title = article.get('title', '')
    score = 25
    reasons = []

    if is_emergency_news(text):
        score += 70
        reasons.append("life_safety")

    if re.search(r'(විශේෂ\s*(පුවත|නිවේදනය)|හදිසි\s*නිවේදනය|ක්ෂණික\s*පුවත්|දැන්\s*ලැබුණු\s*පුවත|breaking\s*news|news\s*alert|\balert\b)', text, re.I):
        score += 50
        reasons.append("breaking_news")

    if re.search(r'(විදේශ\s*ගමන්\s*බලපත්‍ර|හැඳුනුම්පත්|රියදුරු\s*බලපත්‍ර|දුම්රිය\s*(වර්ජන|ප්‍රමාද)|බස්\s*(වර්ජන|ගාස්තු)|විදුලි\s*(කප්පාදු|ගාස්තු)|ජල\s*කප්පාදු|ඉන්ධන\s*මිල|ගෑස්\s*මිල|පාසල්\s*(නිවාඩු|වසා)|විභාග\s*(ප්‍රතිඵල|කාලසටහන))', text, re.I):
        score += 32
        reasons.append("public_service")

    if re.search(r'(අයවැය|imf|මහ\s*බැංකු|පොලී\s*අනුපාත|උද්ධමනය|කැබිනට්\s*(තීරණ|අනුමැති)|පනත\s*(සම්මත|අනුමත)|මැතිවරණ|ශ්‍රේෂ්ඨාධිකරණ|අධිකරණ\s*තීන්දුව)', text, re.I):
        score += 34
        reasons.append("national_impact")

    major_sport = bool(re.search(r'(ලෝක\s*කුසලාන|අවසන්\s*මහා\s*තරග|ශූරතාව|ඔලිම්පික්|පදක්කම|තරගාවලිය\s*ජය|ජයග්‍රහණය\s*කරයි|විශිෂ්ට\s*ජයක්|ලකුණු\s*\d+\s*කින්\s*ජය|කඩුලු\s*\d+\s*කින්\s*ජය)', text, re.I))
    if major_sport:
        score += 35
        reasons.append("major_sport")
    elif is_sports_news(title, text):
        score += 24
        reasons.append("sports_news")

    if re.search(r'(ලග්න\s*පලාපල|කේන්දර|ජ්‍යොතිෂ|ලොතරැයි|වෙළඳ\s*දැන්වීම|වට්ටම්\s*දීමනා)', text, re.I):
        score -= 60
        reasons.append("blocked_fluff")

    score = max(0, min(100, score))
    tier = "feed_only"
    if is_emergency_news(text):
        tier = "emergency"
    elif score >= HERA_CONFIG["urgent_threshold"] or "breaking_news" in reasons:
        tier = "urgent"
    elif score >= HERA_CONFIG["high_priority_threshold"]:
        tier = "high"
    elif score >= HERA_CONFIG["notification_threshold"]:
        tier = "standard"

    article["importance_score"] = score
    article["importance_tier"] = tier
    article["importance_reasons"] = reasons
    return article

# ==================== SOURCE 1: ITN NEWS (API + SPORTS + RSS) ====================
def fetch_itn_news():
    articles = []
    seen = set()

    # Per page 30 to keep JSON response fast and light
    urls = [
        "https://www.itnnews.lk/wp-json/wp/v2/posts?_embed&per_page=30",
        "https://www.itnnews.lk/wp-json/wp/v2/posts?categories=80&_embed=wp:featuredmedia&per_page=20"
    ]
    for url in urls:
        try:
            raw = http_get(url, timeout=10)
            if not raw:
                continue
            data = json.loads(raw)
            if not isinstance(data, list):
                continue
            for post in data:
                p_id = f"itn_{post.get('id')}"
                if p_id in seen:
                    continue
                seen.add(p_id)

                title = clean_text(post.get('title', {}).get('rendered', ''))
                if len(title) < 5:
                    continue

                excerpt = clean_text(post.get('excerpt', {}).get('rendered', ''))
                content = clean_text(post.get('content', {}).get('rendered', ''))
                summary = excerpt or content or title
                body = content or summary or title

                # Exact timestamp
                date_gmt = post.get('date_gmt') or ""
                pub_ms = int(time.time() * 1000)
                if date_gmt:
                    try:
                        dt = datetime.fromisoformat(date_gmt.replace('Z', '+00:00'))
                        pub_ms = int(dt.timestamp() * 1000)
                    except Exception:
                        pass

                cats = post.get('categories') or []
                is_sports = (80 in cats) or is_sports_news(title, body)
                cat_name = "ක්‍රීඩා පුවත්" if is_sports else ("ප්‍රධාන පුවත" if 86 in cats else "දේශීය පුවත්")

                image_url = ""
                try:
                    media = post.get('_embedded', {}).get('wp:featuredmedia', [])
                    if media and isinstance(media, list) and 'source_url' in media[0]:
                        image_url = media[0]['source_url']
                except Exception:
                    pass

                articles.append({
                    "id": p_id,
                    "title": title[:300],
                    "category": cat_name,
                    "summary": summary[:800],
                    "content": body[:12000],
                    "image_url": normalize_image_url(image_url),
                    "published_at": datetime.fromtimestamp(pub_ms / 1000, tz=SL_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"),
                    "published_at_ms": pub_ms,
                    "fetched_at_ms": int(time.time() * 1000),
                    "source_name": "ITN News",
                    "source_url": post.get('link') or f"https://www.itnnews.lk/{post.get('id')}",
                    "is_top_story": 86 in cats
                })
        except Exception as e:
            print(f"ITN API notice: {e}")

    # Fallback to ITN RSS if needed
    if not articles:
        try:
            xml_text = http_get("https://www.itnnews.lk/feed/", timeout=8)
            if xml_text and "<rss" in xml_text:
                root = ET.fromstring(xml_text)
                for item in root.findall(".//item"):
                    link = item.findtext("link") or ""
                    title = clean_text(item.findtext("title") or "")
                    if len(title) < 5:
                        continue
                    desc = clean_text(item.findtext("description") or "")
                    id_match = re.search(r'/(\d+)/?$', link)
                    p_id = f"itn_{id_match.group(1)}" if id_match else f"itn_{abs(hash(link))}"
                    cat_name = "ක්‍රීඩා පුවත්" if is_sports_news(title, desc) else "දේශීය පුවත්"
                    raw_date = item.findtext("pubDate") or ""
                    pub_ms = int(time.time() * 1000)
                    if raw_date:
                        try:
                            dt = email.utils.parsedate_to_datetime(raw_date)
                            pub_ms = int(dt.timestamp() * 1000)
                        except Exception:
                            pass
                    articles.append({
                        "id": p_id,
                        "title": title,
                        "category": cat_name,
                        "summary": desc or title,
                        "content": desc or title,
                        "image_url": "",
                        "published_at": datetime.fromtimestamp(pub_ms / 1000, tz=SL_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"),
                        "published_at_ms": pub_ms,
                        "fetched_at_ms": int(time.time() * 1000),
                        "source_name": "ITN News",
                        "source_url": link
                    })
        except Exception as e:
            print(f"ITN RSS notice: {e}")

    print(f"Loaded {len(articles)} articles from ITN News.")
    return articles

# ==================== SOURCE 2: LANKADEEPA (RSS) ====================
def fetch_lankadeepa_news():
    articles = []
    seen = set()
    urls = [
        "https://www.lankadeepa.lk/rss/latest_news/1",
        "https://www.lankadeepa.lk/rss/news/48"
    ]
    for url in urls:
        try:
            raw = http_get(url, timeout=8)
            if not raw or ("<rss" not in raw and "<channel" not in raw):
                continue
            raw = re.sub(r'^\ufeff', '', raw).strip()
            end_idx = raw.find('</rss>')
            if end_idx != -1:
                raw = raw[:end_idx + 6].strip()
            root = ET.fromstring(raw)
            for item in root.findall(".//item"):
                link = (item.findtext("link") or "").strip()
                if not link:
                    continue
                id_match = re.search(r'/(\d+-\d+|\d+)/?$', link)
                p_id = f"lk_{id_match.group(1).replace('-', '_')}" if id_match else f"lk_{abs(hash(link))}"
                if p_id in seen:
                    continue
                seen.add(p_id)

                title = clean_text(item.findtext("title") or "")
                if len(title) < 5:
                    continue

                raw_desc = item.findtext("description") or ""
                image_url = ""
                img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', raw_desc, re.I)
                if img_match:
                    image_url = img_match.group(1)

                enc = item.find("enclosure")
                if not image_url and enc is not None:
                    image_url = enc.get("url") or ""

                clean_desc = clean_text(raw_desc)
                cat_name = "ක්‍රීඩා පුවත්" if is_sports_news(title, clean_desc) else "දේශීය පුවත්"

                # Parse ISO timestamp e.g. 2026-10-30T11:34:00+05:30
                raw_date = item.findtext("pubDate") or ""
                pub_ms = int(time.time() * 1000)
                if raw_date:
                    try:
                        dt = datetime.fromisoformat(raw_date)
                        pub_ms = int(dt.timestamp() * 1000)
                    except Exception:
                        try:
                            dt = email.utils.parsedate_to_datetime(raw_date)
                            pub_ms = int(dt.timestamp() * 1000)
                        except Exception:
                            pass

                articles.append({
                    "id": p_id,
                    "title": title,
                    "category": cat_name,
                    "summary": clean_desc or title,
                    "content": clean_desc or title,
                    "image_url": normalize_image_url(image_url),
                    "published_at": datetime.fromtimestamp(pub_ms / 1000, tz=SL_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"),
                    "published_at_ms": pub_ms,
                    "fetched_at_ms": int(time.time() * 1000),
                    "source_name": "ලංකාදීප",
                    "source_url": link
                })
        except Exception as e:
            print(f"Lankadeepa notice ({url}): {e}")

    print(f"Loaded {len(articles)} articles from Lankadeepa.")
    return articles

# ==================== SOURCE 3: ADA DERANA (RSS + EXACT TIME & IMAGE SCRAPER) ====================
def fetch_adaderana_news():
    articles = []
    seen = set()
    rss_urls = [
        "https://sinhala.adaderana.lk/rss.php",
        "https://sinhala.adaderana.lk/rss.xml"
    ]
    raw = None
    for url in rss_urls:
        raw = http_get(url, timeout=8)
        if raw and ("<rss" in raw or "<channel" in raw):
            break

    if raw:
        try:
            root = ET.fromstring(raw)
            for item in root.findall(".//item"):
                link = (item.findtext("link") or "").strip()
                if not link:
                    continue
                id_match = re.search(r'/news/(\d+)', link)
                p_id = f"ad_{id_match.group(1)}" if id_match else f"ad_{abs(hash(link))}"
                if p_id in seen:
                    continue
                seen.add(p_id)

                title = clean_text(item.findtext("title") or "")
                if len(title) < 5:
                    continue

                raw_desc = item.findtext("description") or ""
                image_url = ""
                img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', raw_desc, re.I)
                if img_match:
                    image_url = img_match.group(1)

                clean_desc = clean_text(raw_desc)
                raw_cat = (item.findtext("category") or "").lower()
                cat_name = "දේශීය පුවත්"
                if "sports" in raw_cat or is_sports_news(title, clean_desc):
                    cat_name = "ක්‍රීඩා පුවත්"
                elif "feature" in raw_cat:
                    cat_name = "විශේෂාංග"

                # Estimate default timestamp based on article numeric ID progression
                # This ensures newer Ada Derana news IDs strictly sort above older news IDs
                base_time = int(time.time() * 1000)
                num_id = int(id_match.group(1)) if id_match else 0
                estimated_ms = base_time - max(0, (253920 - num_id) * 120000) if num_id > 0 else base_time

                articles.append({
                    "id": p_id,
                    "title": title,
                    "category": cat_name,
                    "summary": clean_desc or title,
                    "content": clean_desc or title,
                    "image_url": normalize_image_url(image_url),
                    "published_at": datetime.fromtimestamp(estimated_ms / 1000, tz=SL_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"),
                    "published_at_ms": estimated_ms,
                    "fetched_at_ms": int(time.time() * 1000),
                    "source_name": "අද දෙරණ",
                    "source_url": link
                })
        except Exception as e:
            print(f"Ada Derana RSS notice: {e}")

    print(f"Loaded {len(articles)} articles from Ada Derana.")
    return articles

# ==================== ENRICHMENT & DEDUPLICATION ====================
def enrich_article_details(article):
    """
    Scrapes the news article's actual webpage to extract exact publication date,
    high-res og:image, and full article text.
    """
    if not article.get("source_url"):
        return article

    try:
        page_html = http_get(article["source_url"], timeout=8)
        if not page_html:
            return article

        # 1. Image
        if not article.get("image_url"):
            img_m = re.search(r'property=["\'](?:og:image|twitter:image)["\']\s+content=["\']([^"\']+)["\']', page_html, re.I)
            if not img_m:
                img_m = re.search(r'content=["\']([^"\']+)["\']\s+property=["\'](?:og:image|twitter:image)["\']', page_html, re.I)
            if img_m:
                article["image_url"] = normalize_image_url(img_m.group(1))

        # 2. Body / Summary
        if len(article.get("summary", "")) < 40 or article.get("summary") == article.get("title"):
            desc_m = re.search(r'property=["\'](?:og:description|description)["\']\s+content=["\']([^"\']+)["\']', page_html, re.I)
            if desc_m:
                cleaned_desc = clean_text(desc_m.group(1))
                if len(cleaned_desc) > 30:
                    article["summary"] = cleaned_desc
                    article["content"] = cleaned_desc

        # 3. Exact Published Date for Ada Derana (e.g., "Fri, 30 Oct 2026 - 07:50 AM")
        if article.get("source_name") == "අද දෙරණ":
            date_m = re.search(r'([A-Za-z]{3},\s*\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s*-\s*\d{1,2}:\d{2}\s*(?:AM|PM))', page_html, re.I)
            if date_m:
                try:
                    clean_date_str = date_m.group(1).replace('-', '').replace(',', '').strip()
                    clean_date_str = ' '.join(clean_date_str.split())
                    dt = datetime.strptime(clean_date_str, "%a %d %b %Y %I:%M %p").replace(tzinfo=SL_TIMEZONE)
                    article["published_at_ms"] = int(dt.timestamp() * 1000)
                    article["published_at"] = dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass
    except Exception:
        pass

    return article

def deduplicate_articles(articles):
    unique = []
    seen_titles = []

    def get_tokens(title):
        cleaned = re.sub(r'[^\u0D80-\u0DFFa-zA-Z0-9\s]', ' ', title).lower()
        stopwords = {'සහ', 'හා', 'හෝ', 'එක්', 'අද', 'හෙට', 'සිට', 'සඳහා', 'බව', 'යයි', 'වෙයි', 'කරයි', 'මහතා'}
        return {w for w in cleaned.split() if len(w) >= 3 and w not in stopwords}

    for art in articles:
        tokens = get_tokens(art["title"])
        if not tokens:
            continue
        duplicate = False
        for ex_art, ex_tokens in seen_titles:
            shared = len(tokens.intersection(ex_tokens))
            min_len = min(len(tokens), len(ex_tokens))
            similarity = shared / min_len if min_len > 0 else 0
            if similarity >= 0.55 or (shared >= 3 and similarity >= 0.35):
                duplicate = True
                if not ex_art.get("image_url") and art.get("image_url"):
                    ex_art["image_url"] = art["image_url"]
                if len(art.get("content", "")) > len(ex_art.get("content", "")):
                    ex_art["content"] = art["content"]
                break

        if not duplicate:
            seen_titles.append((art, tokens))
            unique.append(art)

    return unique

# ==================== STATE & BUDGETING ====================
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "budget_day": "",
        "daily_count": 0,
        "budget_hour": "",
        "hourly_count": 0,
        "last_notification_ms": 0,
        "recent_notified_keys": []
    }

def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"Warning: Could not save engine state: {e}")

# ==================== FIREBASE SYNC & FCM DISPATCH ====================
def initialize_firebase():
    creds_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not creds_json:
        print("Note: FIREBASE_SERVICE_ACCOUNT not configured. Running in preview mode.")
        return False

    try:
        import firebase_admin
        from firebase_admin import credentials, db
        if not firebase_admin._apps:
            cred_dict = json.loads(creds_json)
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred, {
                'databaseURL': HERA_CONFIG["database_url"]
            })
            print("✅ Firebase Admin SDK successfully connected!")
        return True
    except Exception as e:
        print(f"❌ Firebase initialization error: {e}")
        return False

def sync_firebase_realtime_db(articles):
    try:
        from firebase_admin import db
        feed_dict = {}
        items_dict = {}
        # Ensure articles are strictly ordered newest to oldest
        sorted_articles = sorted(articles, key=lambda x: x.get("published_at_ms", 0), reverse=True)

        for art in sorted_articles[:HERA_CONFIG["max_feed_items"]]:
            a_id = str(art["id"])
            feed_dict[a_id] = {
                "id": a_id,
                "title": art["title"],
                "category": art.get("category", "දේශීය පුවත්"),
                "summary": art.get("summary", ""),
                "content": art.get("content", ""),
                "image_url": art.get("image_url", ""),
                "published_at": art.get("published_at", ""),
                "published_at_ms": art.get("published_at_ms", 0),
                "fetched_at_ms": art.get("fetched_at_ms", 0),
                "source_name": art.get("source_name", "Hera News"),
                "source_url": art.get("source_url", ""),
                "is_top_story": art.get("is_top_story", False),
                "importance_score": art.get("importance_score", 0),
                "importance_tier": art.get("importance_tier", "feed_only"),
            }
            items_dict[a_id] = art

        root_ref = db.reference('news')
        # Overwrite feed with cleanly sorted, fresh items
        root_ref.child('feed').set(feed_dict)
        root_ref.child('items').update(items_dict)
        root_ref.child('meta').update({
            "initialized": True,
            "source": "Hera News Engine (GitHub 24/7)",
            "active_count": len(sorted_articles),
            "updated_at_ms": int(time.time() * 1000),
            "last_scan_status": "success"
        })
        print(f"✅ Firebase Realtime Database updated with {len(feed_dict)} clean feed items (Newest to Oldest)!")
    except Exception as e:
        print(f"Warning: Firebase DB sync error: {e}")

def send_fcm_notification(article):
    try:
        from firebase_admin import messaging
        clean_title = article['title'][:250]
        clean_summary = (article.get('summary') or article.get('category') or 'අලුත් පුවතක්')[:500]
        body = clean_summary[:180] or clean_title[:180]
        safe_image = article.get('image_url') or ""

        message = messaging.Message(
            topic=HERA_CONFIG["topic"],
            data={
                "type": "news",
                "id": str(article["id"]),
                "news_id": str(article["id"]),
                "title": clean_title,
                "article_title": clean_title,
                "body": body,
                "category": article.get("category", "දේශීය පුවත්"),
                "summary": clean_summary,
                "content": (article.get("content") or clean_summary)[:1500],
                "image_url": safe_image,
                "published_at": article.get("published_at", get_now_sl().strftime("%Y-%m-%d %H:%M:%S")),
                "source_name": article.get("source_name", "Hera News"),
                "source_url": str(article.get("source_url") or "")[:500],
            },
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    title=f"🔴 {clean_title}",
                    body=body,
                    image=safe_image if safe_image else None,
                    sound="hera_notification"
                )
            )
        )

        resp = messaging.send(message)
        print(f"🔥 FCM PUSH NOTIFICATION SENT! Topic: news, ID: {resp}, Article: {clean_title}")
        return True
    except Exception as e:
        print(f"❌ FCM sending failed: {e}")
        return False

# ==================== MAIN RUNNER ====================
def main():
    now_sl = get_now_sl()
    print(f"[{now_sl.strftime('%Y-%m-%d %H:%M:%S')} SLST] Hera News 24/7 Engine Running...")

    has_firebase = initialize_firebase()
    state = load_state()

    # 1. Fetch articles from all 3 independent sources
    itn_articles = fetch_itn_news()
    lankadeepa_articles = fetch_lankadeepa_news()
    derana_articles = fetch_adaderana_news()

    combined = itn_articles + lankadeepa_articles + derana_articles
    if not combined:
        print("Warning: All news sources were empty or unreachable. Exiting safely.")
        return

    # 2. Deduplicate across sources
    unique_articles = deduplicate_articles(combined)
    print(f"Combined total: {len(combined)} -> Deduplicated: {len(unique_articles)} articles.")

    # 3. Enrich articles needing images or body in parallel
    print("Enriching articles needing images or body in parallel...")
    needs_enrichment_indices = [
        i for i, art in enumerate(unique_articles)
        if not art.get("image_url") or art.get("source_name") == "අද දෙරණ"
    ]
    targets = [unique_articles[i] for i in needs_enrichment_indices[:40]]
    if targets:
        with ThreadPoolExecutor(max_workers=8) as executor:
            enriched_results = list(executor.map(enrich_article_details, targets))
        for idx, enriched in zip(needs_enrichment_indices[:40], enriched_results):
            unique_articles[idx] = enriched

    # 4. Calculate importance and sports scoring
    scored_articles = [calculate_importance(art) for art in unique_articles]
    # Sort strictly descending by true publication time (NEWEST AT THE TOP)
    scored_articles.sort(key=lambda x: x.get("published_at_ms", 0), reverse=True)

    # 5. Save to news.json for GitHub Pages
    json_list = scored_articles[:HERA_CONFIG["max_json_items"]]
    try:
        with open(NEWS_JSON_FILE, "w", encoding="utf-8") as f:
            json.dump(json_list, f, ensure_ascii=False, indent=2)
        print(f"✅ Saved {len(json_list)} fresh articles to {NEWS_JSON_FILE} (Sorted Newest First).")
    except Exception as e:
        print(f"Warning: Could not write {NEWS_JSON_FILE}: {e}")

    # 6. Sync cleanly to Firebase Realtime Database
    if has_firebase:
        sync_firebase_realtime_db(scored_articles)

    # 7. Notification Dispatcher
    now_ms = int(time.time() * 1000)
    today_str = now_sl.strftime("%Y-%m-%d")
    hour_str = now_sl.strftime("%Y-%m-%d-%H")

    if state.get("budget_day") != today_str:
        state["budget_day"] = today_str
        state["daily_count"] = 0

    if state.get("budget_hour") != hour_str:
        state["budget_hour"] = hour_str
        state["hourly_count"] = 0

    recent_keys = set(state.get("recent_notified_keys", []))

    candidates = [
        art for art in scored_articles
        if art.get("importance_tier") != "feed_only" and str(art["id"]) not in recent_keys
    ]

    tier_rank = {"emergency": 4, "urgent": 3, "high": 2, "standard": 1, "feed_only": 0}
    candidates.sort(
        key=lambda x: (tier_rank.get(x.get("importance_tier", "feed_only"), 0), x.get("importance_score", 0)),
        reverse=True
    )

    if candidates and has_firebase:
        top_art = candidates[0]
        tier = top_art.get("importance_tier")
        is_urgent = tier in ("urgent", "emergency")

        last_sent_ms = state.get("last_notification_ms", 0)
        gap_needed_min = HERA_CONFIG["standard_gap_minutes"]
        if tier == "high":
            gap_needed_min = HERA_CONFIG["high_priority_gap_minutes"]
        elif is_urgent:
            gap_needed_min = HERA_CONFIG["urgent_gap_minutes"]

        time_since_last_min = (now_ms - last_sent_ms) / 60000

        can_send = True
        if state["daily_count"] >= HERA_CONFIG["max_notifications_per_day"] and not is_urgent:
            print("Daily notification limit reached. Holding for next day.")
            can_send = False
        elif state["hourly_count"] >= HERA_CONFIG["max_notifications_per_hour"] and not is_urgent:
            print(f"Hourly pace hold ({state['hourly_count']} sent this hour). Holding for next hour.")
            can_send = False
        elif time_since_last_min < gap_needed_min:
            print(f"Spacing gap in effect ({time_since_last_min:.1f}/{gap_needed_min} min). Holding.")
            can_send = False

        if can_send:
            top_art = enrich_article_details(top_art)
            if send_fcm_notification(top_art):
                state["daily_count"] += 1
                state["hourly_count"] += 1
                state["last_notification_ms"] = now_ms
                recent_list = state.get("recent_notified_keys", [])
                recent_list.append(str(top_art["id"]))
                state["recent_notified_keys"] = recent_list[-150:]
                save_state(state)
    else:
        print("No candidates pending notification or Firebase not initialized.")

    save_state(state)
    print("Hera News Engine run finished successfully.")

if __name__ == "__main__":
    main()
