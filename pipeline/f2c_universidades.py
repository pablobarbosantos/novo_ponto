"""
Fase 2c — Polígonos de campus universitário (C3, CORRECOES_2.md).

Santa Mônica pontuou alto por renda e verticalização — mas é o bairro do campus
da UFU. República e apartamento de estudante contam como domicílio em
apartamento, mas não são domicílio com pet nem comprador de ração premium.
Este módulo só coleta os polígonos (via OSM `amenity=university`); o desconto
em si (pct_18a24 alto + perto de campus → fator_verticalizacao × 0.6) é
aplicado em f7_score.py::_demanda_estimada.

Mesmo padrão de fetch do Overpass já usado em f2_concorrentes.py — endpoints,
retry/backoff e cache em disco idênticos, cache em arquivo próprio (não reusa
overpass_concorrentes.json).

Saída: data/processed/universidades.gpkg
Roda sozinho: `python pipeline/f2c_universidades.py`
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import Point, shape

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_PROCESSED, DATA_RAW, DEFAULT_HEADERS,
    get_logger, load_config, log_resumo_fase,
)

LOGGER = get_logger("fase2c_universidades")

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
OSM_RAW_DIR = DATA_RAW / "osm"

OVERPASS_QUERY = """
[out:json][timeout:90];
area["name"="Uberlândia"]["admin_level"="8"]->.a;
(
  node["amenity"="university"](area.a);
  way["amenity"="university"](area.a);
  relation["amenity"="university"](area.a);
);
out center;
""".strip()


def _overpass_fetch() -> dict:
    OSM_RAW_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = OSM_RAW_DIR / "overpass_universidades.json"
    if cache_path.exists() and cache_path.stat().st_size > 50:
        LOGGER.info("cache hit Overpass: %s", cache_path)
        return json.loads(cache_path.read_text(encoding="utf-8"))

    last_error = None
    for endpoint in OVERPASS_ENDPOINTS:
        for attempt in range(4):
            try:
                LOGGER.info("Overpass (universidades): tentando %s (tentativa %d)", endpoint, attempt + 1)
                resp = requests.post(endpoint, data={"data": OVERPASS_QUERY}, timeout=100, headers=DEFAULT_HEADERS)
                if resp.status_code == 429 or resp.status_code >= 500:
                    wait = 2.0 ** attempt
                    LOGGER.warning("Overpass %s respondeu %d, aguardando %.1fs", endpoint, resp.status_code, wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                data = resp.json()
                if "elements" not in data:
                    raise ValueError("resposta sem 'elements'")
                cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                LOGGER.info("Overpass OK via %s: %d elementos", endpoint, len(data["elements"]))
                return data
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                wait = 2.0 ** attempt
                LOGGER.warning("Overpass %s falhou (%s), aguardando %.1fs", endpoint, exc, wait)
                time.sleep(wait)
    raise RuntimeError(f"Overpass indisponível em todos os endpoints testados: {last_error}")


def _elemento_para_geometria(el: dict):
    """node -> ponto; way/relation com 'center' (out center) -> ponto no centroide. Sem
    geometria de polígono completo (spec do Overpass usada é 'out center', mais leve) —
    suficiente pra C3, que só precisa de distância candidato<->campus, não da área exata."""
    if el["type"] == "node":
        lat, lon = el.get("lat"), el.get("lon")
    else:
        center = el.get("center", {})
        lat, lon = center.get("lat"), center.get("lon")
    if lat is None or lon is None:
        return None
    return Point(lon, lat)


def coletar_universidades(crs_metrico: str) -> gpd.GeoDataFrame:
    data = _overpass_fetch()
    rows = []
    for el in data["elements"]:
        geom = _elemento_para_geometria(el)
        if geom is None:
            continue
        tags = el.get("tags", {})
        rows.append({
            "osm_id": f"{el['type']}/{el['id']}",
            "nome": tags.get("name", "(sem nome)"),
            "geometry": geom,
        })
    if not rows:
        LOGGER.warning("nenhum campus universitário encontrado via Overpass (amenity=university) — C3 ficará sem efeito (sem geometria de campus pra medir distância)")
        return gpd.GeoDataFrame(columns=["osm_id", "nome", "geometry"], geometry="geometry", crs=crs_metrico)
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326").to_crs(crs_metrico)
    LOGGER.info("universidades: %d campus/unidades encontrados: %s", len(gdf), sorted(gdf["nome"].unique().tolist()))
    return gdf


def run() -> Path:
    cfg = load_config()
    crs_metrico = cfg["crs"]["metrico"]
    LOGGER.info("=== Fase 2c: campus universitários (C3) ===")

    gdf = coletar_universidades(crs_metrico)

    out_path = DATA_PROCESSED / "universidades.gpkg"
    gdf.to_file(out_path, layer="universidades", driver="GPKG")
    LOGGER.info("gravado: %s (%d registros)", out_path, len(gdf))

    log_resumo_fase(LOGGER, entrada=len(gdf), saida=len(gdf), descartados=0)
    return out_path


if __name__ == "__main__":
    run()
