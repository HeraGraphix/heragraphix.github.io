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
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

SL_TIMEZONE = timezone(timedelta(hours=5, minutes=30))
NEWS_JSON_FILE = "news.json"
STATE_FILE = ".news_engine_state.json"

HERA_CONFIG = {
    "project_id": "hera-keyboard",
    "database_url": "https://hera-keyboard-default-rtdb.firebaseio.com",
    "topic": "news",
    "retention_hours": 72,
    "max_feed_items": 100,
    "max_json_items": 100,
    "max_notifications_per_day": 12,
    "max_notifications_per_hour": 2,
    "minimum_scan_gap_minutes": 7,
    "high_priority_gap_minutes": 20,
    "urgent_gap_minutes": 3,
    "high_priority_threshold": 55,
    "urgent_threshold": 80
}

HTTP_SESSION = requests.Session()
HTTP_SESSION.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8',
    'Accept-Language': 'si,en-US;q=0.9,en;q=0.8',
})

def get_now_sl():
    return datetime.now(SL_TIMEZONE)

def http_get(url, timeout=20):
    # 1. Primary: curl_cffi with Chrome browser TLS impersonation (bypasses 403 on cloud runners)
    try:
        from curl_cffi import requests as cffi_requests
        resp = cffi_requests.get(
            url,
            impersonate='chrome124',
            timeout=timeout,
            verify=True,
            headers={'Accept-Language': 'si,en-US;q=0.9,en;q=0.8'}
        )
        if resp.status_code == 200 and resp.text:
            return resp.text
        else:
            print(f"cffi notice: GET {url} status {resp.status_code}")
    except Exception as e:
        print(f"cffi notice: GET {url} error: {e}")

    # 2. Fallback: Standard requests Session
    try:
        resp = HTTP_SESSION.get(url, timeout=timeout)
        if resp.status_code == 200 and resp.text:
            return resp.text
    except Exception:
        pass

    # 3. Fallback: curl CLI
    try:
        import subprocess
        cmd = ['curl', '-s', '-L', '--compressed', '--max-time', str(timeout), url]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
        if res.returncode == 0 and res.stdout and len(res.stdout.strip()) > 20:
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

def title_tokens(title):
    cleaned = re.sub(r'[^\u0D80-\u0DFFa-zA-Z0-9\s]', ' ', str(title or '')).lower()
    stopwords = {
        'සහ', 'හා', 'හෝ', 'එක්', 'අද', 'හෙට', 'සිට', 'සඳහා', 'බව', 'යයි',
        'වෙයි', 'කරයි', 'මහතා', 'වෙත', 'ගැන', 'ලෙස', 'යන', 'මෙම'
    }
    return {word for word in cleaned.split() if len(word) >= 3 and word not in stopwords}

def token_similarity(left, right):
    if not left or not right:
        return 0.0
    shared = len(left.intersection(right))
    return shared / min(len(left), len(right))

def has_sri_lanka_context(text):
    return bool(re.search(
        r'(ශ්‍රී\s*ලංකා|ශ්‍රීලංකා|ලංකාව|මෙරට|දිවයින|කොළඹ|රුපියල්|ලංකා\s*කණ්ඩායම|අපේ\s*රට)',
        str(text or ''),
        re.I,
    ))

def is_foreign_only(text):
    value = str(text or '')
    foreign_marker = bool(re.search(
        r'(විදෙස්\s*මාධ්‍ය|විදේශීය|ඉන්දියාව|ඉන්දියාවේ|පාකිස්තානය|පාකිස්තාන|'
        r'ඇමරිකාව|ඇමරිකාවේ|රුසියාව|රුසියාවේ|යුක්රේන|ඊශ්‍රායල|ගාසා|චීනය|'
        r'බ්‍රිතාන්‍ය|ප්‍රංශය|ජපානය|කොරියාව|ඔස්ට්‍රේලියාව|යුරෝපය|අප්‍රිකාව|'
        r'ලොව|ලෝකයේ|ගෝලීය)',
        value,
        re.I,
    ))
    return foreign_marker and not has_sri_lanka_context(value)

