"""
Fase 4 — Geração de candidatos (spec ESPEC_CLAUDE_CODE.md §6).

Não usa pontos escolhidos à mão: gera pontos a cada 400m ao longo das vias
secondary/tertiary/residential de maior conectividade dentro do perímetro
urbano, descarta por regras duras (rede, setor sem domicílio/renda baixa) e
mantém os 300 melhores por um score preliminar barato (buffer de 1,5km).

Saída: data/processed/candidatos.gpkg
Roda sozinho: `python pipeline/f4_candidatos.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_PROCESSED, get_logger, load_config, log_resumo_fase,
)

LOGGER = get_logger("fase4_candidatos")

CLASSES_VIA_CANDIDATAS = ("secondary", "tertiary", "residential")


# ---------------------------------------------------------------------------
# Conectividade: grau de cada nó (ponto onde segmentos se tocam) — via bem
# conectada tem pontas que encontram várias outras vias, não é beco sem saída.
# ---------------------------------------------------------------------------

def _calcular_conectividade(vias: gpd.GeoDataFrame, precisao_m: float = 2.0) -> gpd.GeoDataFrame:
    vias = vias.copy()

    def _snap(pt):
        return (round(pt.x / precisao_m) * precisao_m, round(pt.y / precisao_m) * precisao_m)

    grau: dict[tuple, int] = {}
    extremos = []
    for geom in vias.geometry:
        coords = list(geom.coords)
        a, b = _snap(Point(coords[0])), _snap(Point(coords[-1]))
        extremos.append((a, b))
        grau[a] = grau.get(a, 0) + 1
        grau[b] = grau.get(b, 0) + 1

    vias["conectividade"] = [max(grau[a], grau[b]) for a, b in extremos]
    return vias


def _selecionar_vias_conectadas(vias: gpd.GeoDataFrame, perimetro: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    vias_candidatas = vias[vias["highway"].isin(CLASSES_VIA_CANDIDATAS)].copy()
    LOGGER.info("vias de classe candidata (secondary/tertiary/residential): %d de %d", len(vias_candidatas), len(vias))

    perimetro_union = perimetro.geometry.union_all()
    vias_candidatas = vias_candidatas[vias_candidatas.intersects(perimetro_union)]
    LOGGER.info("vias dentro do perímetro urbano: %d", len(vias_candidatas))

    vias_candidatas = _calcular_conectividade(vias_candidatas)
    mediana = vias_candidatas["conectividade"].median()
    vias_conectadas = vias_candidatas[vias_candidatas["conectividade"] >= mediana]
    LOGGER.info(
        "vias de maior conectividade (grau >= mediana=%.0f): %d de %d",
        mediana, len(vias_conectadas), len(vias_candidatas),
    )
    return vias_conectadas


# ---------------------------------------------------------------------------
# Pontos a cada 400m ao longo das vias selecionadas
# ---------------------------------------------------------------------------

def _gerar_pontos_ao_longo(vias: gpd.GeoDataFrame, espacamento_m: float) -> gpd.GeoDataFrame:
    pontos = []
    for geom in vias.geometry:
        comprimento = geom.length
        if comprimento < 1:
            continue
        n = max(1, int(comprimento // espacamento_m))
        for i in range(n + 1):
            d = min(i * espacamento_m, comprimento)
            pontos.append(geom.interpolate(d))
    gdf = gpd.GeoDataFrame(geometry=pontos, crs=vias.crs)
    gdf["_chave_coincidencia"] = gdf.geometry.apply(lambda p: (round(p.x, 0), round(p.y, 0)))
    gdf = gdf.drop_duplicates(subset="_chave_coincidencia").drop(columns="_chave_coincidencia")
    gdf = gdf.reset_index(drop=True)
    LOGGER.info("pontos gerados a cada %dm ao longo das vias selecionadas: %d (após remover coincidentes)", espacamento_m, len(gdf))
    return gdf


# ---------------------------------------------------------------------------
# Filtros duros
# ---------------------------------------------------------------------------

def _filtrar_candidatos(
    pontos: gpd.GeoDataFrame,
    concorrentes: gpd.GeoDataFrame,
    setores: gpd.GeoDataFrame,
    cfg: dict,
) -> tuple[gpd.GeoDataFrame, dict]:
    log_filtros = {"entrada": len(pontos)}

    redes = concorrentes[concorrentes["tipo"] == "rede"]
    raio_bloqueio = cfg["concorrencia"]["raio_bloqueio_rede_m"]
    if not redes.empty:
        dist_rede = np.minimum.reduce([pontos.geometry.distance(r.geometry).values for _, r in redes.iterrows()])
    else:
        dist_rede = np.full(len(pontos), np.inf)
    pontos = pontos.assign(dist_rede_mais_perto=dist_rede)
    sobrou_rede = pontos[pontos["dist_rede_mais_perto"] >= raio_bloqueio].copy()
    log_filtros["apos_filtro_rede"] = len(sobrou_rede)
    LOGGER.info("filtro rede (>=%dm): %d -> %d", raio_bloqueio, len(pontos), len(sobrou_rede))

    # zoneamento: sem camada vetorial (Fase 3 registrou a limitação) — não dá pra filtrar
    # por zona incompatível sem inventar polígono (spec §3.2/§6). Pendência manual por candidato.
    sobrou_rede["zoneamento_verificado"] = False

    juncao = gpd.sjoin_nearest(sobrou_rede, setores[["CD_SETOR", "NM_BAIRRO", "domicilios_ocupados", "renda_media_responsavel", "geometry"]],
                                how="left", distance_col="dist_setor")
    juncao = juncao[~juncao.index.duplicated(keep="first")]  # sjoin_nearest pode empatar

    tem_domicilio = juncao["domicilios_ocupados"].fillna(0) > 0
    piso_renda = cfg["candidatos"]["renda_minima_responsavel"]
    renda_ok = juncao["renda_media_responsavel"].isna() | (juncao["renda_media_responsavel"] >= piso_renda)
    sobrou_setor = juncao[tem_domicilio & renda_ok].copy()
    log_filtros["apos_filtro_setor"] = len(sobrou_setor)
    LOGGER.info(
        "filtro setor (domicílios>0 e renda>=%.0f ou desconhecida): %d -> %d",
        piso_renda, len(juncao), len(sobrou_setor),
    )

    return sobrou_setor, log_filtros


# ---------------------------------------------------------------------------
# Score preliminar barato: soma de domicílios em buffer euclidiano
# ---------------------------------------------------------------------------

def _score_preliminar(pontos: gpd.GeoDataFrame, setores: gpd.GeoDataFrame, buffer_m: float) -> gpd.GeoDataFrame:
    setores_centroides = setores.copy()
    setores_centroides["geometry"] = setores_centroides.geometry.centroid
    sindex = setores_centroides.sindex

    scores = []
    for geom in pontos.geometry:
        buffer = geom.buffer(buffer_m)
        candidatos_idx = list(sindex.query(buffer, predicate="intersects"))
        if not candidatos_idx:
            scores.append(0.0)
            continue
        sub = setores_centroides.iloc[candidatos_idx]
        dentro = sub[sub.geometry.within(buffer)]
        scores.append(dentro["domicilios_ocupados"].fillna(0).sum())
    pontos = pontos.assign(score_preliminar=scores)
    return pontos


# ---------------------------------------------------------------------------

def run() -> Path:
    cfg = load_config()
    espacamento = cfg["candidatos"]["espacamento_via_m"]
    buffer_m = cfg["candidatos"]["buffer_score_preliminar_m"]
    top_n = cfg["candidatos"]["top_n_preliminar"]

    LOGGER.info("=== Fase 4: geração de candidatos ===")

    vias = gpd.read_file(DATA_PROCESSED / "vias.gpkg")
    perimetro = gpd.read_file(DATA_PROCESSED / "perimetro_urbano.gpkg")
    concorrentes = gpd.read_file(DATA_PROCESSED / "concorrentes.gpkg")
    setores = gpd.read_file(DATA_PROCESSED / "setores.gpkg")

    vias_conectadas = _selecionar_vias_conectadas(vias, perimetro)
    pontos = _gerar_pontos_ao_longo(vias_conectadas, espacamento)

    sobrou, log_filtros = _filtrar_candidatos(pontos, concorrentes, setores, cfg)

    n_bairros = sobrou["NM_BAIRRO"].nunique()
    if len(sobrou) < 150 or n_bairros <= 8:
        LOGGER.warning(
            "ACEITE — só %d candidatos em %d bairros (mínimo: 150 em >8) — afrouxando filtro de renda e tentando de novo",
            len(sobrou), n_bairros,
        )
        cfg_afrouxado = dict(cfg)
        cfg_afrouxado["candidatos"] = {**cfg["candidatos"], "renda_minima_responsavel": 0}
        sobrou, log_filtros = _filtrar_candidatos(pontos, concorrentes, setores, cfg_afrouxado)
        n_bairros = sobrou["NM_BAIRRO"].nunique()
        LOGGER.info("após afrouxar: %d candidatos em %d bairros", len(sobrou), n_bairros)

    sobrou = _score_preliminar(sobrou, setores, buffer_m)
    sobrou = sobrou.sort_values("score_preliminar", ascending=False)
    top = sobrou.head(top_n).copy()
    top["candidato_id"] = [f"C{i:04d}" for i in range(1, len(top) + 1)]
    top = top[[
        "candidato_id", "NM_BAIRRO", "CD_SETOR", "score_preliminar",
        "dist_rede_mais_perto", "domicilios_ocupados", "renda_media_responsavel",
        "zoneamento_verificado", "geometry",
    ]].rename(columns={"NM_BAIRRO": "bairro"}).reset_index(drop=True)

    n_bairros_final = top["bairro"].nunique()
    if len(top) < 150:
        LOGGER.warning("ACEITE — mesmo após afrouxar, só %d candidatos sobreviveram (mínimo 150)", len(top))
    if n_bairros_final <= 8:
        LOGGER.warning("ACEITE — candidatos finais cobrem só %d bairros (mínimo >8)", n_bairros_final)
    if len(top) >= 150 and n_bairros_final > 8:
        LOGGER.info("ACEITE — %d candidatos em %d bairros distintos (mínimo 150/>8)", len(top), n_bairros_final)

    out_path = DATA_PROCESSED / "candidatos.gpkg"
    top.to_file(out_path, layer="candidatos", driver="GPKG")
    LOGGER.info("gravado: %s", out_path)

    log_resumo_fase(
        LOGGER, entrada=log_filtros["entrada"], saida=len(top),
        descartados=log_filtros["entrada"] - len(top),
        motivo_descarte="fora do perímetro/baixa conectividade, perto de rede, setor sem domicílio/renda baixa, ou fora do top-N preliminar",
    )
    return out_path


if __name__ == "__main__":
    run()
