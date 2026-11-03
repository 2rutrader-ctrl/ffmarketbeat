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

# Central-bank feeds: keep only items about policy, rates, inflation, growth or markets.
CB_RELEVANT = (
    r"\b(interest rates?|monetary|inflation|projections?|minutes|fomc|rate decision|policy|outlook|"
    r"financial stability|balance sheet|bond|yields?|economy|economic|gdp|growth|labou?r market|"
    r"liquidity|employment|prices?|tariffs?|dollar|euro area|federal funds|discount rate)\b"
)

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
     "https://www.federalreserve.gov/feeds/speeches.xml", CB_RELEVANT),
    ("ecb", "European Central Bank", "Global",
     "https://www.ecb.europa.eu/rss/press.xml", CB_RELEVANT),
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

MAX_ITEMS = 300
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
    r"\b(rbi|reserve bank|repo|monetary|inflation|wpi|gdp|iip|pmi|fiscal|gst|rupee|crude|oil|"
    r"yields?|bonds?|fpis?|fiis?|forex|sebi|ipo|nifty|sensex|union budget|budget 20[0-9][0-9]|fiscal deficit|tariffs?|duty|duties|exports?|"
    r"imports?|economy|economic|gold|silver|dollar|liquidity|fed|ecb|interest rates?|"
    r"stock market|share market|equity market|bond market|currency market|"
    r"quarterly results|q[1-4] (results|earnings)|net profit)\b"
)


# Retail personal-finance and clickbait headlines are not useful for a market briefing.
NOT_MARKET = (
    r"\b(fd|fds|fixed deposits?|savings accounts?|credit cards?|debit cards?|home loans?|personal loans?|"
    r"car loans?|emi|horoscope|petrol|diesel|lpg|aadhaar|pan card|itr|sip|nps|ppf)\b|"
    r"check details|check full list|rate today|price today|rates today|everything you need to know"
)


# Large Indian companies, for the "Companies in the news" list. Names only: no prices, no exchange data.
# (display name, pattern, case_sensitive)
COMPANIES = [
    ("Reliance Industries", r"\bReliance( Industries| Jio| Retail)?\b", True),
    ("TCS", r"\bTCS\b|\bTata Consultancy", True),
    ("Infosys", r"\bInfosys\b", True),
    ("Wipro", r"\bWipro\b", True),
    ("HCLTech", r"\bHCL ?Tech(nologies)?\b", True),
    ("Tech Mahindra", r"\bTech Mahindra\b", True),
    ("HDFC Bank", r"\bHDFC Bank\b", True),
    ("ICICI Bank", r"\bICICI Bank\b", True),
    ("State Bank of India", r"\bSBI\b|\bState Bank of India\b", True),
    ("Axis Bank", r"\bAxis Bank\b", True),
    ("Kotak Mahindra Bank", r"\bKotak( Mahindra)?( Bank)?\b", True),
    ("IndusInd Bank", r"\bIndusInd\b", True),
    ("Bank of Baroda", r"\bBank of Baroda\b", True),
    ("Punjab National Bank", r"\bPNB\b|\bPunjab National Bank\b", True),
    ("Canara Bank", r"\bCanara Bank\b", True),
    ("IDBI Bank", r"\bIDBI\b", True),
    ("Bajaj Finance", r"\bBajaj Finance\b", True),
    ("Bajaj Finserv", r"\bBajaj Finserv\b", True),
    ("Bajaj Auto", r"\bBajaj Auto\b", True),
    ("Hindustan Unilever", r"\bHUL\b|\bHindustan Unilever\b", True),
    ("ITC", r"\bITC\b", True),
    ("Larsen & Toubro", r"\bL&T\b|\bLarsen", True),
    ("Maruti Suzuki", r"\bMaruti( Suzuki)?\b", True),
    ("Mahindra & Mahindra", r"\bM&M\b|\bMahindra & Mahindra\b", True),
    ("Tata Motors", r"\bTata Motors\b", True),
    ("Tata Steel", r"\bTata Steel\b", True),
    ("Tata Power", r"\bTata Power\b", True),
    ("Sun Pharma", r"\bSun Pharma\b", True),
    ("Dr Reddy's", r"\bDr\.? Reddy", True),
    ("Cipla", r"\bCipla\b", True),
    ("Apollo Hospitals", r"\bApollo Hospitals\b", True),
    ("Titan", r"\bTitan (Company|Co)\b|\bTitan\b", True),
    ("Asian Paints", r"\bAsian Paints\b", True),
    ("UltraTech Cement", r"\bUltraTech\b", True),
    ("JSW Steel", r"\bJSW Steel\b", True),
    ("Hindalco", r"\bHindalco\b", True),
    ("Vedanta", r"\bVedanta\b", True),
    ("Coal India", r"\bCoal India\b", True),
    ("ONGC", r"\bONGC\b", True),
    ("NTPC", r"\bNTPC\b", True),
    ("Power Grid", r"\bPower Grid\b|\bPowerGrid\b", True),
    ("BPCL", r"\bBPCL\b|\bBharat Petroleum\b", True),
    ("Indian Oil", r"\bIndian Oil\b|\bIOC\b", True),
    ("Adani Group", r"\bAdani\b", True),
    ("Bharti Airtel", r"\bAirtel\b", True),
    ("Vodafone Idea", r"\bVodafone Idea\b", True),
    ("Eternal (Zomato)", r"\bZomato\b|\bEternal Ltd\b", True),
    ("Paytm", r"\bPaytm\b", True),
    ("LIC", r"\bLIC\b|\bLife Insurance Corporation\b", True),
    ("HAL", r"\bHAL\b|\bHindustan Aeronautics\b", True),
    ("Bharat Electronics", r"\bBharat Electronics\b", True),
    ("IRCTC", r"\bIRCTC\b", True),
    ("IndiGo", r"\bIndiGo\b|\bInterGlobe\b", True),
    ("Nestle India", r"\bNestl[eé] India\b", True),
    ("Britannia", r"\bBritannia\b", True),
    ("Eicher Motors", r"\bEicher\b|\bRoyal Enfield\b", True),
    ("Hero MotoCorp", r"\bHero MotoCorp\b", True),
    ("TVS Motor", r"\bTVS Motor\b", True),
    ("SBI Life", r"\bSBI Life\b", True),
    ("HDFC Life", r"\bHDFC Life\b", True),
    ("Jio Financial", r"\bJio Financial\b", True),
    ("Shriram Finance", r"\bShriram Finance\b", True),
    ("Trent", r"\bTrent\b", True),
    ("Nykaa", r"\bNykaa\b", True),
]
_COMPANY_RE = [(n, re.compile(p, 0 if cs else re.I)) for n, p, cs in COMPANIES]
# Headlines about price moves are not listed under companies: the list is for news mentions only.
MOVE_WORDS = (
    r"\b(surge[sd]?|jump(s|ed)?|rall(y|ies|ied)|tumbl\w*|slump\w*|slid(e|es)?|sink\w*|skyrocket\w*|"
    r"upper circuit|lower circuit|gains?|gained|falls?|fell|drops?|dropped|hits? (record|high|low)|"
    r"52-week|target price|shares? (up|down))\b"
)


