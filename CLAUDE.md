# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

The pipeline described below is **built and has run end-to-end successfully** — `output/relatorio.html` and `output/top10.csv` exist and are current. `.venv/` (Windows) has all dependencies installed. Treat this as a working, iterating project, not a greenfield build: read the existing `pipeline/*.py` before touching a phase, and check `logs/pipeline.log` / the git history for what a phase actually does before re-deriving it from the spec.

- **`ESPEC_CLAUDE_CODE.md`** — the execution spec. Source of truth for folder layout, phase order, config keys, and acceptance criteria. Where this file and the spec disagree on a detail, the spec wins unless a code comment explains why an implementation choice deviated (several do — see "Known deviations from the spec" below).
- **`PLANO_ESCOLHA_DO_PONTO.md`** — business/methodology context (why each data layer matters, how the score is derived, the field checklist). Consult it when a design decision needs justification, or when writing report copy.

Both documents are in Portuguese; code comments, log messages, config, and the generated report all follow suit — keep new work consistent with that.

## What this project does

An autonomous pipeline that produces `output/relatorio.html` — a **self-contained HTML report** (opens by double-click, no server, no internet, no external deps) ranking the **top 10 candidate micro-regions** in Uberlândia/MG for a new pet-retail store, using IBGE census data, OSM/Google Places competitor data, drive-time isochrones, and locally-calibrated scoring weights. Includes an interactive Folium map, one detail sheet per finalist, and a printable visit checklist.

## Commands

```bash
# Windows venv — activate or call the interpreter directly
./.venv/Scripts/python.exe pipeline/f1_ibge.py     # ... through f9_relatorio.py, in order
make all              # runs all 9 phases in order
make clean-processed  # wipes data/processed/ + output/, keeps data/raw/ (cache) untouched
```

Each phase is also runnable standalone and is idempotent — reruns skip cached downloads (`data/raw/`) and re-derive `data/processed/` deterministically. After changing one phase, only that phase and everything downstream needs rerunning.

## Execution rules (non-negotiable, from spec §1)

1. **Idempotent.** Never re-download or re-compute what's already cached. Every download lands in `data/raw/` and is checked (existence + size) before re-fetching. Two known extra caches beyond the obvious: `data/raw/google_places/*.failed` marks a cell/term that exhausted retries so reruns don't re-hammer it; `data/raw/cdn_offline/` caches the Leaflet/jQuery/D3 assets Fase 9 inlines into the report.
2. **Phases are isolated scripts.** Each `pipeline/fN_*.py` runs standalone, reads from `data/`, writes to `data/`. No phase depends on another's in-memory state — only on files the earlier phase produced.
3. **Graceful degradation.** If a manual input (spec §5) is missing, skip the dependent step with a log warning and mark that indicator `"não coletado"` in the report. **The pipeline must never abort for missing manual data.**
4. **Structured logging.** Everything to `logs/pipeline.log` with timestamp, phase, action, record counts. Each phase ends with a summary: rows in, rows out, rows dropped and why.
5. **No fabricated data.** If a source fails, log the failure and move on. Never silently backfill with an estimate. Every number in the final report must be traceable to a source.
6. **Commit per phase.** On successful completion of a phase: `git commit -m "fase N: <descrição> — <n> registros"`.

## Environment

Actually installed in `.venv/` (see `requirements.txt` for exact pins) — the spec's own package list (§2) is missing two things needed in practice, both added:

```bash
python -m venv .venv && ./.venv/Scripts/python.exe -m pip install \
  pandas geopandas shapely pyproj fiona rtree \
  requests folium branca jinja2 \
  statsmodels scikit-learn \
  openpyxl chardet tqdm python-dotenv \
  pyyaml pyarrow \
  'markitdown[all]'
```
- `pyyaml` — needed to parse `config.yaml` at all; the spec lists the config format but not its parser.
- `pyarrow` — needed for `ranking_completo.parquet` (Fase 7 output); pandas' `to_parquet` has no engine without it.

