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
]

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