def is_relevant_foreign_news(text):
    value = str(text or '')
    return bool(re.search(
        r'(ලොව\s*විශාලතම|ලෝකයේ\s*විශාලතම|ගෝලීය|ජාත්‍යන්තර\s*හදිසි|'
        r'ලෝක\s*කුසලාන|ඔලිම්පික්|සුනාමි|භූමිකම්පා|වසංගත|තෙල්\s*මිල|'
        r'ජාත්‍යන්තර\s*වෙළඳපොළ|දැවැන්ත\s*සයිබර්|හැක්\s*කිරීම)',
        value,
        re.I,
    ))

def is_blocked_fluff(text):
    return bool(re.search(
        r'(ලග්න\s*පලාපල|කේන්දර|ජ්‍යොතිෂ|රාහු\s*කාලය|ලොතරැයි|'
        r'වෙළඳ\s*දැන්වීම|වට්ටම්\s*දීමනා|ඕපාදූප|ගොසිප්|gossip|'
        r'නළු\s*නිළි|විවාහ\s*ගිවිස|දික්කසාද)',
        str(text or ''),
        re.I,
    ))

def is_political_fluff(text):
    return bool(re.search(
        r'(පාර්ලිමේන්තුවේ\s*(උණුසුම්|කසුකුසුව|බහින්බස්|ගෝෂා|කෑකෝ|විරෝධතා|ලණු)|'
        r'එකිනෙකාට\s*(චෝදනා|දොස්)|මඩ\s*ප්‍රහාර|දේශපාලන\s*(වාග්\s*සටන්|ඕපාදූප|පොරය|ගොසිප්)|'
        r'මන්ත්‍රීවරු\s*ගහගනී|සභාව\s*කල්\s*තබයි|සභාවේ\s*උණුසුම්|කුණුහරුප|අභියෝග\s*කරයි)',
        str(text or ''),
        re.I
    ))

def should_keep_article(article):
    text = f"{article.get('title', '')} {article.get('summary', '')} {article.get('content', '')}"
    if is_blocked_fluff(text):
        return False
    if is_foreign_only(text):
        article['category'] = 'විදේශීය පුවත්'
        return is_relevant_foreign_news(text)
    return True

def is_sports_news(title, text):
    combined = f"{title} {text}".lower()
    return bool(re.search(
        r'(ක්‍රිකට්|පාපන්දු|ක්‍රීඩා|තරගාවලිය|තරඟාවලිය|ශූරතාව|ඔලිම්පික්|පැරා\s*ක්‍රීඩා|විකට්|ලකුණු|පිතිකර|පන්දු\s*යැව|අවසන්\s*මහා\s*තරග|ටෙස්ට්\s*තරග|එක්දින|විස්සයි20|t20|odi|ipl|lpl|icc|fifa|world\s*cup|තරගය\s*ජය|ජයග්‍රහණය\s*කරයි|පදක්කම|පදක්කම්|ශ්‍රී\s*ලංකා\s*කණ්ඩායම|ක්‍රීඩක)',
        combined
    ))

def is_emergency_news(text):
    low = text.lower()
    if is_foreign_only(low):
        return False
    return bool(re.search(r'(රතු\s*නිවේදන|සුනාමි|භූමිකම්පා|ගංවතුර\s*(අවදානම්|අනතුරු\s*ඇඟවීම)|නාය.{0,24}(අවදානම්|අනතුරු\s*ඇඟවීම)|සුළි\s*කුණාටු|තද\s*සුළං\s*අවදානම්|වාන්\s*දොරටු|ජලාශ.*වාන්|අධික\s*වැසි\s*අවදානම්|ඉවත්\s*වන්නැයි\s*දැනුම්|හදිසි\s*තත්ත්ව|ඇඳිරි\s*නීතිය)', low))

