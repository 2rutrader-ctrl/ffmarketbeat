#!/usr/bin/env python3
"""FF Market Beat: news collector.

Reads official RSS/Atom feeds, keeps ONLY headline + original link + source + time,
and writes news.json. Never stores article text. Uses the Python standard library only.

Sources here are limited to the ones cleared in the terms check. To add a source later
(for example RBI or SEBI after written permission), add one line to FEEDS.
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

# key, credit line shown beside every item, category, feed URL
# PIB publishes for every ministry, so only items that look market or economy related are kept.
PIB_RELEVANT = (
    r"\b(finance|financial|economy|economic|gdp|inflation|cpi|tax|taxes|gst|customs|duty|duties|"
    r"export|exports|import|imports|trade|bank|banks|banking|insurance|rbi|reserve bank|sebi|"
    r"market|markets|investment|investments|fdi|fiscal|budget|bond|bonds|rupee|forex|crude|"
    r"petroleum|gold|msme|industrial production|iip|disinvestment|securities|pension)\b"
)

# key, credit line shown beside every item, category, feed URL, optional relevance filter
FEEDS = [
    ("fed", "US Federal Reserve Board", "Global",
     "https://www.federalreserve.gov/feeds/press_monetary.xml", None),
    ("fed", "US Federal Reserve Board", "Global",
     "https://www.federalreserve.gov/feeds/speeches.xml", None),
    ("ecb", "European Central Bank", "Global",
     "https://www.ecb.europa.eu/rss/press.xml", None),
    ("pib", "Press Information Bureau, Government of India", "India",
     "https://www.pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3", PIB_RELEVANT),
    # Same official feed on the host without "www", in case only one host answers servers like GitHub's.
    ("pib", "Press Information Bureau, Government of India", "India",
     "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3", PIB_RELEVANT),
]

# India lane: headline metadata from the GDELT Project (free, redistribution allowed with citation).
# Only headline, link, publisher name and time are kept. Publishers keep copyright in their articles.
GDELT_DOMAINS = {
    "economictimes.indiatimes.com": "The Economic Times",
    "livemint.com": "Mint",
    "business-standard.com": "Business Standard",
    "thehindubusinessline.com": "The Hindu BusinessLine",
    "financialexpress.com": "The Financial Express",
    "moneycontrol.com": "Moneycontrol",
    "ndtvprofit.com": "NDTV Profit",
    "businesstoday.in": "Business Today",
    "cnbctv18.com": "CNBC-TV18",
    "zeebiz.com": "Zee Business",
    "thehindu.com": "The Hindu",
    "indianexpress.com": "The Indian Express",
    "hindustantimes.com": "Hindustan Times",
    "timesofindia.indiatimes.com": "The Times of India",
    "ndtv.com": "NDTV",
}
GDELT_TOPICS = [
    '(RBI OR "Reserve Bank" OR repo OR "monetary policy")',
    '(inflation OR GDP OR "fiscal deficit" OR "GST collection" OR "industrial production")',
    '(rupee OR "crude oil" OR "bond yield" OR FPI OR "forex reserves")',
    '(SEBI OR IPO OR "quarterly results" OR Nifty OR Sensex)',
]
# Headlines that read like tips or recommendations are dropped. This page is education only.
TIP_WORDS = (
    r"\b(buy|sell|short|long|target|targets|stoploss|stop loss|top picks?|stocks? to|picks?|"
    r"recommend\w*|multibagger|should you|accumulate|bullish|bearish|upside|downside|"
    r"rally to|crash|jackpot|rocket|zoom|soar|plunge)\b"
)

MAX_ITEMS = 120
THOUGHTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "thoughts.json")
USER_AGENT = "FFMarketBeat/1.0 (headline aggregator, links to originals)"


def local(tag):
    """Tag name without its XML namespace."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def parse_date(text):
    if not text:
        return None
    text = text.strip()
    try:
        d = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def parse_feed(xml_bytes, key, credit, category):
    root = ET.fromstring(xml_bytes)
    out = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        title = link = date_text = None
        for child in el:
            name = local(child.tag)
            if name == "title":
                title = (child.text or "").strip()
            elif name == "link":
                link = (child.text or "").strip() or child.attrib.get("href", "").strip()
            elif name in ("pubDate", "published", "updated", "date") and not date_text:
                date_text = child.text
        when = parse_date(date_text)
        if title and link and link.startswith("http"):
            out.append({
                "title": " ".join(title.split()),
                "link": link,
                "source": credit,
                "source_key": key,
                "category": category,
                "published": when.isoformat() if when else None,
            })
    return out


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()