def find_companies(title):
    return [n for n, rx in _COMPANY_RE if rx.search(title)][:3]


# A headline counts as India-specific if it names India, an Indian institution, a rupee amount or a listed Indian company.
INDIA_SCOPE = (
    r"\b(india|indian|indians|bharat|rbi|sebi|nse|bse|nifty|sensex|rupee|rupees|crore|lakh|gst|"
    r"union budget|budget 20[0-9][0-9]|sitharaman|modi|irdai|nclt|fpis?|fiis?|dii|mumbai|delhi|"
    r"dalal street|gift city|mpc|repo rate|west asia)\b|\u20b9|\bRs\.? ?[0-9]"
)


def fetch_big(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


def gdelt_items(n_files=GKG_FILES):
    """India business headlines from GDELT's open 15-minute GKG files (static downloads, no API
    rate limit). Keeps only the page title, link, publisher and time. Raises if nothing usable."""
    import html
    import io
    import zipfile
    tip = re.compile(TIP_WORDS, re.I)
    rel = re.compile(INDIA_RELEVANT, re.I)
    junk = re.compile(NOT_MARKET, re.I)
    scope_rx = re.compile(INDIA_SCOPE, re.I)
    move = re.compile(MOVE_WORDS, re.I)
    title_re = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", re.S)
    # find the newest file name from lastupdate.txt
    last = fetch("http://data.gdeltproject.org/gdeltv2/lastupdate.txt").decode("utf-8", "replace")
    m = re.search(r"/gdeltv2/(\d{14})\.gkg\.csv\.zip", last)
    if not m:
        raise RuntimeError("lastupdate.txt had no gkg file name")
    newest = datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    out, files_ok, records, errors, last_err = [], 0, 0, 0, ""
    from datetime import timedelta
    for k in range(n_files):
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
            comps = find_companies(title) if title else []
            if not (title and link.startswith("http")) or "press-release" in link.lower() or tip.search(title) or junk.search(title) or not (rel.search(title) or comps):
                continue
            is_india = bool(comps) or bool(scope_rx.search(title))
            item = {"title": title, "link": link, "source": name + " (headline via GDELT)",
                    "source_key": "gdelt", "category": "India" if is_india else "Global",
                    "published": when.isoformat()}
            if comps and not move.search(title):
                item["companies"] = comps
            out.append(item)
    if not out:
        raise RuntimeError(f"no usable headlines ({files_ok} of {n_files} files read, {records} records scanned, {errors} download errors; last error: {last_err or 'none'})")
    seen, uniq = set(), []
    for it in out:
        if it["link"] not in seen:
            seen.add(it["link"])
            uniq.append(it)
    uniq.sort(key=lambda x: x["published"], reverse=True)
    return uniq[:150]


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
    prev_items, n_files = [], GKG_FILES
    try:  # keep recent India headlines from earlier runs, so a gap between runs loses nothing
        with open(out_path, encoding="utf-8") as f:
            prev = json.load(f)
        pu = parse_date(prev.get("updated"))
        if pu:
            gap_h = (datetime.now(timezone.utc) - pu).total_seconds() / 3600
            n_files = min(64, max(GKG_FILES, int(gap_h * 4) + 4))
        cutoff = datetime.now(timezone.utc).timestamp() - 48 * 3600
        if sum(1 for it in prev.get("items", []) if it.get("source_key") == "gdelt") < 25:
            n_files = 64  # thin history: read the last 16 hours of GDELT files
        for it in prev.get("items", []):
            d = parse_date(it.get("published"))
            if it.get("source_key") == "gdelt" and d and d.timestamp() > cutoff:
                it.pop("thought", None)
                prev_items.append(it)
    except Exception:
        pass
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
        got = gdelt_items(n_files)
        items.extend(got)
        report["gdelt"] = f"ok, {len(got)} new items ({n_files} files read)"
    except Exception as e:
        report["gdelt"] = f"FAILED: {type(e).__name__}: {e}"
    items.extend(prev_items)  # older headlines carried over from the previous file
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
