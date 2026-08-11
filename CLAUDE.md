# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

This repository currently contains **only planning/spec documents** — no `pipeline/` code, no `config.yaml`, no `data/` tree exist yet. The job is to build the pipeline described below from scratch, following the spec exactly. Do not invent structure that contradicts it.

- **`ESPEC_CLAUDE_CODE.md`** — the execution spec. Read this in full before writing any code; it is the source of truth for folder layout, phase order, config keys, and acceptance criteria.
- **`PLANO_ESCOLHA_DO_PONTO.md`** — business/methodology context (why each data layer matters, how the score is derived, the field checklist). Consult it when a design decision in the spec needs justification, or when writing report copy.

Both documents are in Portuguese; keep code, configs, logs, and the generated report consistent with that (variable names can be English/Portuguese as convenient, but log messages and report text should match the spec's language).

## What this project does

An autonomous pipeline that produces `output/relatorio.html` — a **self-contained HTML report** (opens by double-click, no server, no internet, no external deps) ranking the **top 10 candidate micro-regions** in Uberlândia/MG for a new pet-retail store, using IBGE census data, OSM/Google Places competitor data, drive-time isochrones, and locally-calibrated scoring weights. Includes an interactive Folium map, one detail sheet per finalist, and a printable visit checklist.

## Execution rules (non-negotiable, from spec §1)

1. **Idempotent.** Never re-download or re-compute what's already cached. Every download lands in `data/raw/` and is checked (existence + size) before re-fetching.
2. **Phases are isolated scripts.** Each `pipeline/fN_*.py` runs standalone, reads from `data/`, writes to `data/`. No phase depends on another's in-memory state — only on files the earlier phase produced.
3. **Graceful degradation.** If a manual input (spec §5) is missing, skip the dependent step with a log warning and mark that indicator `"não coletado"` in the report. **The pipeline must never abort for missing manual data.**
4. **Structured logging.** Everything to `logs/pipeline.log` with timestamp, phase, action, record counts. Each phase ends with a summary: rows in, rows out, rows dropped and why.
5. **No fabricated data.** If a source fails, log the failure and move on. Never silently backfill with an estimate. Every number in the final report must be traceable to a source.
6. **Commit per phase.** On successful completion of a phase: `git commit -m "fase N: <descrição> — <n> registros"`.

## Environment

```bash
python -m venv .venv && source .venv/bin/activate

pip install \
  pandas geopandas shapely pyproj fiona rtree \
  requests folium branca jinja2 \
  statsmodels scikit-learn \
  openpyxl chardet tqdm python-dotenv \
  'markitdown[all]'
```

`markitdown` is the standard reader for any non-tabular file (PDF/DOCX/XLSX/PPTX) — always convert before reading:
```bash
markitdown data/raw/<arquivo> -o data/interim/md/<arquivo>.md
```
Required for: the IBGE data dictionary (`.xlsx`, to discover real column codes instead of guessing), the Uberlândia zoning map/law (`.pdf`), and anything dropped in `data/inbox/`.

`.env` (not versioned): `ORS_API_KEY` (openrouteservice.org, free tier) and optional `GOOGLE_PLACES_API_KEY` (falls back to OSM-only when absent).

## Architecture — the pipeline

Nine sequential, independently runnable phases under `pipeline/`, each consuming files from `data/` and producing new files in `data/` (never mutating a prior phase's output in place):

| Phase | Script | Reads → Writes | Purpose |
|---|---|---|---|
| 1 | `f1_ibge.py` | IBGE FTP → `data/processed/setores.gpkg` | Census sector geometry + demographics (households, income, age, house-vs-apartment) for município 3170206 |
| 2 | `f2_concorrentes.py` | Overpass + Google Places → `data/processed/concorrentes.gpkg` | Competitor pet shops/agropecuárias/vets, classified and deduplicated |
| 3 | `f3_vias_zoneamento.py` | Overpass + Prefeitura PDF/GeoJSON → `data/processed/vias.gpkg`, `data/processed/zonas.gpkg` | Road network + zoning (vector if the city publishes it, PDF via markitdown as fallback, else marked as a manual-verification gap) |
| 4 | `f4_candidatos.py` | setores + concorrentes + vias → `data/processed/candidatos.gpkg` | Generates candidate points every 400 m along well-connected streets, filters by distance-to-chain/zoning/income floor, keeps the cheap-score top 300 |
| 5 | `f5_isocronas.py` | candidatos + OpenRouteService → `data/processed/candidatos_com_demanda.gpkg` | 5/10/15-min drive isochrones per candidate; resumable (cached by coordinate hash), can end in partial status if the daily quota runs out |
| 6 | `f6_calibracao.py` | concorrentes + setores → `data/processed/pesos.json` | Derives score weights from Uberlândia's own independent pet shops (OLS on `log(1+avaliações)`; falls back to quartile-difference method if n<25 or R²<0.25) |
| 7 | `f7_score.py` | candidatos_com_demanda + pesos.json | `output/top10.csv`, `data/processed/ranking_completo.parquet` | Demand estimate, competitive strength/saturation, breakeven test (hard filter), percentile-weighted score, 800 m geographic dedup |
| 8 | `f8_manuais.py` | `data/manual/*.csv` → merges into ranking | Joins any manual data present by `candidato_id`/bairro; missing indicators become `"não coletado"`, never a silent zero |
| 9 | `f9_relatorio.py` | everything above → `output/relatorio.html`, `output/mapa.html` | Builds the self-contained report: exec summary, sortable Top 10 table, per-finalist sheets, methodology (with weight-derivation confidence), Limitations section, and the "failed absolute test" list |

Once built, the intended commands are `python pipeline/f1_ibge.py` … `python pipeline/f9_relatorio.py`, plus a `make all` (run everything in order) and `make clean-processed` (wipe derived data, keep `data/raw/`) — see spec §6/§9 for exact expectations; create the `Makefile` as part of implementing the pipeline.

### Config (`config.yaml`)

Central place for municipio code, CRS pair (geographic EPSG:4674 in, metric EPSG:31982 for all distance/area math), business economics (rent ceiling, margin per bag, pet-ownership rate, capture-rate range, breakeven multiple), isochrone minutes, competition weights/radius, and the three known chain locations (Petz/Cobasi) used for the 1.5 km exclusion filter. Several values are explicitly provisional placeholders (`margem_por_saco_premium`, `taxa_posse_pet_domicilio`, `gasto_medio_mensal_por_pet`) meant to be replaced with real figures — don't treat them as calibrated.

### Manual inputs (`data/manual/*.csv`)

Eight CSVs a human fills in over time (imóveis, iFood, Meta público, Google buscas, contagem de fluxo, cliente oculto, geo-teste, marcas/carteira) — see spec §5 for exact schemas. **None are required to produce the Top 10**; the pipeline creates empty templates with headers on first run and logs which are pending. When `geoteste.csv` exists, it reorders the top 5 by measured cost-per-conversation — the only measured (vs. estimated) signal, and the report must say so explicitly.

### Known data gotchas (spec §6, Fase 1)

- `CD_SETOR` / `CD_MUN` are **strings with leading zeros** — read with `dtype=str`; letting them become ints silently breaks joins.
- IBGE CSVs are **latin-1/cp1252, `;`-separated, comma-decimal**. Detect encoding with `chardet`, never assume UTF-8.
- Special values `X`, `-`, empty mean *suppressed for confidentiality*, not zero — convert to `NaN`.
- The census mesh ships in EPSG:4674 — **reproject to EPSG:31982 before any distance/area calculation.**

### What must stay manual (spec §7)

Never automate: Google Maps scraping, real-estate portal scraping, Meta Audience estimator, Google Ads Keyword Planner, iFood data, foot-traffic counts, mystery shopping, origin surveys, or calls to feed/med reps. These require login, violate ToS, or are inherently field work — the pipeline marks them as pending manual entries instead of attempting them.

### Pipeline acceptance criteria (spec §8)

Before declaring a run complete, verify: `output/relatorio.html` opens error-free with no server; Top 10 has exactly 10 rows with no geographic duplicate within 800 m; no Top 10 candidate within 1.5 km of a chain; every number traces to a Methodology source; Limitations section is populated (or explicitly says nothing was missing); `logs/pipeline.log` has no unhandled exception; a second run re-downloads nothing and reproduces identical output; `output/top10.csv` opens correctly in Excel (UTF-8 with BOM, `;` separator).