GKG_FILES = 12  # 12 files of 15 minutes each = the last 3 hours
INDIA_RELEVANT = (
    r"\b(rbi|reserve bank|repo|monetary|inflation|cpi|wpi|gdp|iip|pmi|fiscal|gst|rupee|crude|oil|"
    r"yield|bond|fpi|fii|forex|sebi|ipo|nifty|sensex|results|earnings|budget|tax|tariff|duty|trade|"
    r"export|import|bank|banks|market|markets|economy|economic|gold|silver|fed|ecb|dollar|liquidity|"
    r"lending|loan|rate|rates|growth|stocks?|shares?|rupees?)\b"
)


def fetch_big(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


def gdelt_items():
    """India business headlines from GDELT's open 15-minute GKG files (static downloads, no API
    rate limit). Keeps only the page title, link, publisher and time. Raises if nothing usable."""
    import html
    import io
    import zipfile
    tip = re.compile(TIP_WORDS, re.I)
    rel = re.compile(INDIA_RELEVANT, re.I)
    title_re = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", re.S)
    # find the newest file name from lastupdate.txt
    last = fetch("http://data.gdeltproject.org/gdeltv2/lastupdate.txt").decode("utf-8", "replace")
    m = re.search(r"/gdeltv2/(\d{14})\.gkg\.csv\.zip", last)
    if not m:
        raise RuntimeError("lastupdate.txt had no gkg file name")
    newest = datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    out, files_ok, records, errors, last_err = [], 0, 0, 0, ""
    from datetime import timedelta
    for k in range(GKG_FILES):
        stamp = (newest - timedelta(minutes=15 * k)).strftime("%Y%m%d%H%M%S")
        try:
            raw = fetch_big(f"http://data.gdeltproject.org/gdeltv2/{stamp}.gkg.csv.zip")
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                text = z.read(z.namelist()[0]).decode("utf-8", "replace")
        except Exception as e:
            errors += 1
            last_err = f"{type(e).__name__}: {e}"[:150]
            continue
        files_ok += 1
        for line in text.split("\n"):
            cols = line.split("\t")
            if len(cols) < 27:
                continue
            records += 1
            dom = cols[3].lower()
            name = next((n for d, n in GDELT_DOMAINS.items() if dom == d or dom.endswith("." + d)), None)
            if not name:
                continue
            tm = title_re.search(cols[26])
            title = " ".join(html.unescape(tm.group(1)).split()) if tm else ""
            link = cols[4].strip()
            try:
                when = datetime.strptime(cols[1], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if not (title and link.startswith("http")) or tip.search(title) or not rel.search(title):
                continue
            out.append({"title": title, "link": link, "source": name + " (headline via GDELT)",
                        "source_key": "gdelt", "category": "India", "published": when.isoformat()})
    if not out:
        raise RuntimeError(f"no usable headlines ({files_ok} of {GKG_FILES} files read, {records} records scanned, {errors} download errors; last error: {last_err or 'none'})")
    seen, uniq = set(), []
    for it in out:
        if it["link"] not in seen:
            seen.add(it["link"])
            uniq.append(it)
    uniq.sort(key=lambda x: x["published"], reverse=True)
    return uniq[:60]


def load_thoughts():
    """FF Thought library. Generic education about a topic. Never a view on direction."""
    try:
        with open(THOUGHTS_FILE, encoding="utf-8") as f:
            lib = json.load(f)
    except Exception:
        return []
    for t in lib:
        t["_re"] = re.compile(
            r"\b(?:" + "|".join(re.escape(k.lower()) for k in t["keywords"]) + r")\b")
    return lib


def attach_thought(item, lib):
    low = item["title"].lower()
    for t in lib:  # first match wins, so order in thoughts.json is priority
        if t["_re"].search(low):
            item["thought"] = {"topic": t["title"], "short": t.get("short", t["title"]), "text": t["text"]}
            return


def main(out_path="news.json"):
    items, report = [], {}
    for key, credit, category, url, relevant in FEEDS:
        try:
            got = parse_feed(fetch(url), key, credit, category)
            if relevant:
                rx = re.compile(relevant, re.I)
                got = [g for g in got if rx.search(g["title"])]
            items.extend(got)
            msg = f"ok, {len(got)} items"
        except Exception as e:  # one bad feed must never break the others
            msg = f"FAILED: {type(e).__name__}: {e}"
        report[key] = (report[key] + "; " + msg) if key in report else msg
    try:
        got = gdelt_items()
        items.extend(got)
        report["gdelt"] = f"ok, {len(got)} items"
    except Exception as e:
        report["gdelt"] = f"FAILED: {type(e).__name__}: {e}"
    seen, unique = set(), []
    for it in items:
        if it["link"] not in seen:
            seen.add(it["link"])
            unique.append(it)
    unique.sort(key=lambda x: x["published"] or "", reverse=True)
    lib = load_thoughts()
    for it in unique:
        attach_thought(it, lib)
    payload = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "feed_status": report,
        "items": unique[:MAX_ITEMS],
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    for k, v in report.items():
        print(f"{k}: {v}")
    print(f"wrote {len(payload['items'])} items to {out_path}")


if __name__ == "__main__":
    main(*sys.argv[1:2])