def calculate_importance(article):
    text = f"{article.get('title', '')} {article.get('summary', '')} {article.get('content', '')}"
    title = article.get('title', '')
    score = 30
    reasons = []

    if is_foreign_only(text):
        reasons.append("foreign_context")
        score -= 35
        if is_relevant_foreign_news(text):
            score += 20
            reasons.append("global_impact")

    if is_blocked_fluff(text):
        score -= 60
        reasons.append("blocked_fluff")

    if is_political_fluff(text):
        score -= 40
        reasons.append("political_fluff")

    if is_emergency_news(text):
        score += 60
        reasons.append("life_safety")

    if re.search(r'(විශේෂ\s*(පුවත|නිවේදනය)|හදිසි\s*නිවේදනය|ක්ෂණික\s*පුවත්|දැන්\s*ලැබුණු\s*පුවත|breaking\s*news|news\s*alert|\balert\b|flash\s*news)', text, re.I):
        score += 45
        reasons.append("breaking_news")

    if re.search(r'(විදේශ\s*ගමන්\s*බලපත්‍ර|හැඳුනුම්පත්|රියදුරු\s*බලපත්‍ර|දුම්රිය\s*(වර්ජන|ප්‍රමාද|ගාස්තු)|බස්\s*(වර්ජන|ගාස්තු)|විදුලි\s*(කප්පාදු|ගාස්තු)|ජල\s*කප්පාදු|ඉන්ධන\s*මිල|ගෑස්\s*මිල|පාසල්\s*(නිවාඩු|වසා)|විභාග\s*(ප්‍රතිඵල|කාලසටහන))', text, re.I):
        score += 35
        reasons.append("public_service")

    if re.search(r'(අයවැය|imf|මහ\s*බැංකු|පොලී\s*අනුපාත|උද්ධමනය|ඩොලර|රන්\s*මිල|කැබිනට්\s*(තීරණ|අනුමැති)|ජනාධිපති.*(තීරණ|නියෝග|ප්‍රකාශ)|අගමැති.*(තීරණ|නියෝග)|පනත\s*(සම්මත|අනුමත)|මැතිවරණ|ශ්‍රේෂ්ඨාධිකරණ|අධිකරණ\s*තීන්දුව)', text, re.I):
        score += 32
        reasons.append("national_impact")

    if re.search(r'(පොලිස්\s*විශේෂ|cid|tid|විශේෂ\s*කාර්ය\s*බලකා|මත්ද්‍රව්‍ය\s*වැටලී|මහා\s*පරිමාණ.*අත්අඩංගුවට|ඝාතන.*විමර්ශන|මාරක\s*(රිය|දුම්රිය)\s*අනතුර|ගිනි\s*ගැනීම|නෞකා\s*අනතුර)', text, re.I):
        score += 30
        reasons.append("law_and_order")

    is_defeat = bool(re.search(
        r'(පරාජය|පරාජය\s*වෙයි|පරාජයට\s*පත්වෙයි|පරාජය\s*විය|පැරදෙයි|පැරදුණ|පැරදුන)',
        text,
        re.I
    ))
    is_toss_or_routine = bool(re.search(r'(කාසියේ\s*වාසිය|පුහුණු\s*සැසිය|සංචිතය\s*නම්|ක්‍රීඩකයන්\s*නම්)', text, re.I))
    sl_match_or_win = has_sri_lanka_context(text) and not is_defeat and not is_toss_or_routine and bool(re.search(
        r'(අද\s*(පැවැත්වෙන|ක්‍රියාත්මක|තරගය)|අද\s*ශ්‍රී\s*ලංකා|'
        r'ශ්‍රී\s*ලංකා\s*(කණ්ඩායම|පිල).*(තරගය\s*ජය|ජයග්‍රහණය|මුහුණ\s*දෙයි|සටනට)|'
        r'ලෝක\s*කුසලාන|අවසන්\s*මහා\s*තරග|ශූරතාව|ඔලිම්පික්|පදක්කම|'
        r'තරගාවලිය\s*ජය|විශිෂ්ට\s*ජයක්|'
        r'ශ්‍රී\s*ලංකාව\s*ජය|ශ්‍රී\s*ලංකා\s*කණ්ඩායම\s*ජය)',
        text,
        re.I,
    ))
    if sl_match_or_win:
        score += 35
        reasons.append("national_team_sport")
    elif is_sports_news(title, text):
        if is_defeat:
            score -= 20
        else:
            score += 15
        reasons.append("sports_news")

    score = max(0, min(100, score))
    tier = "feed_only"
    if is_emergency_news(text):
        tier = "emergency"
    elif score >= HERA_CONFIG["urgent_threshold"] or "breaking_news" in reasons:
        tier = "urgent"
    elif score >= HERA_CONFIG["high_priority_threshold"]:
        tier = "high"

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
        "https://www.itnnews.lk/wp-json/wp/v2/posts?categories=80&_embed=wp:featuredmedia&per_page=6"
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
                # Exclude sports stories older than 48 hours to preserve priority for breaking national news
                now_ms_check = int(time.time() * 1000)
                if is_sports and (now_ms_check - pub_ms > 48 * 3600 * 1000):
                    continue

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
        raw = http_get(url, timeout=18)
        if raw and ("<rss" in raw or "<channel" in raw):
            break
        else:
            raw = None

    if raw:
        try:
            raw_bytes = raw.encode('utf-8') if isinstance(raw, str) else raw
            root = ET.fromstring(raw_bytes)
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

    # Robust Fallback: Scrape Ada Derana Homepage HTML if RSS provided fewer than 5 items
    if len(articles) < 5:
        try:
            hp_html = http_get("https://sinhala.adaderana.lk/", timeout=15)
            if hp_html and "/news/" in hp_html:
                soup = BeautifulSoup(hp_html, 'html.parser')
                base_time = int(time.time() * 1000)
                for a_tag in soup.find_all('a', href=re.compile(r'/news/(\d+)')):
                    href = a_tag.get('href', '')
                    full_link = href if href.startswith('http') else f"https://sinhala.adaderana.lk{href}"
                    id_m = re.search(r'/news/(\d+)', full_link)
                    p_id = f"ad_{id_m.group(1)}" if id_m else f"ad_{abs(hash(full_link))}"
                    if p_id in seen:
                        continue
                    seen.add(p_id)

                    title = clean_text(a_tag.get_text())
                    if len(title) < 8 or "කියවන්න" in title or "විස්තර" in title:
                        continue

                    img_elem = a_tag.find('img')
                    img_src = ""
                    if img_elem:
                        img_src = img_elem.get('src') or img_elem.get('data-src') or ""

                    num_id = int(id_m.group(1)) if id_m else 0
                    estimated_ms = base_time - max(0, (253960 - num_id) * 120000) if num_id > 0 else base_time

                    cat_name = "ක්‍රීඩා පුවත්" if is_sports_news(title, "") else "දේශීය පුවත්"
                    articles.append({
                        "id": p_id,
                        "title": title,
                        "category": cat_name,
                        "summary": title,
                        "content": title,
                        "image_url": normalize_image_url(img_src),
                        "published_at": datetime.fromtimestamp(estimated_ms / 1000, tz=SL_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"),
                        "published_at_ms": estimated_ms,
                        "fetched_at_ms": int(time.time() * 1000),
                        "source_name": "අද දෙරණ",
                        "source_url": full_link
                    })
        except Exception as e:
            print(f"Ada Derana HTML fallback notice: {e}")

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
            img_m = re.search(r'<meta[^>]+property=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)["\']', page_html, re.I)
            if not img_m:
                img_m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\'](?:og:image|twitter:image)["\']', page_html, re.I)
            if not img_m:
                img_m = re.search(r'property=["\'](?:og:image|twitter:image)["\']\s+content=["\']([^"\']+)["\']', page_html, re.I)
            if not img_m and "media.adaderanasinhala.lk" in page_html:
                img_m = re.search(r'(https://media\.adaderanasinhala\.lk/articles/\d+/[^"\'\s>]+)', page_html, re.I)
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

    for art in articles:
        tokens = title_tokens(art["title"])
        if not tokens:
            continue
        duplicate = False
        for ex_art, ex_tokens in seen_titles:
            shared = len(tokens.intersection(ex_tokens))
            similarity = token_similarity(tokens, ex_tokens)
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

