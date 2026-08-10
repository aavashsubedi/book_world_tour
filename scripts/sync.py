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
DATA_DIR = os.path.join(ROOT, "data")
CACHE_PATH = os.path.join(DATA_DIR, "countries.json")
CONFIG_PATH = os.path.join(ROOT, "config.json")

USER_AGENT = "BookWorldTour/1.0 (personal reading-map project; github.com)"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"

# Historical states -> their modern map successor (world-atlas has only current codes)
HISTORICAL_ISO = {
    "810": "643",  # Soviet Union -> Russia
    "200": "203",  # Czechoslovakia -> Czechia
    "890": "688",  # Yugoslavia -> Serbia
    "278": "276",  # East Germany -> Germany
    "280": "276",  # West Germany -> Germany
}

# Historical states with NO ISO code at all, by Wikidata QID -> (modern name, iso_n3).
# Without this, e.g. Tolstoy (Russian Empire) yields no ISO and a namesake wins instead.
HISTORICAL_QID = {
    "Q34266":  ("Russia", "643"),          # Russian Empire
    "Q15180":  ("Russia", "643"),          # Soviet Union
    "Q28513":  ("Austria", "040"),         # Austria-Hungary
    "Q131964": ("Austria", "040"),         # Austrian Empire
    "Q33946":  ("Czechia", "203"),         # Czechoslovakia
    "Q36704":  ("Serbia", "688"),          # Yugoslavia
    "Q172107": ("Germany", "276"),         # Kingdom of Prussia
    "Q43287":  ("Germany", "276"),         # German Empire
    "Q41304":  ("Germany", "276"),         # Weimar Republic
    "Q7318":   ("Germany", "276"),         # Nazi Germany
    "Q161885": ("United Kingdom", "826"),  # Kingdom of Great Britain
    "Q174193": ("United Kingdom", "826"),  # UK of Great Britain and Ireland
    "Q172579": ("Italy", "380"),           # Kingdom of Italy
    "Q8733":   ("China", "156"),           # Qing dynasty
    "Q13426199": ("Turkey", "792"),        # Ottoman Empire
}


def http_get_json(url, headers=None, data=None, timeout=30):
    """GET/POST JSON with retry + backoff (Wikidata 429s on bursts)."""
    for attempt, backoff in enumerate((15, 40, 70, 0)):
        try:
            req = urllib.request.Request(
                url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and backoff:
                print(f"  HTTP {e.code}, retrying in {backoff}s...", file=sys.stderr)
                time.sleep(backoff)
                continue
            raise


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

def load_csv(csv_path):
    books = []
    if not os.path.exists(csv_path):
        return books
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
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
        # The feed is already filtered by ?shelf=read; user_shelves only lists
        # *custom* shelves (favorites etc.), so keep those. Guard explicitly
        # against the other exclusive shelves in case the feed URL lacks ?shelf=.
        shelves = [s.strip() for s in (item.findtext("user_shelves") or "").split(",")]
        if "currently-reading" in shelves or "to-read" in shelves:
            continue
        if title and author:
            books.append({"title": title, "author": author, "date_read": date_read})
    print(f"RSS feed: {len(books)} items")
    return books


# ------------------------------------------------------------ country resolvers

def wikidata_resolve_author(author):
    """Return {'country', 'iso_n3', 'source'} or None.

    Single SPARQL query: search the name, keep humans (P31 Q5), take their
    country of citizenship (P27) and its ISO 3166-1 numeric code (P299).
    """
    sparql = """
    SELECT ?country ?countryLabel ?iso ?writer WHERE {
      SERVICE wikibase:mwapi {
        bd:serviceParam wikibase:endpoint "www.wikidata.org";
                        wikibase:api "EntitySearch";
                        mwapi:search %s;
                        mwapi:language "en".
        ?item wikibase:apiOutputItem mwapi:item.
        ?ordinal wikibase:apiOrdinal true.
      }
      ?item wdt:P31 wd:Q5; wdt:P27 ?country.
      OPTIONAL { ?country wdt:P299 ?iso. }
      OPTIONAL { ?item wdt:P106/wdt:P279* wd:Q36180. BIND(1 AS ?writer) }
      SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
    } ORDER BY DESC(?writer) ASC(?ordinal) LIMIT 5
    """ % json.dumps(author)
    try:
        data = http_get_json(
            "https://query.wikidata.org/sparql?format=json&query="
            + urllib.parse.quote(sparql),
            headers={"Accept": "application/sparql-results+json"},
        )
        # Rows are ordered writers-first, then by search rank. Take the first row
        # whose country has an ISO code or is a known historical state.
        for row in data["results"]["bindings"]:
            qid = row["country"]["value"].rsplit("/", 1)[-1]
            if qid in HISTORICAL_QID:
                name, iso = HISTORICAL_QID[qid]
                return {"country": name, "iso_n3": iso, "source": "wikidata"}
            if "iso" in row:
                return {"country": row["countryLabel"]["value"],
                        "iso_n3": row["iso"]["value"].zfill(3),
                        "source": "wikidata"}
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

def collect_person_books(person):
    """Merge a person's CSV backfill + RSS feed, deduped (fresher date wins)."""
    csv_path = os.path.join(DATA_DIR, f"goodreads_export_{person['id']}.csv")
    books = load_csv(csv_path)
    if not books and os.path.exists(os.path.join(DATA_DIR, "goodreads_export.csv")):
        books = load_csv(os.path.join(DATA_DIR, "goodreads_export.csv"))  # legacy name
    merged = {}
    for b in books + load_rss(person.get("rss_url", "")):
        k = book_key(b["title"], b["author"])
        if k not in merged or (b["date_read"] and not merged[k]["date_read"]):
            merged[k] = b
    return sorted(merged.values(), key=lambda b: b["date_read"] or "", reverse=True)


def save_cache(cache):
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False, sort_keys=True)


