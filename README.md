# 📚 Book World Tour

A world map of your reading: every country lights up when you've read a book by
an author from there. One site can host several people's maps — toggle between
them in the top left. Free to host (GitHub Pages), free to sync (GitHub
Actions), no server, no login.

**Note: this repo is public** — everyone's reading lists (`data/*.json`, and
any Goodreads CSVs you commit) are visible to anyone.

## Setup (one time)

1. **Push this repo to GitHub** (public repo).
2. **Enable GitHub Pages**: repo → Settings → Pages → Source: *Deploy from a
   branch* → `main` / root.
3. **Add people** in `config.json` — one entry per person. The `rss_url` comes
   from their Goodreads profile: `https://www.goodreads.com/review/list_rss/<goodreads-user-id>?shelf=read`
   (their shelf must be public):

   ```json
   { "people": [
     { "id": "aavash", "name": "Aavash", "rss_url": "https://www.goodreads.com/review/list_rss/77277889?shelf=read" }
   ] }
   ```
4. **Backfill full history** (optional but recommended — RSS only covers the
   ~100 most recent books): Goodreads → My Books → Import/Export → Export
   Library, save as `data/goodreads_export_<id>.csv` (matching the person's
   `id`), commit and push. The sync workflow runs automatically on that push.
5. **(Optional) Gemini fallback**: most authors resolve via Wikidata for free.
   For the rest, add a free Gemini API key as a repo secret named
   `GEMINI_API_KEY` (repo → Settings → Secrets and variables → Actions).

The map is then live at `https://<you>.github.io/book_world_tour/` and updates
itself once a day. Your last-viewed person is remembered in the browser, and
`?u=<id>` deep-links to someone's map.

## How it works

```
config.json (people + RSS urls) ──┐
data/goodreads_export_<id>.csv ───┼─→ scripts/sync.py ─→ data/books_<id>.json ─→ index.html
                                  │            │
                                  │            └─→ data/countries.json (shared author→country cache)
                                  └─ .github/workflows/sync.yml (daily cron)
```

- `scripts/sync.py` (stdlib-only Python) merges each person's CSV backfill with
  their RSS feed, then resolves each **new** author's country via Wikidata
  (country of citizenship → ISO 3166-1 numeric code), falling back to Gemini if
  configured. The cache is shared across people, so each author is looked up
  once ever.
- `.github/workflows/sync.yml` runs daily (and on CSV changes) and commits the
  result; GitHub Pages redeploys automatically.
- `index.html` renders the selected person's `books_<id>.json` as a choropleth
  (D3 + TopoJSON, darker blue = more books), with hover tooltips listing the
  books per country, and light/dark theme support.

Authors whose country can't be resolved are counted in the book total but not
on the map; they appear in `data/countries.json` with `"source": "unresolved"`
and are retried on each run. You can hand-edit that file to fix or override any
author — resolved entries are never overwritten.

## Visiting maps on other sites

`?src=<https URL of a books_*.json on someone else's GitHub Pages>` renders any
remote map in this UI (GitHub Pages serves JSON with open CORS headers).
