# 📚 Book World Tour

A world map of your reading: every country lights up when you've read a book by
an author from there. Free to host (GitHub Pages), free to sync (GitHub
Actions), no server, no login.

**Note: this repo is public** — your reading list (`data/*.json`, and the
Goodreads CSV if you commit it) is visible to anyone. That's also what makes
the "visit a friend" feature work.

## Setup (one time)

1. **Push this repo to GitHub** (public repo).
2. **Enable GitHub Pages**: repo → Settings → Pages → Source: *Deploy from a
   branch* → `main` / root.
3. **Backfill your history**: on Goodreads go to *My Books → Import/Export →
   Export Library*, save the file as `data/goodreads_export.csv`, commit and
   push. The sync workflow runs automatically on that push.
4. **Daily sync via RSS**: on your Goodreads *read* shelf page, copy the RSS
   link (icon at the bottom of the page) and paste it into `config.json` as
   `rss_url`. This keeps the map fresh without re-exporting.
5. **(Optional) Gemini fallback**: most authors resolve via Wikidata for free.
   For obscure ones, add a free Gemini API key as a repo secret named
   `GEMINI_API_KEY` (repo → Settings → Secrets and variables → Actions).

The map is then live at `https://<you>.github.io/book_world_tour/` and updates
itself once a day.

## Visit a friend

Anyone with their own copy of this project can be "visited" — append their data
URL to your page:

```
https://<you>.github.io/book_world_tour/?src=https://<friend>.github.io/book_world_tour/data/books.json
```

Same map UI, their reading data. Works because GitHub Pages serves JSON with
open CORS headers.

## How it works

```
Goodreads CSV export ─┐
                      ├─→ scripts/sync.py ─→ data/books.json ─→ index.html (map)
Goodreads RSS feed  ──┘         │
                                └─→ data/countries.json (author→country cache)
```

- `scripts/sync.py` (stdlib-only Python) merges the CSV backfill with the RSS
  feed, then resolves each **new** author's country via Wikidata (country of
  citizenship → ISO 3166-1 numeric code), falling back to Gemini if configured.
  Results are cached in `data/countries.json`, so each author is looked up once
  ever.
- `.github/workflows/sync.yml` runs it daily (and on CSV changes) and commits
  the result; GitHub Pages redeploys automatically.
- `index.html` renders `data/books.json` as a choropleth (D3 + TopoJSON,
  darker blue = more books), with hover tooltips listing the books per country
  and light/dark theme support.

Authors whose country can't be resolved are counted in the book total but not
on the map; they appear in `data/countries.json` with `"source": "unresolved"`
— you can hand-edit that file to fix or override any author (edits persist; the
sync never overwrites an existing entry).