def resolve_authors(books, cache):
    """Fill the shared author->country cache for any author not yet resolved.

    Authors with no iso_n3 (source "unresolved") are retried each run — a later
    run may succeed, e.g. after a rate-limit blip or once GEMINI_API_KEY is
    added. Resolved entries (and hand-edits) are never re-queried or overwritten.
    """
    resolved = {a for a, info in cache.items() if info.get("iso_n3")}
    unresolved = sorted({b["author"] for b in books} - resolved)
    if unresolved:
        print(f"Resolving {len(unresolved)} new author(s)...", flush=True)
    for i, author in enumerate(unresolved, 1):
        sample_title = next(b["title"] for b in books if b["author"] == author)
        result = wikidata_resolve_author(author) or gemini_resolve_author(author, sample_title)
        if result:
            result["iso_n3"] = HISTORICAL_ISO.get(result["iso_n3"], result["iso_n3"])
        cache[author] = result or {"country": None, "iso_n3": None, "source": "unresolved"}
        print(f"  [{i}/{len(unresolved)}] {author} -> "
              f"{cache[author]['country']} ({cache[author]['source']})", flush=True)
        # Checkpoint: a long backfill can span hundreds of authors, and losing
        # them all to an interrupt or a job timeout would mean redoing the
        # lookups from scratch.
        if i % 10 == 0:
            save_cache(cache)
        time.sleep(1.5)  # be polite to the APIs


def main():
    config = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            config = json.load(f)
    people = config.get("people") or [
        {"id": "me", "name": "Me", "rss_url": config.get("rss_url", "")}]

    cache = {}
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            cache = json.load(f)

    for person in people:
        print(f"=== {person['name']} ({person['id']}) ===")
        books = collect_person_books(person)
        if not books:
            print("No books found (no CSV and no/empty RSS) — skipping")
            continue

        resolve_authors(books, cache)
        save_cache(cache)

        for b in books:
            info = cache.get(b["author"], {})
            b["country"] = info.get("country")
            b["iso_n3"] = info.get("iso_n3")

        out = {
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "person": person["name"],
            "books": books,
        }
        with open(os.path.join(DATA_DIR, f"books_{person['id']}.json"), "w") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)

        n_countries = len({b["iso_n3"] for b in books if b["iso_n3"]})
        n_unresolved = sum(1 for b in books if not b["iso_n3"])
        print(f"Wrote {len(books)} books across {n_countries} countries "
              f"({n_unresolved} with unresolved author country)")


if __name__ == "__main__":
    main()
