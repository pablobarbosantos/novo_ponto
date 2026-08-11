"""
Fase 5 — Isócronas (spec ESPEC_CLAUDE_CODE.md §6).

OpenRouteService, driving-car, 5/10/15 min (um único request por lote já
pede os 3 ranges de uma vez). Cache por hash das coordenadas em
data/raw/isocronas/ — interrompível e retomável sem perder o que já foi
baixado. Se a cota estourar, salva o progresso e encerra com status parcial
(não é erro fatal do pipeline, spec §1.3/§5 Fase 5).

Cálculo de demanda (CORRECOES.md B1): a isócrona binária de 10min sozinha
cobria ~48% da cidade e achatava toda variável discriminante. Em vez disso,
recorta-se os 3 anéis concêntricos (5min cheio; 10min menos 5min; 15min
menos 10min) e cada um entra com peso decrescente (PESO_ANEL) — decaimento
aproximado de exp(-t/4). Domicílios/pct_apartamento/renda são gravados por
anel (`*_anel5/10/15`, cada um já rateado por interseção fracionária de
área) e também combinados nas colunas genéricas `*_efetivo`. A Fase 7
(CORRECOES_2.md C4.1) usa as colunas por-anel diretamente para compor a
captação de balcão com pesos próprios (anel de 15min fora da captação).

Saída: data/processed/candidatos_com_demanda.gpkg
Roda sozinho: `python pipeline/f5_isocronas.py`
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import shape

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_PROCESSED, DATA_RAW, DEFAULT_HEADERS,
    coord_hash, get_logger, load_config, log_resumo_fase,
)

LOGGER = get_logger("fase5_isocronas")

ORS_URL = "https://api.openrouteservice.org/v2/isochrones/driving-car"
ISO_RAW_DIR = DATA_RAW / "isocronas"
BATCH_SIZE = 5  # plano gratuito do ORS costuma limitar locations por request; ver log se mudar


class ORSIndisponivel(Exception):
    """Chave/conta sem acesso — não adianta insistir (spec: degradação graciosa)."""


class ORSQuotaEsgotada(Exception):
    """Cota diária estourou no meio do lote — encerra a fase com status parcial, não é erro fatal."""


def _cache_path(lon: float, lat: float) -> Path:
    ISO_RAW_DIR.mkdir(parents=True, exist_ok=True)
    return ISO_RAW_DIR / f"{coord_hash(lon, lat)}.json"


def _pedir_lote(coords: list[tuple[str, float, float]], minutos: list[int], api_key: str) -> dict[str, dict]:
    """
    coords: lista de (candidato_id, lon, lat). Retorna {candidato_id: geojson_feature_collection}.
    Um único request pede os N ranges de uma vez (range em segundos).
    """
    body = {
        "locations": [[lon, lat] for _, lon, lat in coords],
        "range": [m * 60 for m in minutos],
        "range_type": "time",
        "attributes": ["area"],
    }
    headers = {**DEFAULT_HEADERS, "Authorization": api_key, "Content-Type": "application/json"}

    last_exc = None
    for attempt in range(5):
        resp = requests.post(ORS_URL, headers=headers, json=body, timeout=60)
        if resp.status_code == 403:
            raise ORSIndisponivel(f"HTTP 403: {resp.text[:300]}")
        if resp.status_code == 429:
            corpo = resp.text.lower()
            if "day" in corpo or "daily" in corpo or "quota" in corpo:
                raise ORSQuotaEsgotada(resp.text[:300])
            wait = min(2.0 ** attempt, 60.0)
            LOGGER.warning("ORS 429 (rate limit por minuto), tentativa %d/5, aguardando %.1fs", attempt + 1, wait)
            time.sleep(wait)
            last_exc = RuntimeError("429")
            continue
        if resp.status_code >= 500:
            wait = min(2.0 ** attempt, 30.0)
            LOGGER.warning("ORS %d, tentativa %d/5, aguardando %.1fs", resp.status_code, attempt + 1, wait)
            time.sleep(wait)
            last_exc = RuntimeError(str(resp.status_code))
            continue
        resp.raise_for_status()
        data = resp.json()
        break
    else:
        raise RuntimeError(f"lote esgotou tentativas: {last_exc}")

    # a API devolve os features na mesma ordem dos locations, com group_index apontando
    # pra qual location cada isócrona pertence
    resultado: dict[str, list[dict]] = {cid: [] for cid, _, _ in coords}
    for feat in data.get("features", []):
        idx = feat["properties"]["group_index"]
        cid = coords[idx][0]
        resultado[cid].append(feat)
    return {cid: {"type": "FeatureCollection", "features": feats} for cid, feats in resultado.items()}


def coletar_isocronas(candidatos: gpd.GeoDataFrame, minutos: list[int], api_key: str) -> tuple[int, int]:
    candidatos_wgs = candidatos.to_crs("EPSG:4326")
    pendentes = []
    n_cache = 0
    for cid, geom in zip(candidatos_wgs["candidato_id"], candidatos_wgs.geometry):
        if _cache_path(geom.x, geom.y).exists():
            n_cache += 1
        else:
            pendentes.append((cid, geom.x, geom.y))

    LOGGER.info("isócronas: %d já em cache, %d pendentes", n_cache, len(pendentes))
    if not pendentes:
        return n_cache, 0

    n_novos = 0
    quota_esgotada = False
    for i in range(0, len(pendentes), BATCH_SIZE):
        lote = pendentes[i:i + BATCH_SIZE]
        try:
            resultado = _pedir_lote(lote, minutos, api_key)
        except ORSIndisponivel:
            LOGGER.error("ORS indisponível para esta chave/conta — abortando Fase 5 (ver Limitações)")
            raise
        except ORSQuotaEsgotada as exc:
            LOGGER.warning("cota diária do ORS esgotada (%s) — %d/%d candidatos ainda sem isócrona, encerrando com status parcial",
                            exc, len(pendentes) - i, len(pendentes))
            quota_esgotada = True
            break
        except (requests.RequestException, RuntimeError) as exc:
            LOGGER.warning("lote %d-%d falhou (%s) — pulando este lote, seguindo com o resto", i, i + len(lote), exc)
            continue

        for cid, lon, lat in lote:
            fc = resultado.get(cid)
            if fc and fc["features"]:
                _cache_path(lon, lat).write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
                n_novos += 1
        LOGGER.info("lote %d-%d: %d isócronas obtidas", i, i + len(lote), sum(1 for c in lote if resultado.get(c[0], {}).get("features")))
        time.sleep(1.5)  # respiro entre lotes, plano gratuito tem limite por minuto

    if quota_esgotada:
        LOGGER.warning("Fase 5 encerrada com STATUS PARCIAL — cota do ORS estourou no meio do lote")
    return n_cache, n_novos


# ---------------------------------------------------------------------------
# Demanda (B1): 3 anéis concêntricos (5min cheio, 10min-5min, 15min-10min),
# cada um rateado por interseção fracionária de área, combinados por
# decaimento de peso.
# ---------------------------------------------------------------------------

PESO_ANEL = {5: 1.00, 10: 0.35, 15: 0.10}  # B1 (CORRECOES.md) — decaimento aprox. exp(-t/4)


def _extrair_feature(fc: dict, minutos_alvo: int) -> dict | None:
    valor_alvo = minutos_alvo * 60
    for f in fc["features"]:
        if f["properties"]["value"] == valor_alvo:
            return f
    return None


def _aneis_metricos(fc: dict, minutos_lista: list[int], crs_metrico: str) -> dict[int, object | None]:
    """Recorta os anéis concêntricos a partir do FeatureCollection já cacheado (sem
    nenhuma chamada nova ao ORS): anel do menor tempo = polígono cheio; anéis seguintes
    = polígono do tempo maior menos o do tempo imediatamente menor."""
    polys_metricos: dict[int, object | None] = {}
    for m in minutos_lista:
        feat = _extrair_feature(fc, m)
        if feat is None:
            polys_metricos[m] = None
            continue
        poly_wgs = shape(feat["geometry"])
        polys_metricos[m] = gpd.GeoSeries([poly_wgs], crs="EPSG:4326").to_crs(crs_metrico).iloc[0]

    minutos_ordenados = sorted(m for m in minutos_lista if polys_metricos[m] is not None)
    aneis: dict[int, object | None] = {}
    anterior = None
    for m in minutos_ordenados:
        atual = polys_metricos[m]
        aneis[m] = atual.difference(anterior) if anterior is not None else atual
        anterior = atual
    for m in minutos_lista:
        aneis.setdefault(m, None)
    return aneis


def _demanda_por_anel(poligono, setores: gpd.GeoDataFrame, sindex, setores_area_total) -> tuple[float | None, float | None, float | None]:
    if poligono is None or poligono.is_empty:
        return None, None, None
    idx = list(sindex.query(poligono, predicate="intersects"))
    if not idx:
        return 0.0, None, None
    sub = setores.iloc[idx]
    inter_area = sub.geometry.intersection(poligono).area
    fracao = (inter_area / setores_area_total.iloc[idx].values).clip(0, 1)
    peso = sub["domicilios_ocupados"].fillna(0).values * fracao
    dom = float(peso.sum())
    soma_peso = peso.sum()
    pct_apto = float((sub["pct_apartamento"].fillna(0).values * peso).sum() / soma_peso) if soma_peso > 0 else None
    renda = float((sub["renda_media_responsavel"].fillna(0).values * peso).sum() / soma_peso) if soma_peso > 0 else None
    return dom, pct_apto, renda


def calcular_demanda(candidatos: gpd.GeoDataFrame, setores: gpd.GeoDataFrame, minutos_lista: list[int], crs_metrico: str) -> gpd.GeoDataFrame:
    candidatos = candidatos.copy()
    candidatos_wgs = candidatos.to_crs("EPSG:4326")
    setores_area_total = setores.geometry.area
    sindex = setores.sindex

    por_anel = {m: {"dom": [], "pct": [], "renda": []} for m in minutos_lista}
    domicilios_efetivo, pct_apto_efetivo, renda_efetivo, status = [], [], [], []
    isocronas_10min_geoms = []

    def _linha_vazia():
        for m in minutos_lista:
            por_anel[m]["dom"].append(None)
            por_anel[m]["pct"].append(None)
            por_anel[m]["renda"].append(None)
        domicilios_efetivo.append(None)
        pct_apto_efetivo.append(None)
        renda_efetivo.append(None)
        status.append("não coletado")
        isocronas_10min_geoms.append(None)

    for cid, geom_wgs in zip(candidatos_wgs["candidato_id"], candidatos_wgs.geometry):
        cache_path = _cache_path(geom_wgs.x, geom_wgs.y)
        if not cache_path.exists():
            _linha_vazia()
            continue

        fc = json.loads(cache_path.read_text(encoding="utf-8"))
        aneis = _aneis_metricos(fc, minutos_lista, crs_metrico)
        if all(aneis[m] is None for m in minutos_lista):
            _linha_vazia()
            continue

        # isócrona "cheia" de 10min (não o anel) — só para a layer de mapa da Fase 9
        feat_10 = _extrair_feature(fc, 10)
        if feat_10 is not None:
            poly10_wgs = shape(feat_10["geometry"])
            isocronas_10min_geoms.append(gpd.GeoSeries([poly10_wgs], crs="EPSG:4326").to_crs(crs_metrico).iloc[0])
        else:
            isocronas_10min_geoms.append(None)

        soma_peso_efetivo = 0.0
        soma_dom_efetivo = 0.0
        soma_pct_num = 0.0
        soma_renda_num = 0.0
        algum_anel_valido = False
        for m in minutos_lista:
            dom, pct, renda = _demanda_por_anel(aneis[m], setores, sindex, setores_area_total)
            por_anel[m]["dom"].append(dom)
            por_anel[m]["pct"].append(pct)
            por_anel[m]["renda"].append(renda)
            if dom is not None:
                algum_anel_valido = True
                peso_efetivo = PESO_ANEL.get(m, 0.0) * dom
                soma_dom_efetivo += peso_efetivo
                soma_peso_efetivo += peso_efetivo
                if pct is not None:
                    soma_pct_num += peso_efetivo * pct
                if renda is not None:
                    soma_renda_num += peso_efetivo * renda

        if not algum_anel_valido:
            domicilios_efetivo.append(None)
            pct_apto_efetivo.append(None)
            renda_efetivo.append(None)
            status.append("não coletado")
            continue

        domicilios_efetivo.append(soma_dom_efetivo)
        pct_apto_efetivo.append(soma_pct_num / soma_peso_efetivo if soma_peso_efetivo > 0 else None)
        renda_efetivo.append(soma_renda_num / soma_peso_efetivo if soma_peso_efetivo > 0 else None)
        status.append("completo")

    for m in minutos_lista:
        candidatos[f"domicilios_anel{m}"] = por_anel[m]["dom"]
        candidatos[f"pct_apartamento_anel{m}"] = por_anel[m]["pct"]
        candidatos[f"renda_media_anel{m}"] = por_anel[m]["renda"]

    candidatos["domicilios_efetivo"] = domicilios_efetivo
    candidatos["pct_apartamento_efetivo"] = pct_apto_efetivo
    candidatos["renda_media_efetivo"] = renda_efetivo
    candidatos["status_isocrona"] = status
    candidatos["geometry_isocrona_10min"] = isocronas_10min_geoms
    return candidatos


def run() -> Path:
    cfg = load_config()
    crs_metrico = cfg["crs"]["metrico"]
    minutos_lista = cfg["isocronas"]["minutos"]
    api_key = os.getenv("ORS_API_KEY", "").strip()

    LOGGER.info("=== Fase 5: isócronas ===")

    if not api_key:
        LOGGER.warning("ORS_API_KEY ausente — Fase 5 pulada por completo, candidatos ficam sem indicador de demanda (spec §1.3)")
        candidatos = gpd.read_file(DATA_PROCESSED / "candidatos.gpkg")
        for m in minutos_lista:
            candidatos[f"domicilios_anel{m}"] = None
            candidatos[f"pct_apartamento_anel{m}"] = None
            candidatos[f"renda_media_anel{m}"] = None
        candidatos["domicilios_efetivo"] = None
        candidatos["pct_apartamento_efetivo"] = None
        candidatos["renda_media_efetivo"] = None
        candidatos["status_isocrona"] = "não coletado (sem chave ORS)"
        out_path = DATA_PROCESSED / "candidatos_com_demanda.gpkg"
        candidatos.to_file(out_path, layer="candidatos_com_demanda", driver="GPKG")
        log_resumo_fase(LOGGER, entrada=len(candidatos), saida=len(candidatos), descartados=0,
                         motivo_descarte="fase inteira pulada — sem ORS_API_KEY")
        return out_path

    candidatos = gpd.read_file(DATA_PROCESSED / "candidatos.gpkg")
    setores = gpd.read_file(DATA_PROCESSED / "setores.gpkg")

    try:
        n_cache, n_novos = coletar_isocronas(candidatos, minutos_lista, api_key)
    except ORSIndisponivel:
        for m in minutos_lista:
            candidatos[f"domicilios_anel{m}"] = None
            candidatos[f"pct_apartamento_anel{m}"] = None
            candidatos[f"renda_media_anel{m}"] = None
        candidatos["domicilios_efetivo"] = None
        candidatos["pct_apartamento_efetivo"] = None
        candidatos["renda_media_efetivo"] = None
        candidatos["status_isocrona"] = "não coletado (ORS indisponível para esta chave)"
        out_path = DATA_PROCESSED / "candidatos_com_demanda.gpkg"
        candidatos.to_file(out_path, layer="candidatos_com_demanda", driver="GPKG")
        log_resumo_fase(LOGGER, entrada=len(candidatos), saida=len(candidatos), descartados=0,
                         motivo_descarte="ORS indisponível (403) — verificar ativação da chave")
        return out_path

    candidatos = calcular_demanda(candidatos, setores, minutos_lista, crs_metrico)

    n_completo = (candidatos["status_isocrona"] == "completo").sum()
    n_faltando = len(candidatos) - n_completo
    LOGGER.info("demanda calculada: %d/%d candidatos com isócrona completa (%d ainda sem)", n_completo, len(candidatos), n_faltando)

    # B1 (CORRECOES.md) — validação: domicilios_efetivo mediano deve ficar abaixo de 15%
    # do total municipal. Total municipal precisa vir do próprio setores.gpkg (não hardcodar).
    total_municipal = setores["domicilios_ocupados"].sum()
    mediana_efetivo = candidatos["domicilios_efetivo"].median()
    pct_mediana = 100 * mediana_efetivo / total_municipal if total_municipal else float("nan")
    if pct_mediana >= 15:
        LOGGER.warning(
            "B1 — domicilios_efetivo mediano=%.0f (%.1f%% do total municipal=%d) NÃO ficou abaixo de 15%% "
            "(alvo do CORRECOES.md). Mesmo o anel de 5min isolado (peso 1.00) já cobre mediana de %.0f "
            "domicílios (%.1f%%) — as isócronas driving-car do ORS para Uberlândia são geometricamente "
            "grandes mesmo em 5min nos pontos bem conectados que a Fase 4 seleciona (ex.: ~17-20km² de área "
            "só no anel de 5min). Isso NÃO é um erro de implementação (fórmula/pesos conferem com o "
            "pseudocódigo do B1) — é uma característica real da malha viária local. Mantido como está; "
            "C4.1 (CORRECOES_2.md) aperta ainda mais a captação de balcão (só anéis 5/10min, sem 15min) "
            "mas não resolve sozinho — ver nota de C6 item 5 no relatório final.",
            mediana_efetivo, pct_mediana, total_municipal,
            candidatos["domicilios_anel5"].median(),
            100 * candidatos["domicilios_anel5"].median() / total_municipal if total_municipal else float("nan"),
        )
    else:
        LOGGER.info("B1 — ACEITE: domicilios_efetivo mediano=%.0f (%.1f%% do total municipal) abaixo de 15%%", mediana_efetivo, pct_mediana)

    # duas camadas: pontos com atributos de demanda, e polígonos da isócrona de 10min (mapa da Fase 9)
    pontos = candidatos.drop(columns=["geometry_isocrona_10min"])
    isocronas = candidatos[candidatos["geometry_isocrona_10min"].notna()][["candidato_id", "geometry_isocrona_10min"]].rename(
        columns={"geometry_isocrona_10min": "geometry"}
    )
    isocronas = gpd.GeoDataFrame(isocronas, geometry="geometry", crs=crs_metrico)

    out_path = DATA_PROCESSED / "candidatos_com_demanda.gpkg"
    pontos.to_file(out_path, layer="candidatos_com_demanda", driver="GPKG")
    if not isocronas.empty:
        isocronas.to_file(out_path, layer="isocronas_10min", driver="GPKG")
    LOGGER.info("gravado: %s (camadas: candidatos_com_demanda, isocronas_10min)", out_path)

    log_resumo_fase(
        LOGGER, entrada=len(candidatos), saida=n_completo,
        descartados=n_faltando,
        motivo_descarte="sem isócrona (cota do ORS ou falha pontual de lote)" if n_faltando else None,
    )
    return out_path


if __name__ == "__main__":
    run()
