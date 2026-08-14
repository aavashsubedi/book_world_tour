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
import unicodedata
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
    "Q27306":  ("Germany", "276"),         # Kingdom of Prussia
    "Q43287":  ("Germany", "276"),         # German Empire
    "Q41304":  ("Germany", "276"),         # Weimar Republic
    "Q7318":   ("Germany", "276"),         # Nazi Germany
    "Q161885": ("United Kingdom", "826"),  # Kingdom of Great Britain
    "Q174193": ("United Kingdom", "826"),  # UK of Great Britain and Ireland
    "Q172579": ("Italy", "380"),           # Kingdom of Italy
    "Q8733":   ("China", "156"),           # Qing dynasty
    "Q12560":  ("Turkey", "792"),          # Ottoman Empire
    "Q1747689": ("Italy", "380"),          # Ancient Rome
    "Q2277":    ("Italy", "380"),          # Roman Empire
    "Q17167":   ("Italy", "380"),          # Roman Republic
    "Q11772":   ("Greece", "300"),         # Ancient Greece
    "Q12544":   ("Turkey", "792"),         # Byzantine Empire
}

# Occupations that mark a search hit as the author we're after rather than a
# same-named painter, footballer or genus of birds.
WRITER_OCCUPATIONS = {
    "Q36180",    # writer
    "Q49757",    # poet
    "Q6625963",  # novelist
    "Q214917",   # playwright
    "Q1930187",  # journalist
    "Q11774202", # essayist
    "Q4964182",  # philosopher
    "Q201788",   # historian
    "Q333634",   # translator
    "Q28389",    # screenwriter
    "Q482980",   # author
    "Q4853732",  # children's writer
    "Q15980158", # non-fiction writer
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
    if not rss_url:
        print("No rss_url configured — CSV only (add one for daily auto-updates)")
        return books
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

def _claim_ids(entity, prop):
    """QIDs asserted by `prop` on `entity`, skipping novalue/somevalue snaks."""
    out = []
    for c in entity.get("claims", {}).get(prop, []):
        snak = c.get("mainsnak", {})
        if snak.get("snaktype") != "value":
            continue
        val = snak.get("datavalue", {}).get("value", {})
        if isinstance(val, dict) and "id" in val:
            out.append(val["id"])
    return out


_country_cache = {}


def _country_info(qid):
    """(name, iso_n3) for a country QID, or None if it has no usable code."""
    if qid in HISTORICAL_QID:
        return HISTORICAL_QID[qid]
    if qid in _country_cache:
        return _country_cache[qid]
    result = None
    ent = http_get_json(
        f"{WIKIDATA_API}?action=wbgetentities&ids={qid}"
        "&props=labels|claims&languages=en&format=json"
    )["entities"][qid]
    for c in ent.get("claims", {}).get("P299", []):  # ISO 3166-1 numeric
        snak = c.get("mainsnak", {})
        if snak.get("snaktype") == "value":
            iso = snak["datavalue"]["value"]
            name = ent.get("labels", {}).get("en", {}).get("value", qid)
            result = (name, str(iso).zfill(3))
            break
    _country_cache[qid] = result
    return result


def _name_key(name):
    """Comparable form of a name: no accents, no punctuation, no spacing.

    "M.L. Rio" and "M. L. Rio" both become "mlrio".
    """
    stripped = "".join(c for c in unicodedata.normalize("NFKD", name)
                       if not unicodedata.combining(c))
    return "".join(c for c in stripped.lower() if c.isalnum())


def _name_tokens(name):
    """Sorted name parts, so a reversed order still matches.

    Wikidata files some authors under their local convention — Min Jin Lee is
    labelled "Lee Min Jin".
    """
    return tuple(sorted(k for k in (_name_key(t) for t in re.split(r"[\s,]+", name)) if k))


def _names_match(a, b):
    return _name_key(a) == _name_key(b) or _name_tokens(a) == _name_tokens(b)


def _resolve_from_qids(qids, author):
    """Best human among these candidates -> {'country','iso_n3','source'} or None."""
    if not qids:
        return None
    # "mul" as well as "en": Wikidata moved names that are spelled the same in
    # every language to a shared multilingual label, leaving the English one
    # empty — Gabriel Garcia Marquez and John Wyndham both live there now.
    ents = http_get_json(
        f"{WIKIDATA_API}?action=wbgetentities&ids={'|'.join(qids)}"
        "&props=claims|descriptions|labels|aliases&languages=en|mul&format=json"
    )["entities"]

    # Humans whose name actually matches, writers first, search rank as the
    # tie-break. The name check is what stops a candidate with no country from
    # handing the query off to an unrelated person further down the list —
    # that is how "M.L. Rio" once resolved to Spain via Ramón Mercader.
    ranked = []
    for rank, qid in enumerate(qids):
        ent = ents.get(qid, {})
        if "Q5" not in _claim_ids(ent, "P31"):  # instance of: human
            continue
        names = []
        labels = ent.get("labels") or {}
        if isinstance(labels, dict):
            names += [(v or {}).get("value", "") for v in labels.values()]
        aliases = ent.get("aliases") or {}
        if isinstance(aliases, dict):
            for group in aliases.values():
                names += [a.get("value", "") for a in (group or [])]
        if not any(n and _names_match(n, author) for n in names):
            continue
        desc = ent.get("descriptions", {}).get("en", {}).get("value", "").lower()
        writes = bool(set(_claim_ids(ent, "P106")) & WRITER_OCCUPATIONS) or any(
            w in desc for w in ("writer", "author", "novelist", "poet",
                                "playwright", "essayist", "journalist"))
        ranked.append((0 if writes else 1, rank, qid, ent))
    ranked.sort()

    for _, _, qid, ent in ranked:
        citizenships = _claim_ids(ent, "P27")  # country of citizenship

        # 1. A modern state the author actually held citizenship of. Wilde is
        #    Irish, not "UK of Great Britain and Ireland".
        for country_qid in citizenships:
            if country_qid in HISTORICAL_QID:
                continue
            info = _country_info(country_qid)
            if info:
                return {"country": info[0], "iso_n3": info[1],
                        "source": "wikidata", "qid": qid}

        # 2. Birthplace. More precise than a defunct multinational empire —
        #    Slavici's only citizenship is Austria-Hungary, but he was born in
        #    Transylvania and is a Romanian writer. Also covers ancient and
        #    stateless authors, who have no citizenship at all.
        for place_qid in _claim_ids(ent, "P19"):  # place of birth
            place = http_get_json(
                f"{WIKIDATA_API}?action=wbgetentities&ids={place_qid}"
                "&props=claims&languages=en&format=json"
            )["entities"][place_qid]
            # A city's P17 lists every state that ever held it, oldest first —
            # Kyiv runs from Kievan Rus' to Ukraine. Take the modern one.
            place_countries = _claim_ids(place, "P17")
            for country_qid in place_countries:
                if country_qid in HISTORICAL_QID:
                    continue
                info = _country_info(country_qid)
                if info:
                    return {"country": info[0], "iso_n3": info[1],
                            "source": "wikidata-birthplace", "qid": qid}
            for country_qid in place_countries:
                if country_qid in HISTORICAL_QID:
                    name, iso = HISTORICAL_QID[country_qid]
                    return {"country": name, "iso_n3": iso,
                            "source": "wikidata-birthplace", "qid": qid}

        # 3. Last resort: the empire itself, mapped to a successor state.
        for country_qid in citizenships:
            if country_qid in HISTORICAL_QID:
                name, iso = HISTORICAL_QID[country_qid]
                return {"country": name, "iso_n3": iso,
                        "source": "wikidata-historical", "qid": qid}
    return None


# Goodreads uses these where an author is unknown; they match real Wikidata
# people of the same name, so never look them up.
NON_AUTHORS = {"anonymous", "unknown", "various", "variousauthors", "anon",
               "naauthor", "notavailable", "naa", "collective", "naauthors"}


def wikidata_resolve_author(author):
    """Return {'country', 'iso_n3', 'source'} or None.

    Uses the regular Wikidata API — the SPARQL endpoint is rate-limited far
    more aggressively. Candidates are ranked by us rather than trusted from
    search order, since plain search happily returns a painter or a genus of
    birds for names like "Homer" or "Sappho".
    """
    if _name_key(author) in NON_AUTHORS:
        return None
    try:
        search = http_get_json(
            f"{WIKIDATA_API}?action=wbsearchentities&format=json&language=en"
            "&type=item&limit=5&search=" + urllib.parse.quote(author)
        )
        result = _resolve_from_qids([h["id"] for h in search.get("search", [])], author)
        if result:
            return result

        # Label search ranks poorly for some names — "Min Jin Lee" returns three
        # ORCID researcher stubs and never the novelist. Full-text search ranks
        # by notability, so retry through it restricted to humans.
        query = urllib.parse.quote(f"{author} haswbstatement:P31=Q5")
        cirrus = http_get_json(
            f"{WIKIDATA_API}?action=query&list=search&srsearch={query}"
            "&srlimit=4&format=json"
        )
        return _resolve_from_qids(
            [h["title"] for h in cirrus.get("query", {}).get("search", [])], author)
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
