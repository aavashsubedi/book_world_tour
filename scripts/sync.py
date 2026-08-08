#!/usr/bin/env python3
"""Sync Goodreads reading data into data/books.json.

Sources (merged, deduped):
  1. data/goodreads_export.csv  — one-time full-history backfill (Goodreads CSV export)
  2. Goodreads shelf RSS feed   — daily incremental sync (config.json: rss_url)

Author -> country resolution (cached in data/countries.json, each author resolved once):
  1. Wikidata: search author, read P27 (country of citizenship), get label + P299 (ISO numeric)
  2. Gemini fallback (env GEMINI_API_KEY) for authors Wikidata can't resolve

Stdlib only — no pip dependencies.
"""

import csv
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(ROOT, "data", "goodreads_export.csv")
CACHE_PATH = os.path.join(ROOT, "data", "countries.json")
BOOKS_PATH = os.path.join(ROOT, "data", "books.json")
CONFIG_PATH = os.path.join(ROOT, "config.json")

USER_AGENT = "BookWorldTour/1.0 (personal reading-map project; github.com)"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"


def http_get_json(url, headers=None, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get_text(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def norm_author(name):
    """Normalize an author name for dedupe/cache keys."""
    return re.sub(r"\s+", " ", name).strip()


def book_key(title, author):
    return (re.sub(r"\s*\(.*?\)\s*$", "", title).strip().lower(), norm_author(author).lower())


# ---------------------------------------------------------------- Goodreads CSV

def load_csv():
    books = []
    if not os.path.exists(CSV_PATH):
        return books
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("Exclusive Shelf", "").strip() != "read":
                continue
            title = row.get("Title", "").strip()
            author = norm_author(row.get("Author", ""))
            if not title or not author:
                continue
            books.append({
                "title": title,
                "author": author,
                "date_read": (row.get("Date Read") or "").strip().replace("/", "-") or None,
            })
    print(f"CSV backfill: {len(books)} read books")
    return books


# ---------------------------------------------------------------- Goodreads RSS

def load_rss(rss_url):
    books = []
    try:
        xml_text = http_get_text(rss_url)
        root = ET.fromstring(xml_text)
    except Exception as e:
        print(f"RSS fetch failed ({e}) — continuing with CSV data only", file=sys.stderr)
        return books
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        author = norm_author(item.findtext("author_name") or "")
        read_at = (item.findtext("user_read_at") or "").strip()
        date_read = None
        if read_at:
            try:
                date_read = datetime.strptime(read_at, "%a, %d %b %Y %H:%M:%S %z").strftime("%Y-%m-%d")
            except ValueError:
                pass
        shelves = (item.findtext("user_shelves") or "").strip()
        # A read-shelf feed leaves user_shelves empty or includes "read"
        if shelves and "read" not in [s.strip() for s in shelves.split(",")]:
            continue
        if title and author:
            books.append({"title": title, "author": author, "date_read": date_read})
    print(f"RSS feed: {len(books)} items")
    return books


# ------------------------------------------------------------ country resolvers

def wikidata_entity_claims(entity_id, prop):
    data = http_get_json(
        f"{WIKIDATA_API}?action=wbgetclaims&entity={entity_id}&property={prop}&format=json"
    )
    return data.get("claims", {}).get(prop, [])


def wikidata_resolve_author(author):
    """Return {'country', 'iso_n3', 'source'} or None."""
    try:
        search = http_get_json(
            f"{WIKIDATA_API}?action=wbsearchentities&format=json&language=en&type=item&limit=3&search="
            + urllib.parse.quote(author)
        )
        for hit in search.get("search", []):
            qid = hit["id"]
            desc = (hit.get("description") or "").lower()
            # Prefer hits that look like people/writers; skip obvious non-humans
            if desc and not any(w in desc for w in
                                ("writer", "author", "novelist", "poet", "journalist",
                                 "philosopher", "historian", "essayist", "playwright",
                                 "born", "person", "professor", "academic")):
                continue
            claims = wikidata_entity_claims(qid, "P27")  # country of citizenship
            if not claims:
                continue
            country_qid = claims[0]["mainsnak"]["datavalue"]["value"]["id"]
            ent = http_get_json(
                f"{WIKIDATA_API}?action=wbgetentities&ids={country_qid}"
                "&props=labels|claims&languages=en&format=json"
            )["entities"][country_qid]
            label = ent["labels"]["en"]["value"]
            iso = None
            for c in ent.get("claims", {}).get("P299", []):  # ISO 3166-1 numeric
                iso = c["mainsnak"]["datavalue"]["value"]
                break
            if iso:
                return {"country": label, "iso_n3": iso.zfill(3), "source": "wikidata"}
    except Exception as e:
        print(f"  wikidata error for {author}: {e}", file=sys.stderr)
    return None


def gemini_resolve_author(author, book_title):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    prompt = (
        f'The author "{author}" wrote "{book_title}". What single country is this author '
        "most associated with (nationality/origin)? Respond with ONLY a JSON object, no "
        'markdown: {"country": "<English country name>", "iso_n3": "<3-digit ISO 3166-1 '
        'numeric code as string>"}. If truly unknown, use {"country": null, "iso_n3": null}.'
    )
    try:
        body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
        data = http_get_json(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            data=body,
        )
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
        parsed = json.loads(text)
        if parsed.get("country") and parsed.get("iso_n3"):
            return {"country": parsed["country"], "iso_n3": str(parsed["iso_n3"]).zfill(3),
                    "source": "gemini"}
    except Exception as e:
        print(f"  gemini error for {author}: {e}", file=sys.stderr)
    return None


# ------------------------------------------------------------------------- main

def main():
    config = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            config = json.load(f)

    # Merge CSV backfill + RSS, dedupe (RSS wins on date conflicts: it's fresher)
    merged = {}
    for b in load_csv() + load_rss(config.get("rss_url", "")):
        k = book_key(b["title"], b["author"])
        if k not in merged or (b["date_read"] and not merged[k]["date_read"]):
            merged[k] = b
    books = sorted(merged.values(), key=lambda b: b["date_read"] or "", reverse=True)
    if not books:
        print("No books found (no CSV and no/empty RSS) — leaving books.json untouched")
        return

    # Load author->country cache
    cache = {}
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            cache = json.load(f)

    unresolved = sorted({b["author"] for b in books} - set(cache))
    if unresolved:
        print(f"Resolving {len(unresolved)} new author(s)...")
    for author in unresolved:
        sample_title = next(b["title"] for b in books if b["author"] == author)
        result = wikidata_resolve_author(author) or gemini_resolve_author(author, sample_title)
        cache[author] = result or {"country": None, "iso_n3": None, "source": "unresolved"}
        print(f"  {author} -> {cache[author]['country']} ({cache[author]['source']})")
        time.sleep(0.5)  # be polite to the APIs

    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False, sort_keys=True)

    for b in books:
        info = cache.get(b["author"], {})
        b["country"] = info.get("country")
        b["iso_n3"] = info.get("iso_n3")

    out = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "books": books,
    }
    with open(BOOKS_PATH, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    n_countries = len({b["iso_n3"] for b in books if b["iso_n3"]})
    n_unresolved = sum(1 for b in books if not b["iso_n3"])
    print(f"Wrote {len(books)} books across {n_countries} countries "
          f"({n_unresolved} with unresolved author country)")


if __name__ == "__main__":
    main()