def load_existing_articles():
    if not os.path.exists(NEWS_JSON_FILE):
        return []
    try:
        with open(NEWS_JSON_FILE, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]
    except Exception as error:
        print(f"Warning: Could not load existing {NEWS_JSON_FILE}: {error}")
        return []

def merge_with_history(fresh_articles, existing_articles, now_ms):
    by_id = {}
    for article in existing_articles:
        article_id = str(article.get("id") or "").strip()
        if not article_id:
            continue
        saved = dict(article)
        saved["first_seen_ms"] = int(
            saved.get("first_seen_ms")
            or saved.get("fetched_at_ms")
            or saved.get("published_at_ms")
            or now_ms
        )
        by_id[article_id] = saved

    for article in fresh_articles:
        article_id = str(article.get("id") or "").strip()
        if not article_id:
            continue
        previous = by_id.get(article_id, {})
        current = dict(article)
        current["first_seen_ms"] = int(
            previous.get("first_seen_ms")
            or previous.get("fetched_at_ms")
            or current.get("fetched_at_ms")
            or now_ms
        )
        by_id[article_id] = current

    cutoff_ms = now_ms - HERA_CONFIG["retention_hours"] * 60 * 60 * 1000
    future_limit_ms = now_ms + 6 * 60 * 60 * 1000
    recent = []
    for article in by_id.values():
        published_ms = int(article.get("published_at_ms") or 0)
        effective_ms = published_ms or int(article.get("first_seen_ms") or 0)
        if effective_ms < cutoff_ms or effective_ms > future_limit_ms:
            continue
        if not should_keep_article(article):
            continue
        # Drop sports stories older than 48 hours so stale matches never clutter the feed
        if is_sports_news(article.get("title", ""), article.get("summary", "")) and (now_ms - effective_ms > 48 * 3600 * 1000):
            continue
        recent.append(article)

    recent.sort(key=lambda item: int(item.get("published_at_ms") or 0), reverse=True)
    return deduplicate_articles(recent)[:HERA_CONFIG["max_feed_items"]]

