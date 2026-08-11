"""
Fase 7 — Score e filtros (spec ESPEC_CLAUDE_CODE.md §6).

7.1 Demanda estimada · 7.2 Força da concorrência (isócrona de 15min) ·
7.3 Saturação · 7.4 Ponto de equilíbrio · 7.5 Teste absoluto (filtro duro) ·
7.6 Score final (percentil, pesos da Fase 6) · 7.7 Dedup geográfica (800m).

Roda ANTES da Fase 8 (entradas manuais) — por isso `imoveis.csv` ainda está
vazio neste ponto do pipeline na primeira rodada. Isso não é um bug: spec
§1.3 manda degradar graciosamente, e a Fase 8 reprocessa/anexa depois sem
precisar refazer nada daqui. "Ausente" nunca vira zero silencioso — vira
sinalização explícita (spec §6 Fase 7.6).

Saída: output/top10.csv (UTF-8 BOM, ';'), data/processed/ranking_completo.parquet
Roda sozinho: `python pipeline/f7_score.py`
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_MANUAL, DATA_PROCESSED, DATA_RAW, OUTPUT_DIR,
    coord_hash, get_logger, load_config, log_resumo_fase,
)

LOGGER = get_logger("fase7_score")

ISO_RAW_DIR = DATA_RAW / "isocronas"
RAIO_DEDUP_M = 800


# ---------------------------------------------------------------------------
# 7.1 — Demanda estimada
# ---------------------------------------------------------------------------

def _demanda_estimada(cand: pd.DataFrame, cfg: dict) -> pd.Series:
    taxa_posse = cfg["negocio"]["taxa_posse_pet_domicilio"]
    gasto_medio = cfg["negocio"]["gasto_medio_mensal_por_pet"]
    fator_verticalizacao = 1.0 + 0.4 * cand["pct_apartamento_10min"].fillna(0)
    potencial = cand["domicilios_10min"] * taxa_posse * fator_verticalizacao * gasto_medio
    return potencial.where(cand["domicilios_10min"].notna())  # sem isócrona => sem demanda calculável, não 0


# ---------------------------------------------------------------------------
# 7.2 — Força da concorrência (isócrona de 15 min)
# ---------------------------------------------------------------------------

def _carregar_isocrona_15min(lon: float, lat: float, crs_metrico: str):
    from shapely.geometry import shape
    cache_path = ISO_RAW_DIR / f"{coord_hash(lon, lat)}.json"
    if not cache_path.exists():
        return None
    fc = json.loads(cache_path.read_text(encoding="utf-8"))
    for feat in fc["features"]:
        if feat["properties"]["value"] == 900.0:  # 15 min em segundos
            poly_wgs = shape(feat["geometry"])
            return gpd.GeoSeries([poly_wgs], crs="EPSG:4326").to_crs(crs_metrico).iloc[0]
    return None


def _forca_concorrencia(cand: gpd.GeoDataFrame, concorrentes: gpd.GeoDataFrame, cfg: dict, crs_metrico: str) -> pd.DataFrame:
    peso_rede = cfg["concorrencia"]["peso_rede"]
    peso_independente = cfg["concorrencia"]["peso_independente"]
    avaliacoes = pd.to_numeric(concorrentes["avaliacoes"], errors="coerce").fillna(0.0)
    # sem nº de avaliações não dá pra medir a "força" desse concorrente (é o numerador da
    # fórmula) — conta como 0 de força, não inventa um número; fica sinalizado à parte.
    sem_avaliacao = pd.to_numeric(concorrentes["avaliacoes"], errors="coerce").isna()
    peso_tipo = np.where(concorrentes["tipo"] == "rede", peso_rede, peso_independente)
    sindex = concorrentes.sindex

    cand_wgs = cand.to_crs("EPSG:4326")
    resultados = []
    for cid, geom_metrico, lon, lat in zip(cand["candidato_id"], cand.geometry, cand_wgs.geometry.x, cand_wgs.geometry.y):
        iso15 = _carregar_isocrona_15min(lon, lat, crs_metrico)
        if iso15 is None:
            resultados.append({"candidato_id": cid, "forca_concorrencia": None, "n_concorrentes_15min": None, "n_sem_avaliacao_15min": None, "n_clinicas_sem_loja": None})
            continue
        idx = list(sindex.query(iso15, predicate="intersects"))
        sub = concorrentes.iloc[idx]
        dentro = sub[sub.geometry.within(iso15)]
        if dentro.empty:
            resultados.append({"candidato_id": cid, "forca_concorrencia": 0.0, "n_concorrentes_15min": 0, "n_sem_avaliacao_15min": 0, "n_clinicas_sem_loja": 0})
            continue
        dist_km = dentro.geometry.distance(geom_metrico) / 1000.0
        av = avaliacoes.loc[dentro.index]
        pt = peso_tipo[[concorrentes.index.get_loc(i) for i in dentro.index]]
        forca = float((av / dist_km.clip(lower=0.15) * pt).sum())
        n_clinicas_sem_loja = int(((dentro["tipo"] == "veterinaria") & (dentro["vende_racao"] != True)).sum())  # noqa: E712
        resultados.append({
            "candidato_id": cid, "forca_concorrencia": forca,
            "n_concorrentes_15min": len(dentro),
            "n_sem_avaliacao_15min": int(sem_avaliacao.loc[dentro.index].sum()),
            "n_clinicas_sem_loja": n_clinicas_sem_loja,
        })
    return pd.DataFrame(resultados)


# ---------------------------------------------------------------------------
# 7.4 — Ponto de equilíbrio (aluguel real vem da Fase 8; aqui ainda não existe)
# ---------------------------------------------------------------------------

def _ponto_equilibrio(cand: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    imoveis_path = DATA_MANUAL / "imoveis.csv"
    aluguel_por_bairro = {}
    if imoveis_path.exists():
        imoveis = pd.read_csv(imoveis_path)
        if not imoveis.empty and "bairro" in imoveis.columns and "aluguel" in imoveis.columns:
            aluguel_por_bairro = imoveis.groupby("bairro")["aluguel"].median().to_dict()

    aluguel_estimado = cand["bairro"].map(aluguel_por_bairro)
    aluguel_disponivel = aluguel_estimado.notna()
    if not aluguel_disponivel.any():
        LOGGER.warning("nenhum dado de aluguel em data/manual/imoveis.csv ainda — custo_fixo usa só o custo fixo extra (piso), sem o aluguel; rode a Fase 8 depois de preencher o CSV e recalcule")

    custo_fixo_extra = cfg["negocio"]["custo_fixo_extra_mensal"]
    custo_fixo = aluguel_estimado.fillna(0) + custo_fixo_extra
    margem_saco = cfg["negocio"]["margem_por_saco_premium"]
    sacos_breakeven = custo_fixo / margem_saco

    return pd.DataFrame({
        "aluguel_estimado_regiao": aluguel_estimado,
        "aluguel_disponivel": aluguel_disponivel,
        "custo_fixo_mensal": custo_fixo,
        "sacos_breakeven": sacos_breakeven,
    })


# ---------------------------------------------------------------------------
# 7.6 — Acesso e parada rápida (hierarquia da via, mão dupla, canteiro central)
# ---------------------------------------------------------------------------

def _acesso_score(cand: gpd.GeoDataFrame, vias: gpd.GeoDataFrame) -> pd.Series:
    juncao = gpd.sjoin_nearest(cand[["candidato_id", "geometry"]], vias[["highway", "oneway", "lanes", "geometry"]], how="left")
    juncao = juncao[~juncao.index.duplicated(keep="first")]

    base_por_classe = {"residential": 1.0, "tertiary": 0.75, "secondary": 0.4}
    scores = juncao["highway"].map(base_por_classe).fillna(0.5)
    mao_dupla = ~juncao["oneway"].astype(str).str.lower().isin(("yes", "true", "1"))
    scores = scores + mao_dupla.astype(float) * 0.15
    lanes = pd.to_numeric(juncao["lanes"], errors="coerce")
    scores = scores - ((lanes >= 4).fillna(False)).astype(float) * 0.25  # proxy de via com canteiro central
    return scores.clip(lower=0).reindex(cand.index)


# ---------------------------------------------------------------------------
# 7.7 — Dedup geográfica (<800m, mantém o melhor score do cluster)
# ---------------------------------------------------------------------------

def _dedup_geografica(cand: gpd.GeoDataFrame, raio_m: float = RAIO_DEDUP_M) -> gpd.GeoDataFrame:
    cand = cand.sort_values("score_final", ascending=False).reset_index(drop=True)
    sindex = cand.sindex
    suprimido = [False] * len(cand)
    for i, geom in enumerate(cand.geometry):
        if suprimido[i]:
            continue
        vizinhos = list(sindex.query(geom.buffer(raio_m)))
        for j in vizinhos:
            if j <= i or suprimido[j]:
                continue
            if cand.geometry.iloc[i].distance(cand.geometry.iloc[j]) < raio_m:
                suprimido[j] = True
    cand["duplicata_geografica"] = suprimido
    return cand


# ---------------------------------------------------------------------------

def run() -> tuple[Path, Path]:
    cfg = load_config()
    crs_metrico = cfg["crs"]["metrico"]
    pesos = json.loads((DATA_PROCESSED / "pesos.json").read_text(encoding="utf-8"))["pesos_eixos_score_final"]

    LOGGER.info("=== Fase 7: score e filtros ===")

    cand = gpd.read_file(DATA_PROCESSED / "candidatos_com_demanda.gpkg", layer="candidatos_com_demanda")
    concorrentes = gpd.read_file(DATA_PROCESSED / "concorrentes.gpkg")
    vias = gpd.read_file(DATA_PROCESSED / "vias.gpkg")
    n_entrada = len(cand)

    # 7.1
    cand["potencial_mensal"] = _demanda_estimada(cand, cfg)

    # 7.2 + 7.3
    forca = _forca_concorrencia(cand, concorrentes, cfg, crs_metrico)
    cand = cand.merge(forca, on="candidato_id", how="left")
    soma_forcas_efetiva = cand["forca_concorrencia"].clip(lower=0.01)
    cand["saturacao"] = cand["potencial_mensal"] / soma_forcas_efetiva

    # 7.4
    cand = pd.concat([cand, _ponto_equilibrio(cand, cfg)], axis=1)

    # 7.5 — teste absoluto (filtro duro, independe do score)
    captura_min = cfg["negocio"]["captura_min"]
    multiplo_min = cfg["negocio"]["multiplo_minimo_breakeven"]
    tem_dados_suficientes = cand["potencial_mensal"].notna()
    passou_teste = (cand["potencial_mensal"] * captura_min) >= (multiplo_min * cand["custo_fixo_mensal"])
    cand["teste_absoluto_passou"] = np.where(tem_dados_suficientes, passou_teste, False)
    cand["teste_absoluto_motivo"] = np.select(
        [~tem_dados_suficientes, ~passou_teste & tem_dados_suficientes],
        ["sem isócrona/demanda calculada", "potencial mensal × captura mínima abaixo de multiplo_minimo_breakeven × custo fixo (aluguel ainda não coletado eleva o risco de falso-positivo aqui)"],
        default="passou",
    )

    # 7.6 — score final por percentil
    cand["acesso_score"] = _acesso_score(cand, vias)
    cand["oferta_imovel_score"] = 0.0  # sem imoveis.csv preenchido ainda — penalidade explícita (spec §7.6), não zero "medido"
    cand["oferta_imovel_disponivel"] = False

    pct_demanda = cand["potencial_mensal"].rank(pct=True, na_option="bottom")
    pct_saturacao = cand["saturacao"].rank(pct=True, na_option="bottom")
    pct_acesso = cand["acesso_score"].rank(pct=True, na_option="bottom")
    pct_oferta = cand["oferta_imovel_score"].rank(pct=True, na_option="bottom")

    cand["score_final"] = (
        pesos["demanda_estimada"] * pct_demanda
        + pesos["saturacao"] * pct_saturacao
        + pesos["oferta_imovel"] * pct_oferta
        + pesos["acesso"] * pct_acesso
    )

    # 7.7
    cand = _dedup_geografica(cand)

    aprovados = cand[cand["teste_absoluto_passou"] & ~cand["duplicata_geografica"]].sort_values("score_final", ascending=False)
    reprovados = cand[~cand["teste_absoluto_passou"]]
    LOGGER.info(
        "teste absoluto: %d passaram, %d reprovados; dedup geográfica: %d duplicatas suprimidas",
        cand["teste_absoluto_passou"].sum(), len(reprovados), cand["duplicata_geografica"].sum(),
    )

    top10 = aprovados.head(10).copy()
    if len(top10) < 10:
        LOGGER.warning("ACEITE — só %d candidatos aprovados sobraram pro Top 10 (esperado 10)", len(top10))
    else:
        LOGGER.info("ACEITE — Top 10 completo com 10 candidatos aprovados")

    # checagem de aceite: nenhum a menos de 1,5km de rede (já é garantido pela Fase 4, aqui é só auditoria)
    raio_bloqueio = cfg["concorrencia"]["raio_bloqueio_rede_m"]
    if (top10["dist_rede_mais_perto"] < raio_bloqueio).any():
        LOGGER.warning("ACEITE — existe candidato no Top 10 a menos de %dm de uma rede (não deveria, checar Fase 4)", raio_bloqueio)
    else:
        LOGGER.info("ACEITE — nenhum candidato do Top 10 a menos de %dm de rede", raio_bloqueio)

    # --- saídas -------------------------------------------------------
    colunas_saida = [
        "candidato_id", "bairro", "domicilios_10min", "pct_apartamento_10min", "renda_media_10min",
        "n_concorrentes_15min", "forca_concorrencia", "n_clinicas_sem_loja",
        "saturacao", "potencial_mensal", "oferta_imovel_disponivel", "aluguel_estimado_regiao",
        "acesso_score", "score_final", "teste_absoluto_passou", "teste_absoluto_motivo",
    ]
    colunas_saida = [c for c in colunas_saida if c in cand.columns]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    top10_out = top10[colunas_saida]
    top10_path = OUTPUT_DIR / "top10.csv"
    top10_out.to_csv(top10_path, index=False, sep=";", encoding="utf-8-sig")
    LOGGER.info("gravado: %s (%d linhas)", top10_path, len(top10_out))

    ranking_path = DATA_PROCESSED / "ranking_completo.parquet"
    cand_para_parquet = cand.drop(columns="geometry")
    cand_para_parquet.to_parquet(ranking_path, index=False)
    LOGGER.info("gravado: %s (%d linhas, todos os candidatos)", ranking_path, len(cand_para_parquet))

    log_resumo_fase(
        LOGGER, entrada=n_entrada, saida=len(top10),
        descartados=n_entrada - len(top10),
        motivo_descarte="reprovados no teste absoluto, duplicata geográfica <800m, ou fora do top 10 por score",
    )
    return top10_path, ranking_path


if __name__ == "__main__":
    run()