No conda/GDAL system install needed — `pyogrio` (geopandas' default I/O backend) ships GDAL as a wheel on Windows.

`markitdown` is the standard reader for any non-tabular file (PDF/DOCX/XLSX/PPTX) — always convert before reading:
```bash
markitdown data/raw/<arquivo> -o data/interim/md/<arquivo>.md
```
Used for: the IBGE data dictionaries (`.xlsx`, to discover real column codes instead of guessing — see `data/interim/md/dicionario_*.md`), and the Uberlândia zoning map (`.pdf`).

`.env` (not versioned): `ORS_API_KEY` (openrouteservice.org) and `GOOGLE_PLACES_API_KEY`. Both in active use — Fase 5 needs ORS; Fase 2 falls back to OSM-only if the Google key is absent/rejected.

## Architecture — the pipeline

Nine sequential, independently runnable phases under `pipeline/`, each consuming files from `data/` and producing new files in `data/`:

| Phase | Script | Output | Purpose |
|---|---|---|---|
| 1 | `f1_ibge.py` | `data/processed/setores.gpkg` (1988 setores) | Census sector geometry + demographics for município 3170206. Discovers the latest IBGE file names by listing the FTP directory (never hardcoded) |
| 2 | `f2_concorrentes.py` | `data/processed/concorrentes.gpkg` (~305 registros) | Competitor pet shops/agropecuárias/vets from OSM + Google Places (New API), classified, deduplicated, chains resolved by address text match |
| 3 | `f3_vias_zoneamento.py` | `data/processed/vias.gpkg`, `zonas_permitidas.csv`, `perimetro_urbano.gpkg` | Road network (Overpass) + zoning legend extracted from the current zoning PDF; perimeter = IBGE "Urbana" sectors ∪ buffer around classified roads |
| 4 | `f4_candidatos.py` | `data/processed/candidatos.gpkg` (300 registros) | Points every 400 m along well-connected secondary/tertiary/residential streets, filtered by distance-to-chain/income floor, top-300 by cheap buffer score |
| 5 | `f5_isocronas.py` | `data/processed/candidatos_com_demanda.gpkg` | 5/10/15-min drive isochrones per candidate (ORS, one request per batch of 5 locations covers all 3 ranges); demand = area-weighted intersection with setores |
| 6 | `f6_calibracao.py` | `data/processed/pesos.json` | Regresses `log(1+avaliações)` of independent pet shops on their 1 km entorno; falls back to quartile-difference method when R² is weak (has been, both times run: R²≈0.09) |
| 7 | `f7_score.py` | `output/top10.csv`, `data/processed/ranking_completo.parquet` | Demand, competitive force (15-min isochrone), saturation, breakeven test, percentile score, 800 m geographic dedup — this last step is aggressive by design (see below) |
| 8 | `f8_manuais.py` | same two files, enriched | Joins `data/manual/*.csv` by `candidato_id`/bairro; **must** re-filter by `teste_absoluto_passou & ~duplicata_geografica` before re-picking Top 10, or suppressed duplicates leak back in |
| 9 | `f9_relatorio.py` | `output/relatorio.html`, `output/mapa.html` | Self-contained report + Folium map, fully offline (see below) |

### Known deviations from / additions to the spec

- **Fase 4's 800 m dedup pool is thin.** The top-300 candidates cluster hard in 2-3 dense bairros (Santa Mônica, Presidente Roosevelt, Centro), so Fase 7's 800 m dedup collapses 300→~12 survivors before picking the Top 10. This meets the letter of the aceite criteria (≥10 survive, no dup <800 m) but leaves little margin — if a future data change shrinks that pool below 10, Fase 7 will warn loudly in the log rather than fail silently.
- **IBGE income data is a separate product, not in `Agregados_por_Setores_Censitarios/`.** It's under `Agregados_por_Setores_Censitarios_Rendimento_do_Responsavel/` (a sibling FTP directory) — `f1_ibge.py` fetches both. Variable codes: `V06004` (mean), `V06006` (median).
- **Google Places legacy API (`maps.googleapis.com/maps/api/place/*`) is not enabled** on the configured key/project — `f2_concorrentes.py` uses **Places API (New)** (`places.googleapis.com/v1/places:searchText`) instead. Geocoding still uses the legacy `maps.googleapis.com/maps/api/geocode/json` (that one works).
- **The zoning map PDF URL in the spec is dead.** A new zoning law (LC 812/2026, Jan 2026) replaced it; `f3_vias_zoneamento.py` tries a short list of known-current URLs (newest first, found via web search at implementation time — there's no browsable directory index for this server, unlike IBGE's FTP). The zone *legend* (code + name) is extracted from the PDF's garbled text layer via regex; the *permitted-uses table* is not — it's in the law's text, which `leismunicipais.com.br` / `leis.org` block automated access to. Logged as a limitation, not guessed.
- **No vetorial zoning layer exists.** The Prefeitura's open-data/Mapas-e-Bairros pages return HTTP 403 to automated requests. `zonas.gpkg` from the spec's Fase 3 output line was never created — only `zonas_permitidas.csv` (legend only). Zoning compatibility per candidate stays a manual pendency, as the spec's own contingency plan anticipates.
- **`config.yaml` needed one value the spec references but never defines**: `candidatos.renda_minima_responsavel` (the plano's "~2 salários mínimos" floor). Set as an explicit R$ placeholder, flagged provisional, since no source in the pipeline carries the current official minimum wage.
- **Report offline-ness required real work, not just `tiles=None`.** Folium's default template pulls Leaflet/Bootstrap/jQuery/D3/FontAwesome from CDNs unconditionally. Fase 9 downloads each once (cached in `data/raw/cdn_offline/`) and inlines only what's actually exercised (Leaflet, jQuery, D3 — Choropleth's legend needs the latter two); Bootstrap/awesome-markers/FontAwesome/Glyphicons are stripped entirely since nothing here uses `folium.Icon` (DivIcon/CircleMarker throughout, precisely to avoid this dependency). The embedded Folium document(s) are wrapped in `<iframe srcdoc="...">`, never concatenated raw into the outer page — a raw concat nests `<html>` inside `<html>`, which is invalid and breaks both documents' CSS/JS. Verified with a real headless-browser pass (Playwright + system Edge): 0 console errors, 0 failed requests, map/legend/markers all render.
- **`av or 0` doesn't do what it looks like in Python** when `av` can be `NaN` (`NaN` is truthy) — bit Fase 9's competitor circle-marker radius once (NaN radius → invalid SVG path → console error). Use `float(av) if pd.notna(av) else 0.0` for anything from a numeric pandas column that might be missing.
- **GPKG reruns are byte-different but data-identical.** SQLite/GPKG serialization isn't byte-stable across writes even for identical data (verified: full row+geometry equality holds). Some Fase 7 float columns (`forca_concorrencia`, `saturacao`) also drift at the ~1e-5 relative level between runs — spatial-index query order affects float64 summation order. Neither changes the Top 10 selection or ranking order; both are noted here so nobody chases a phantom bug.

### Config (`config.yaml`)

Municipio code, CRS pair (geographic EPSG:4674 in, metric EPSG:31982 for all distance/area math), business economics, isochrone minutes, competition weights/radius, the three known chain locations, and `candidatos.*` (candidate generation params, including the income floor above). Several `negocio.*` values are explicitly provisional placeholders (`margem_por_saco_premium`, `taxa_posse_pet_domicilio`, `gasto_medio_mensal_por_pet`) — don't treat them as calibrated.

### Manual inputs (`data/manual/*.csv`)

Eight CSVs a human fills in over time — currently all still just headers (0/8 filled). See spec §5 for exact schemas. **None are required to produce the Top 10.** When `geoteste.csv` gets data, Fase 8 reorders the top 5 by measured cost-per-conversation. After filling any of these, rerun `f7_score.py` then `f8_manuais.py` then `f9_relatorio.py` (Fase 7 first if `imoveis.csv` changed — that's the one that feeds back into the breakeven test's cost basis, not just Fase 8's enrichment columns).

### Known data gotchas (spec §6, Fase 1) — verified against the real files, not assumed

- `CD_SETOR` / `CD_MUN` are **strings with leading zeros** — read with `dtype=str`.
- Encoding/decimal separator **varies by file, not uniformly "latin-1 + comma"** as the spec states: `basico_BR.csv` is cp1252 with comma-decimal; `domicilio1`/`demografia`/`renda_responsavel` CSVs are plain ASCII with period-decimal. `_common.read_ibge_csv()` detects per-file rather than assuming.
- Special values `X`, `-`, empty mean *suppressed for confidentiality*, not zero — convert to `NaN` (`_common.clean_numeric()`).
- The census mesh ships in EPSG:4674 — reprojected to EPSG:31982 in Fase 1, before any distance/area calculation downstream.
- Overpass's main instance (`overpass-api.de`) 406-rejects the default `python-requests` User-Agent — every outbound request in this pipeline sends a descriptive UA (`_common.DEFAULT_HEADERS`).

### What must stay manual (spec §7)

Never automate: Google Maps scraping, real-estate portal scraping, Meta Audience estimator, Google Ads Keyword Planner, iFood data, foot-traffic counts, mystery shopping, origin surveys, or calls to feed/med reps. The pipeline marks them as pending manual entries instead of attempting them.

### Pipeline acceptance criteria (spec §8) — status as of the last run

`output/relatorio.html` opens error-free (verified headless, see above) · Top 10 has exactly 10 rows, no dup <800 m · no Top 10 candidate <1.5 km from a chain · every report number traces to a Methodology source · Limitations section populated · `logs/pipeline.log` has no unhandled exception on a fresh run · reruns re-download nothing (verified: file count in `data/raw/` unchanged) and reproduce the same Top 10/ranking (see the GPKG/float-drift note above for the byte-level nuance) · `top10.csv` has the UTF-8 BOM + `;` separator.