def was_story_notified(article, recent_titles):
    tokens = title_tokens(article.get("title", ""))
    return any(token_similarity(tokens, title_tokens(title)) >= 0.50 for title in recent_titles)

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

def sync_firebase_realtime_db(articles, source_counts=None):
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
                "first_seen_ms": art.get("first_seen_ms", 0),
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
        # The curated feed is authoritative. Replace items too so stale
        # Apps Script records cannot remain addressable indefinitely.
        root_ref.child('items').set(items_dict)
        root_ref.child('meta').update({
            "initialized": True,
            "source": "Hera News Engine (GitHub 24/7)",
            "active_count": len(feed_dict),
            "maximum_articles": HERA_CONFIG["max_feed_items"],
            "retention_hours": HERA_CONFIG["retention_hours"],
            "decision_engine_version": "hera-intelligence-v3",
            "maximum_notifications_per_day": HERA_CONFIG["max_notifications_per_day"],
            "updated_at_ms": int(time.time() * 1000),
            "last_scan_status": "success",
            "source_counts": source_counts or {},
        })
        print(f"✅ Firebase Realtime Database updated with {len(feed_dict)} clean feed items (Newest to Oldest)!")
    except Exception as e:
        print(f"Warning: Firebase DB sync error: {e}")

def send_fcm_notification(article):
    try:
        from firebase_admin import messaging
        clean_title = article['title'][:120]
        clean_summary = (article.get('summary') or article.get('category') or 'අලුත් පුවතක්')[:180]
        body = clean_summary[:120] or clean_title[:120]
        safe_image = article.get('image_url') or ""

        # Sized safely for FCM 4096-byte limit with 3-byte Sinhala UTF-8 characters
        message = messaging.Message(
            topic=HERA_CONFIG["topic"],
            data={
                "type": "news",
                "id": str(article["id"]),
                "news_id": str(article["id"]),
                "title": clean_title,
                "article_title": clean_title,
                "body": body,
                "category": article.get("category", "දේශීය පුවත්")[:30],
                "summary": clean_summary,
                "content": (article.get("content") or clean_summary)[:400],
                "image_url": safe_image,
                "published_at": article.get("published_at", get_now_sl().strftime("%Y-%m-%d %H:%M:%S")),
                "published_at_ms": str(article.get("published_at_ms") or ""),
                "source_name": article.get("source_name", "Hera News")[:40],
                "source_url": str(article.get("source_url") or "")[:200],
            },
            android=messaging.AndroidConfig(
                priority="high",
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
    now_ms = int(time.time() * 1000)
    print(f"[{now_sl.strftime('%Y-%m-%d %H:%M:%S')} SLST] Hera News 24/7 Engine Running...")

    state = load_state()
    dry_run = os.environ.get("HERA_DRY_RUN", "").strip().lower() == "true"
    force_run = os.environ.get("HERA_FORCE_RUN", "").strip().lower() == "true"
    last_scan_ms = int(state.get("last_successful_scan_ms") or 0)
    minutes_since_scan = (now_ms - last_scan_ms) / 60000 if last_scan_ms else None
    if (
        not force_run
        and minutes_since_scan is not None
        and minutes_since_scan < HERA_CONFIG["minimum_scan_gap_minutes"]
    ):
        print(
            f"Duplicate trigger ignored: last successful scan was "
            f"{minutes_since_scan:.1f} minutes ago."
        )
        return

    has_firebase = False if dry_run else initialize_firebase()
    existing_articles = load_existing_articles()

    # 1. Fetch articles from all 3 independent sources
    itn_articles = fetch_itn_news()
    lankadeepa_articles = fetch_lankadeepa_news()
    derana_articles = fetch_adaderana_news()
    source_counts = {
        "itn": len(itn_articles),
        "lankadeepa": len(lankadeepa_articles),
        "ada_derana": len(derana_articles),
    }

    combined = itn_articles + lankadeepa_articles + derana_articles
    if not combined:
        raise RuntimeError("All news sources were empty or unreachable.")

    # 2. Remove obvious junk/irrelevant foreign stories, then deduplicate.
    eligible_articles = [article for article in combined if should_keep_article(article)]
    unique_articles = deduplicate_articles(eligible_articles)
    print(
        f"Combined total: {len(combined)} -> eligible: {len(eligible_articles)} "
        f"-> deduplicated: {len(unique_articles)} articles."
    )

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

    # 4. Merge the current scan with up to 7 days of history so a short RSS
    # response cannot shrink the app feed. Re-score after enrichment.
    fresh_scored = [calculate_importance(art) for art in unique_articles]
    scored_articles = merge_with_history(fresh_scored, existing_articles, now_ms)
    scored_articles = [calculate_importance(art) for art in scored_articles]
    scored_articles.sort(
        key=lambda item: int(item.get("published_at_ms") or 0),
        reverse=True,
    )
    image_count = sum(1 for item in scored_articles if item.get("image_url"))
    content_count = sum(1 for item in scored_articles if item.get("content"))
    print(
        f"Curated feed quality: {len(scored_articles)} items, "
        f"{image_count} images, {content_count} full-content entries."
    )

    # 5. Save to news.json for GitHub Pages
    json_list = scored_articles[:HERA_CONFIG["max_json_items"]]
    if dry_run:
        print(f"DRY RUN: validated {len(json_list)} articles; no files or cloud data changed.")
    else:
        try:
            with open(NEWS_JSON_FILE, "w", encoding="utf-8") as f:
                json.dump(json_list, f, ensure_ascii=False, indent=2)
            print(f"✅ Saved {len(json_list)} fresh articles to {NEWS_JSON_FILE} (Sorted Newest First).")
        except Exception as e:
            print(f"Warning: Could not write {NEWS_JSON_FILE}: {e}")

    # 6. Sync cleanly to Firebase Realtime Database
    if has_firebase:
        sync_firebase_realtime_db(scored_articles, source_counts)

    # 7. Notification Dispatcher
    today_str = now_sl.strftime("%Y-%m-%d")
    hour_str = now_sl.strftime("%Y-%m-%d-%H")

    if state.get("budget_day") != today_str:
        state["budget_day"] = today_str
        state["daily_count"] = 0

    if state.get("budget_hour") != hour_str:
        state["budget_hour"] = hour_str
        state["hourly_count"] = 0

    recent_keys = set(str(value) for value in state.get("recent_notified_keys", []))
    recent_titles = [str(value) for value in state.get("recent_notified_titles", [])]
    for article in scored_articles:
        if str(article.get("id")) in recent_keys:
            recent_titles.append(article.get("title", ""))

    # First v3 run establishes a baseline and never floods users with the
    # existing backlog. Subsequent runs keep retrying unsent important stories
    # until pacing permits them or they age out.
    initialized = bool(state.get("v3_initialized"))
    freshness_cutoff_ms = now_ms - 4 * 60 * 60 * 1000  # 4-hour window ensures breaking stories aren't lost between scans
    candidates = []
    if initialized:
        candidates = [
            article for article in scored_articles
            if article.get("importance_tier") in ("high", "urgent", "emergency")
            and int(article.get("published_at_ms") or 0) >= freshness_cutoff_ms
            and str(article.get("id")) not in recent_keys
            and not was_story_notified(article, recent_titles)
        ]
    else:
        baseline = [
            article.get("title", "") for article in scored_articles
            if article.get("importance_tier") in ("high", "urgent", "emergency")
        ]
        recent_titles.extend(baseline)
        state["v3_initialized"] = True
        print("Notification baseline initialized; existing backlog will not be sent.")

    tier_rank = {"emergency": 4, "urgent": 3, "high": 2, "feed_only": 0}
    candidates.sort(
        key=lambda item: (
            tier_rank.get(item.get("importance_tier", "feed_only"), 0),
            item.get("importance_score", 0),
            item.get("published_at_ms", 0),
        ),
        reverse=True
    )

    if candidates and has_firebase:
        top_art = candidates[0]
        tier = top_art.get("importance_tier")
        is_urgent = tier in ("urgent", "emergency")

        last_sent_ms = state.get("last_notification_ms", 0)
        gap_needed_min = HERA_CONFIG["high_priority_gap_minutes"]
        if is_urgent:
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
                recent_titles.append(top_art.get("title", ""))
                save_state(state)
    else:
        print("No candidates pending notification or Firebase not initialized.")

    if not dry_run:
        state["recent_notified_titles"] = [title for title in recent_titles if title][-150:]
        state["last_successful_scan_ms"] = now_ms
        state["last_source_counts"] = source_counts
        state["last_feed_count"] = len(scored_articles)
        save_state(state)
    print("Hera News Engine run finished successfully.")

if __name__ == "__main__":
    main()
