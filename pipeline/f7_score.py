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
    aplicar_cap_bairro, coord_hash, get_logger, load_config, log_resumo_fase,
)

LOGGER = get_logger("fase7_score")

ISO_RAW_DIR = DATA_RAW / "isocronas"
RAIO_DEDUP_M = 800
MAX_POR_BAIRRO_TOP10 = 2  # G2 (CORRECOES.md) — dez esquinas de cinco bairros não serve pra visitar


# ---------------------------------------------------------------------------
# 7.1 — Demanda estimada (captação de balcão, C4.1 CORRECOES_2.md)
# ---------------------------------------------------------------------------

# C4.1 — sobrescreve a composição genérica do B1 (PESO_ANEL={5:1.00,10:0.35,15:0.10}) só
# para a captação de balcão: "ninguém atravessa a cidade pra comprar ração", o anel de
# 15min sai inteiramente da conta (fica só para forca_concorrencia e C4.2, que continuam
# usando a isócrona/anéis completos). Onde B1 conflita com C4.1, C4.1 prevalece.
PESO_CAPTACAO_BALCAO = {5: 1.00, 10: 0.25}


def _demanda_estimada(cand: pd.DataFrame, cfg: dict, universidades: gpd.GeoDataFrame | None = None) -> pd.Series:
    taxa_posse = cfg["negocio"]["taxa_posse_pet_domicilio"]
    gasto_medio = cfg["negocio"]["gasto_medio_mensal_por_pet"]

    peso_total = sum(PESO_CAPTACAO_BALCAO[m] * cand[f"domicilios_anel{m}"].fillna(0) for m in PESO_CAPTACAO_BALCAO)
    domicilios_captacao = peso_total
    pct_apto_captacao = sum(
        PESO_CAPTACAO_BALCAO[m] * cand[f"domicilios_anel{m}"].fillna(0) * cand[f"pct_apartamento_anel{m}"].fillna(0)
        for m in PESO_CAPTACAO_BALCAO
    ) / peso_total.replace(0, np.nan)
    renda_captacao = sum(
        PESO_CAPTACAO_BALCAO[m] * cand[f"domicilios_anel{m}"].fillna(0) * cand[f"renda_media_anel{m}"].fillna(0)
        for m in PESO_CAPTACAO_BALCAO
    ) / peso_total.replace(0, np.nan)
    pct_18a24_captacao = sum(
        PESO_CAPTACAO_BALCAO[m] * cand[f"domicilios_anel{m}"].fillna(0) * cand[f"pct_18a24_anel{m}"].fillna(0)
        for m in PESO_CAPTACAO_BALCAO
    ) / peso_total.replace(0, np.nan)

    fator_verticalizacao = 1.0 + 0.4 * pct_apto_captacao.fillna(0)

    # C3 (CORRECOES_2.md) — desconta o fator de verticalização quando há suspeita de
    # moradia estudantil: % de 18-24 anos alto (percentil 75 dos 300 candidatos) E perto
    # de campus universitário. República/apartamento de estudante não é domicílio com pet
    # nem comprador de ração premium.
    cfg_estudantil = cfg.get("estudantil", {})
    n_suspeitos = 0
    if universidades is not None and not universidades.empty and pct_18a24_captacao.notna().any():
        dist_campus_km = cand.geometry.apply(lambda g: universidades.distance(g).min() / 1000.0)
        percentil_corte = cfg_estudantil.get("percentil_pct_18a24", 0.75)
        limite = pct_18a24_captacao.quantile(percentil_corte)
        dist_maxima = cfg_estudantil.get("distancia_campus_km", 2.0)
        suspeita_estudantil = (pct_18a24_captacao > limite) & (dist_campus_km < dist_maxima)
        fator_desconto = cfg_estudantil.get("fator_desconto_verticalizacao", 0.6)
        fator_verticalizacao = fator_verticalizacao.where(~suspeita_estudantil, fator_verticalizacao * fator_desconto)
        n_suspeitos = int(suspeita_estudantil.sum())
        cand["dist_campus_km"] = dist_campus_km
        cand["suspeita_estudantil"] = suspeita_estudantil
    else:
        cand["dist_campus_km"] = None
        cand["suspeita_estudantil"] = False
    LOGGER.info("C3 — %d candidato(s) com suspeita de verticalização estudantil (fator de verticalização ×%.1f aplicado)",
                n_suspeitos, cfg_estudantil.get("fator_desconto_verticalizacao", 0.6))
    if universidades is not None and not universidades.empty and "suspeita_estudantil" in cand.columns:
        sm = cand[cand["bairro"] == "Santa Mônica"]
        if not sm.empty and not sm["suspeita_estudantil"].any():
            LOGGER.warning(
                "C3 — validação do CORRECOES_2.md esperava Santa Mônica fortemente representado entre "
                "os descontados; nenhum dos %d candidatos de Santa Mônica disparou o filtro (dist_campus_km "
                "mín=%.2fkm, limite configurado=%.1fkm). Causa raiz investigada: o único ponto OSM "
                "amenity=university nas proximidades (UFU Campus Santa Mônica) está com o campo 'name' "
                "malformado no OSM ('1H', não o nome do campus) e representa um único prédio, não o "
                "polígono do campus inteiro — a distância medida (mín. ~2,0km) fica bem na borda do corte "
                "de %.1fkm. Não ajustado aqui (mudar o limite só pra encaixar Santa Mônica seria ajustar o "
                "critério ao resultado desejado, não uma correção de bug). C2 (conhecimento local, "
                "bairros_penalizados) já cobre esse mesmo efeito por conhecimento direto do avaliador, "
                "então o score final não fica cego a isso — só o eixo automático C3 é que não sozinho.",
                len(sm), sm["dist_campus_km"].min(), cfg_estudantil.get("distancia_campus_km", 2.0),
                cfg_estudantil.get("distancia_campus_km", 2.0),
            )

    potencial = domicilios_captacao * taxa_posse * fator_verticalizacao * gasto_medio

    cand["domicilios_captacao_efetivo"] = domicilios_captacao  # reusado por B2 (Huff) e C4.2 (raio de entrega)
    cand["pct_apartamento_captacao_efetivo"] = pct_apto_captacao
    cand["renda_media_captacao_efetivo"] = renda_captacao  # só descritivo — renda não entra na fórmula de demanda
    cand["pct_18a24_captacao_efetivo"] = pct_18a24_captacao
    return potencial.where(domicilios_captacao.notna())


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
# B2 (CORRECOES.md) — Demanda capturada via modelo gravitacional de Huff.
# Substitui potencial_mensal × captura_min no teste absoluto: a captura de uma loja
# nova depende de quantos concorrentes disputam cada domicílio, não de uma fração fixa.
# ---------------------------------------------------------------------------

# C4.1 (CORRECOES_2.md) já aplicado — o modelo de Huff usa a mesma composição de captação
# de balcão que _demanda_estimada (PESO_CAPTACAO_BALCAO, definida acima), não mais o
# PESO_ANEL genérico do B1. Mantém os dois insumos de demanda (potencial_mensal e
# demanda_capturada) consistentes sobre a mesma área de captação.
PESO_ANEL_CAPTACAO = PESO_CAPTACAO_BALCAO
DIST_MIN_KM = 0.15  # mesmo piso de distância usado em _forca_concorrencia, evita explosão em 1/dist²


def _extrair_feature_isocrona(fc: dict, minutos_alvo: int) -> dict | None:
    valor_alvo = minutos_alvo * 60
    for f in fc["features"]:
        if f["properties"]["value"] == valor_alvo:
            return f
    return None


def _aneis_metricos_f7(fc: dict, minutos_lista: list[int], crs_metrico: str) -> dict[int, object | None]:
    """Mesmo recorte de anéis concêntricos que a Fase 5 (f5_isocronas._aneis_metricos)
    faz a partir do cache já baixado — reimplementado aqui porque o modelo de Huff
    precisa da granularidade por setor, que a Fase 5 já agrega antes de gravar."""
    from shapely.geometry import shape
    polys_metricos: dict[int, object | None] = {}
    for m in minutos_lista:
        feat = _extrair_feature_isocrona(fc, m)
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


def _pesos_setores_captacao(cache_path: Path, minutos_lista: list[int], crs_metrico: str,
                             setores: gpd.GeoDataFrame, sindex, setores_area_total,
                             pesos_anel: dict[int, float]) -> pd.Series | None:
    """Domicílios ocupados × fração de área do setor dentro de cada anel × peso do anel,
    por setor — a mesma composição que domicilios_efetivo usa, só que sem agregar em um
    escalar (o modelo de Huff precisa saber DE ONDE vêm os domicílios, setor a setor,
    pra calcular a distância até cada concorrente)."""
    if not cache_path.exists():
        return None
    fc = json.loads(cache_path.read_text(encoding="utf-8"))
    aneis = _aneis_metricos_f7(fc, minutos_lista, crs_metrico)
    peso_total = pd.Series(0.0, index=setores.index)
    algum = False
    for m, peso_m in pesos_anel.items():
        poligono = aneis.get(m)
        if poligono is None or poligono.is_empty:
            continue
        idx = list(sindex.query(poligono, predicate="intersects"))
        if not idx:
            continue
        sub = setores.iloc[idx]
        inter_area = sub.geometry.intersection(poligono).area
        fracao = (inter_area / setores_area_total.iloc[idx].values).clip(0, 1)
        contrib = sub["domicilios_ocupados"].fillna(0).values * fracao * peso_m
        peso_total.iloc[idx] = peso_total.iloc[idx].values + contrib
        algum = True
    return peso_total if algum else None


def _atratividade_concorrentes(concorrentes: gpd.GeoDataFrame, cfg: dict) -> pd.Series:
    av = pd.to_numeric(concorrentes["avaliacoes"], errors="coerce")
    max_av = av.max()
    if max_av and max_av > 0:
        a_conc = 0.5 + 1.5 * (av.fillna(0) / max_av)
    else:
        a_conc = pd.Series(0.5, index=concorrentes.index)  # sem nenhuma avaliação na base — todos iguais
    peso_rede = cfg["concorrencia"]["peso_rede"]
    peso_independente = cfg["concorrencia"]["peso_independente"]
    peso_tipo = np.where(concorrentes["tipo"] == "rede", peso_rede, peso_independente)
    return a_conc * peso_tipo


def _denominador_huff_por_setor(setores: gpd.GeoDataFrame, concorrentes: gpd.GeoDataFrame, atratividade: pd.Series) -> pd.Series:
    """Σ_k atratividade_k / dist(setor_j, k)_km² — não depende do candidato i, calculado
    uma única vez pra todos os ~1988 setores (a fração de Huff usa isso como denominador)."""
    centroides = setores.geometry.centroid
    denom = np.zeros(len(setores))
    atrat_vals = atratividade.to_numpy()
    for k, geom_k in enumerate(concorrentes.geometry):
        dist_km = centroides.distance(geom_k).to_numpy() / 1000.0
        denom += atrat_vals[k] / np.clip(dist_km, DIST_MIN_KM, None) ** 2
    return pd.Series(denom, index=setores.index)


def _demanda_capturada(cand: gpd.GeoDataFrame, setores: gpd.GeoDataFrame, denom_huff_setor: pd.Series,
                        cfg: dict, minutos_lista: list[int], crs_metrico: str, pesos_anel: dict[int, float]) -> pd.Series:
    taxa_posse = cfg["negocio"]["taxa_posse_pet_domicilio"]
    gasto_medio = cfg["negocio"]["gasto_medio_mensal_por_pet"]
    A_nova = 1.0
    setores_area_total = setores.geometry.area
    sindex = setores.sindex
    centroides = setores.geometry.centroid

    cand_wgs = cand.to_crs("EPSG:4326")
    resultados = []
    for geom_metrico, lon, lat in zip(cand.geometry, cand_wgs.geometry.x, cand_wgs.geometry.y):
        cache_path = ISO_RAW_DIR / f"{coord_hash(lon, lat)}.json"
        pesos_setor = _pesos_setores_captacao(cache_path, minutos_lista, crs_metrico, setores, sindex, setores_area_total, pesos_anel)
        if pesos_setor is None or pesos_setor.sum() <= 0:
            resultados.append(None)
            continue
        idx_rel = pesos_setor[pesos_setor > 0].index
        dist_km = centroides.loc[idx_rel].distance(geom_metrico).to_numpy() / 1000.0
        atrat_i = A_nova / np.clip(dist_km, DIST_MIN_KM, None) ** 2
        p_j_i = atrat_i / (denom_huff_setor.loc[idx_rel].to_numpy() + atrat_i)
        demanda = float((pesos_setor.loc[idx_rel].to_numpy() * taxa_posse * gasto_medio * p_j_i).sum())
        resultados.append(demanda)
    return pd.Series(resultados, index=cand.index)


# ---------------------------------------------------------------------------
# C4.2 (CORRECOES_2.md) — raio de entrega viável, separado da captação que decide o ponto
# (C4.1). Tempo de mapa != tempo de porta: ida+volta, fricção de prédio (portaria,
# interfone, elevador). Serve pra dimensionar a ficha, não pra escolher o ponto.
# ---------------------------------------------------------------------------

def _raio_entrega(cand: pd.DataFrame, cfg: dict) -> tuple[pd.Series, pd.Series]:
    fator = cfg["negocio"]["fator_ida_volta_entrega"]
    friccao = cfg["negocio"]["friccao_entrega_min"]
    teto_min = cfg["negocio"]["raio_entrega_max_min"]
    custo_hora = cfg["negocio"]["custo_hora_entregador"]

    # tempo de deslocamento (mapa, só ida) máximo que ainda cabe no teto de porta-a-porta
    tempo_deslocamento_max = (teto_min - friccao) / fator  # (25-6)/2.5 = 7.6min
    # só há dados nos anéis de 5/10min cacheados — interpola linearmente entre os dois
    # (decisão confirmada: sem nova chamada ao ORS, sem custo extra de API)
    frac = (tempo_deslocamento_max - 5) / (10 - 5)
    domicilios_entrega = cand["domicilios_anel5"].fillna(0) + frac * cand["domicilios_anel10"].fillna(0)
    tempo_porta_a_porta = tempo_deslocamento_max * fator + friccao  # ≈ teto_min, por construção
    custo_por_entrega = pd.Series(tempo_porta_a_porta / 60.0 * custo_hora, index=cand.index)

    # Raio equivalente só para o log de validação do CORRECOES_2.md — precisa vir de uma
    # ÁREA interpolada (mesmos pesos 1.0/frac usados acima para domicílios), não de
    # domicilios_entrega diretamente: um raio de sqrt(N_domicílios/π) mistura contagem de
    # domicílios com m² e dá um número sem sentido físico (bug encontrado nesta sessão —
    # produzia ~0.2km quando o esperado é 3-4km; "area_anel{5,10}_km2" vem de f5_isocronas.py).
    if "area_anel5_km2" in cand.columns and "area_anel10_km2" in cand.columns:
        area_interpolada_km2 = cand["area_anel5_km2"].fillna(0) + frac * cand["area_anel10_km2"].fillna(0)
        raio_equivalente_km = (area_interpolada_km2.clip(lower=0) / 3.14159).pow(0.5)
        raio_log = "%.1fkm" % raio_equivalente_km.median()
    else:
        raio_log = "não disponível (area_anel5_km2/area_anel10_km2 ausentes — rode f5_isocronas.py de novo)"
    LOGGER.info(
        "C4.2 — raio de entrega viável: tempo_deslocamento_max=%.1fmin, tempo_porta_a_porta=%.1fmin, "
        "custo_por_entrega=R$%.2f, raio equivalente mediano=%s (esperado ~3-4km, CORRECOES_2.md)",
        tempo_deslocamento_max, tempo_porta_a_porta, custo_por_entrega.iloc[0] if len(custo_por_entrega) else float("nan"),
        raio_log,
    )
    return domicilios_entrega, custo_por_entrega


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

    # B3 (CORRECOES.md) — sem dado real de aluguel, o fallback tem que ser o teto de aluguel
    # configurado (negocio.teto_aluguel), NUNCA 0: custo_fixo=0 neutraliza o teste absoluto
    # (foi o defeito medido: 300/300 candidatos aprovados). aluguel_e_estimado fica marcado
    # pra aparecer no relatório (tabela e ficha) sempre que o fallback for usado.
    teto_aluguel = cfg["negocio"]["teto_aluguel"]
    aluguel_e_estimado = ~aluguel_disponivel
    aluguel_final = aluguel_estimado.fillna(teto_aluguel)
    if not aluguel_disponivel.any():
        LOGGER.warning(
            "nenhum dado de aluguel em data/manual/imoveis.csv ainda — custo_fixo usa o fallback "
            "negocio.teto_aluguel=%d (não zero); rode a Fase 8 depois de preencher o CSV e recalcule",
            teto_aluguel,
        )

    custo_fixo_extra = cfg["negocio"]["custo_fixo_extra_mensal"]
    custo_fixo = aluguel_final + custo_fixo_extra
    margem_saco = cfg["negocio"]["margem_por_saco_premium"]
    sacos_breakeven = custo_fixo / margem_saco

    return pd.DataFrame({
        "aluguel_estimado_regiao": aluguel_final,
        "aluguel_e_estimado": aluguel_e_estimado,
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
# 7.6 (G3, CORRECOES.md) — composição pura do score final, extraída pra ser testável sem
# I/O (tests/test_g3_monotonicidade.py): score final tem que cair monotonicamente quando
# a força da concorrência sobe (saturacao=potencial/força cai, pct_saturacao cai, e o peso
# de saturação nunca é negativo — ver _normalizar_pesos em f6_calibracao.py).
# ---------------------------------------------------------------------------

def _compor_score_final(percentis: dict, pesos: dict):
    """Soma ponderada pura — funciona tanto com escalares (teste unitário) quanto com
    pandas Series (uso real em run()). Só combina os eixos presentes em ambos os dicts,
    pra permitir M1 (renormalização quando um eixo está ausente) chamar isso já filtrado."""
    total = 0.0
    for eixo, peso in pesos.items():
        if eixo in percentis:
            total = total + peso * percentis[eixo]
    return total


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
    minutos_lista = cfg["isocronas"]["minutos"]
    pesos = json.loads((DATA_PROCESSED / "pesos.json").read_text(encoding="utf-8"))["pesos_eixos_score_final"]

    LOGGER.info("=== Fase 7: score e filtros ===")

    # C4.1 — valida consistência entre captacao_balcao.* e isocronas.minutos (só documenta/
    # audita; os pesos em si ficam em PESO_CAPTACAO_BALCAO, não neste bloco de config).
    cap_cfg = cfg.get("captacao_balcao", {})
    for chave in ("anel_primario_min", "anel_secundario_min"):
        m = cap_cfg.get(chave)
        if m is not None and m not in minutos_lista:
            LOGGER.warning("C4.1 — captacao_balcao.%s=%s não está em isocronas.minutos=%s — sem isócrona cacheada para esse anel", chave, m, minutos_lista)

    cand = gpd.read_file(DATA_PROCESSED / "candidatos_com_demanda.gpkg", layer="candidatos_com_demanda")
    concorrentes = gpd.read_file(DATA_PROCESSED / "concorrentes.gpkg")
    vias = gpd.read_file(DATA_PROCESSED / "vias.gpkg")
    setores = gpd.read_file(DATA_PROCESSED / "setores.gpkg")
    universidades_path = DATA_PROCESSED / "universidades.gpkg"
    universidades = gpd.read_file(universidades_path) if universidades_path.exists() else None
    if universidades is None:
        LOGGER.warning("C3 — data/processed/universidades.gpkg não existe ainda (rode f2c_universidades.py) — desconto de verticalização estudantil desativado nesta rodada")
    n_entrada = len(cand)

    # 7.1
    cand["potencial_mensal"] = _demanda_estimada(cand, cfg, universidades)

    # 7.2 + 7.3
    forca = _forca_concorrencia(cand, concorrentes, cfg, crs_metrico)
    cand = cand.merge(forca, on="candidato_id", how="left")
    soma_forcas_efetiva = cand["forca_concorrencia"].clip(lower=0.01)
    cand["saturacao"] = cand["potencial_mensal"] / soma_forcas_efetiva

    # B2 — demanda_capturada (modelo de Huff): substitui potencial_mensal×captura_min no
    # teste absoluto. potencial_mensal (bruto, sem competição) continua alimentando o eixo
    # demanda_estimada do score — só o teste absoluto muda de insumo.
    LOGGER.info(
        "B2 — captura_min=%.2f/captura_max=%.2f do config.yaml não são mais usados no teste "
        "absoluto desde a adoção do modelo de Huff (demanda_capturada); mantidos no config só "
        "por retrocompatibilidade, sem efeito ativo",
        cfg["negocio"]["captura_min"], cfg["negocio"]["captura_max"],
    )
    atratividade = _atratividade_concorrentes(concorrentes, cfg)
    denom_huff_setor = _denominador_huff_por_setor(setores, concorrentes, atratividade)
    cand["demanda_capturada"] = _demanda_capturada(cand, setores, denom_huff_setor, cfg, minutos_lista, crs_metrico, PESO_ANEL_CAPTACAO)

    # validação do B2 (CORRECOES.md): soma de demanda_capturada por bairro não pode
    # ultrapassar o potencial daquele bairro (domicílios do bairro inteiro × posse × gasto)
    potencial_por_bairro = (
        setores.groupby("NM_BAIRRO")["domicilios_ocupados"].sum().fillna(0)
        * cfg["negocio"]["taxa_posse_pet_domicilio"] * cfg["negocio"]["gasto_medio_mensal_por_pet"]
    )
    soma_capturada_por_bairro = cand.groupby("bairro")["demanda_capturada"].sum()
    n_bairros_estourados = 0
    for bairro, soma in soma_capturada_por_bairro.items():
        potencial = potencial_por_bairro.get(bairro)
        if potencial is not None and soma > potencial:
            n_bairros_estourados += 1
            LOGGER.warning(
                "B2 — soma de demanda_capturada em '%s' (R$ %.0f) ultrapassa o potencial do bairro (R$ %.0f)",
                bairro, soma, potencial,
            )
    if n_bairros_estourados == 0:
        LOGGER.info("B2 — ACEITE: nenhum bairro com soma de demanda_capturada acima do potencial do bairro")
    else:
        LOGGER.warning("B2 — %d bairro(s) com demanda_capturada somada acima do potencial (candidatos do mesmo bairro competindo pela mesma demanda sem se descontar)", n_bairros_estourados)

    # 7.4
    cand = pd.concat([cand, _ponto_equilibrio(cand, cfg)], axis=1)

    # C4.2 — raio de entrega viável (informativo, não entra no score nem no teste absoluto)
    cand["domicilios_entrega_viavel"], cand["custo_por_entrega"] = _raio_entrega(cand, cfg)

    # 7.5 — teste absoluto (filtro duro, independe do score)
    multiplo_min = cfg["negocio"]["multiplo_minimo_breakeven"]
    tem_dados_suficientes = cand["demanda_capturada"].notna()
    passou_teste = cand["demanda_capturada"] >= (multiplo_min * cand["custo_fixo_mensal"])
    cand["teste_absoluto_passou"] = np.where(tem_dados_suficientes, passou_teste, False)
    cand["teste_absoluto_motivo"] = np.select(
        [~tem_dados_suficientes, ~passou_teste & tem_dados_suficientes],
        ["sem isócrona/demanda calculada", "demanda_capturada (modelo de Huff) abaixo de multiplo_minimo_breakeven × custo fixo"],
        default="passou",
    )
    taxa_aprovacao = cand["teste_absoluto_passou"].mean()
    if taxa_aprovacao >= 0.999:
        # B3 (CORRECOES.md) — mesmo depois de B1+B2, se o teste continuar aprovando 100%,
        # o documento manda registrar isso explicitamente no relatório, não esconder: "pode
        # ser real, mas precisa ser dito". f9_relatorio.py lê essa nota da Metodologia.
        LOGGER.warning(
            "B3 — teste absoluto aprovou 100%% dos candidatos mesmo depois de B1+B2+B3 "
            "(fallback de aluguel=%d). Isso pode ser real (mercado com bastante espaço), mas "
            "precisa ficar dito no relatório, não escondido — ver seção de Metodologia.",
            cfg["negocio"]["teto_aluguel"],
        )

    # C5 (CORRECOES_2.md) — filtro duro de percentil máximo de força de concorrência:
    # elimina candidatos no quartil superior (é o que barra Santa Mônica automaticamente,
    # sem precisar de conhecimento local declarado em C2).
    percentil_max_forca = cfg["concorrencia"]["percentil_maximo_forca"]
    pct_forca = cand["forca_concorrencia"].rank(pct=True, na_option="bottom")
    cand["filtro_forca_passou"] = pct_forca <= percentil_max_forca
    n_reprovados_forca = int((~cand["filtro_forca_passou"]).sum())
    LOGGER.info(
        "C5 — filtro de percentil máximo de força de concorrência (<=%.0f%%): %d/%d candidatos reprovados",
        percentil_max_forca * 100, n_reprovados_forca, len(cand),
    )
    # motivo combinado pra tabela de reprovados da Fase 9 — sem isso, quem passa no teste
    # absoluto mas cai no filtro de força apareceria erroneamente como "passou"
    cand["teste_absoluto_motivo"] = np.where(
        cand["teste_absoluto_passou"] & ~cand["filtro_forca_passou"],
        f"reprovado no filtro de força de concorrência (C5, percentil > {percentil_max_forca*100:.0f}%)",
        cand["teste_absoluto_motivo"],
    )

    # C1.4 (CORRECOES_2.md) — eixo vitalidade_comercial. vitalidade_bairro.csv é gerado por
    # f2b_vitalidade.py (C1.1); sem ele, o eixo fica indisponível e M1 renormaliza —
    # degradação graciosa, igual ao resto do pipeline.
    #
    # FILTRO DURO DESATIVADO (decisão do usuário, achado real de dados): a validação que o
    # próprio CORRECOES_2.md exige ("rodar os indicadores contra Centro e Presidente
    # Roosevelt — eles precisam estar entre os piores; se não, o indicador não está
    # capturando o fenômeno, investigar antes de confiar nele") FALHOU com dado real de
    # produção — RFB CNPJ, 356.420 estabelecimentos de Uberlândia, 73.116 de comércio
    # varejista (divisão 47), 61.343 casados a bairro do IBGE: Centro tem mortalidade de
    # 24,4% (abaixo da mediana de 29,9%, posição #66 de 75 — uma das MELHORES, não das
    # piores) e Presidente Roosevelt 31,3% (#24 de 75, fora do quartil superior). C1.2
    # (fechamento permanente no Google Places) deu 0/273 — o método de descoberta via
    # textSearch estruturalmente não retorna loja já fechada. C1.3 confirmado indisponível
    # (limitação de tier de API, ver f2_concorrentes.py). Nenhum dos 3 sub-indicadores de
    # C1 validou. Eliminar candidatos por um filtro duro nessas condições arriscaria cortar
    # bairros sem relação real com decadência comercial (ex.: Portal do Vale, maior
    # mortalidade medida = 50%, sem nenhuma evidência de ser análogo a Centro/PR). O eixo
    # continua entrando no score (informativo, peso 15%) — a exclusão de Centro/Presidente
    # Roosevelt fica a cargo do C2 (conhecimento local, filtro duro explícito por nome).
    vitalidade_path = DATA_PROCESSED / "vitalidade_bairro.csv"
    cfg_vitalidade = cfg.get("vitalidade_comercial", {})
    cand["filtro_vitalidade_passou"] = True  # sempre — filtro duro desativado, ver nota acima
    if vitalidade_path.exists():
        vitalidade_bairro = pd.read_csv(vitalidade_path)
        cand = cand.merge(vitalidade_bairro[["bairro", "taxa_mortalidade"]], on="bairro", how="left")
        cand["vitalidade_comercial_score"] = 1.0 - cand["taxa_mortalidade"]
        percentil_filtro = cfg_vitalidade.get("percentil_filtro_mortalidade", 0.75)
        limite_mortalidade = vitalidade_bairro["taxa_mortalidade"].quantile(percentil_filtro)
        n_no_quartil_superior = int((cand["taxa_mortalidade"] >= limite_mortalidade).sum())
        LOGGER.info(
            "C1.4 — eixo informativo (filtro duro desativado): %d/%d candidatos estariam no quartil "
            "superior de mortalidade (limite=%.3f) se o filtro estivesse ativo",
            n_no_quartil_superior, len(cand), limite_mortalidade,
        )
    else:
        LOGGER.warning("C1.4 — data/processed/vitalidade_bairro.csv não existe ainda (rode f2b_vitalidade.py) — eixo de vitalidade desativado nesta rodada")
        cand["vitalidade_comercial_score"] = np.nan

    # 7.6 — score final por percentil
    cand["acesso_score"] = _acesso_score(cand, vias)
    cand["oferta_imovel_score"] = 0.0  # sem imoveis.csv preenchido ainda — penalidade explícita (spec §7.6), não zero "medido"
    cand["oferta_imovel_disponivel"] = False

    # M1 (CORRECOES.md) — renormaliza os pesos só sobre os eixos efetivamente disponíveis
    # nesta rodada. Sem isso, um eixo 100% ausente (ex.: oferta_imovel sem imoveis.csv)
    # consome sua fatia do peso (20%) de forma uniforme sem nenhum sinal discriminante,
    # distorcendo a escala do score final sem aviso.
    disponibilidade = {
        "demanda_estimada": bool(cand["potencial_mensal"].notna().any()),
        "saturacao": bool(cand["saturacao"].notna().any()),
        "acesso": bool(cand["acesso_score"].notna().any()),
        "oferta_imovel": bool(cand["oferta_imovel_disponivel"].any()),
        "vitalidade_comercial": bool(cand["vitalidade_comercial_score"].notna().any()),
    }
    # só entra na renormalização se tiver dado disponível E peso calibrado em pesos.json
    # (ex.: se vitalidade_comercial.peso_eixo foi configurado mas a Fase 6 ainda não rodou
    # de novo, o eixo não está em `pesos` — tratado como indisponível, não como peso 0 mudo)
    eixos_disponiveis = [e for e, ok in disponibilidade.items() if ok and e in pesos]
    soma_disponivel = sum(pesos[e] for e in eixos_disponiveis)
    if soma_disponivel > 0:
        pesos_renormalizados = {e: pesos[e] / soma_disponivel for e in eixos_disponiveis}
    else:
        pesos_renormalizados = {}
        LOGGER.error("M1 — nenhum eixo do score disponível nesta rodada, score_final ficará todo zero")

    percentis = {
        "demanda_estimada": cand["potencial_mensal"].rank(pct=True, na_option="bottom"),
        "saturacao": cand["saturacao"].rank(pct=True, na_option="bottom"),
        "acesso": cand["acesso_score"].rank(pct=True, na_option="bottom"),
        "oferta_imovel": cand["oferta_imovel_score"].rank(pct=True, na_option="bottom"),
        "vitalidade_comercial": cand["vitalidade_comercial_score"].rank(pct=True, na_option="bottom"),
    }
    percentis_disponiveis = {e: percentis[e] for e in eixos_disponiveis}
    cand["score_final"] = _compor_score_final(percentis_disponiveis, pesos_renormalizados)

    LOGGER.info(
        "M1 — eixos disponíveis nesta rodada: %s (pesos originais %s -> renormalizados %s)",
        eixos_disponiveis,
        {k: round(v, 3) for k, v in pesos.items()},
        {k: round(v, 3) for k, v in pesos_renormalizados.items()},
    )
    pesos_efetivos_path = DATA_PROCESSED / "pesos_efetivos_aplicados.json"
    pesos_efetivos_path.write_text(json.dumps({
        "pesos_originais": pesos,
        "eixos_disponiveis": eixos_disponiveis,
        "eixos_ausentes": [e for e in pesos if e not in eixos_disponiveis],
        "pesos_renormalizados": pesos_renormalizados,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    LOGGER.info("gravado: %s", pesos_efetivos_path)

    # C2 (CORRECOES_2.md) — conhecimento local declarado: aplicado depois do score, antes
    # do corte do Top10. Exclusão vira filtro duro (junto com teste absoluto/dedup/força);
    # penalização multiplica score_final, preservando o valor bruto pra auditoria.
    conhecimento_local = cfg.get("conhecimento_local", {})
    excluidos_cfg = {b["nome"]: b["motivo"] for b in conhecimento_local.get("bairros_excluidos", [])}
    penalizados_cfg = {b["nome"]: (b["fator"], b["motivo"]) for b in conhecimento_local.get("bairros_penalizados", [])}

    cand["excluido_conhecimento_local"] = cand["bairro"].isin(excluidos_cfg)
    cand["motivo_exclusao_conhecimento_local"] = cand["bairro"].map(excluidos_cfg)
    # mesma lógica de sobreposição de motivo do C5/C1.4 — sem isso, quem passa em todos os
    # outros filtros mas é excluído por bairro apareceria como "passou" na tabela de reprovados
    cand["teste_absoluto_motivo"] = np.where(
        cand["teste_absoluto_passou"] & cand["filtro_forca_passou"] & cand["filtro_vitalidade_passou"] & cand["excluido_conhecimento_local"],
        cand["bairro"].map(lambda b: f"bairro excluído por conhecimento local (C2): {excluidos_cfg.get(b, '')}"),
        cand["teste_absoluto_motivo"],
    )

    cand["score_final_bruto"] = cand["score_final"]  # preserva o valor pré-penalidade para auditoria
    fator_penalizacao = cand["bairro"].map(lambda b: penalizados_cfg.get(b, (1.0, None))[0])
    cand["fator_penalizacao_conhecimento_local"] = fator_penalizacao
    cand["motivo_penalizacao_conhecimento_local"] = cand["bairro"].map(lambda b: penalizados_cfg.get(b, (1.0, None))[1])
    cand["score_final"] = cand["score_final_bruto"] * fator_penalizacao

    n_excluidos = int(cand["excluido_conhecimento_local"].sum())
    n_penalizados = int((fator_penalizacao != 1.0).sum())
    LOGGER.info(
        "C2 — conhecimento local: %d candidato(s) excluído(s) (%s), %d penalizado(s) (%s)",
        n_excluidos, sorted(excluidos_cfg) or "nenhum bairro excluído configurado",
        n_penalizados, sorted(penalizados_cfg) or "nenhum bairro penalizado configurado",
    )
    # A1/A7 (CORRECOES_3.md) — desde que a exclusão passou a rodar também na Fase 4 (antes do
    # corte preliminar), esta checagem na Fase 7 é defesa em profundidade e DEVE ser um no-op:
    # nenhum candidato de bairro vetado deveria nem chegar até aqui. Se chegar, é sinal de bug
    # (ex.: nome de bairro grafado diferente entre as duas fases), não um resultado esperado.
    if n_excluidos > 0:
        LOGGER.warning(
            "A1/A7 — defesa em profundidade: %d candidato(s) em bairro vetado chegaram à Fase 7 "
            "mesmo devendo ter sido removidos na Fase 4 (A1, CORRECOES_3.md) — investigar bug na "
            "exclusão da Fase 4 (nome de bairro pode não bater exatamente entre f4_candidatos.py "
            "e config.yaml)", n_excluidos,
        )
    else:
        LOGGER.info("A1/A7 — defesa em profundidade OK: nenhum candidato em bairro vetado chegou à Fase 7")

    # 7.7
    cand = _dedup_geografica(cand)

    aprovados = cand[
        cand["teste_absoluto_passou"] & ~cand["duplicata_geografica"] & cand["filtro_forca_passou"]
        & ~cand["excluido_conhecimento_local"] & cand["filtro_vitalidade_passou"]
    ].sort_values("score_final", ascending=False)
    reprovados = cand[
        ~cand["teste_absoluto_passou"] | ~cand["filtro_forca_passou"]
        | cand["excluido_conhecimento_local"] | ~cand["filtro_vitalidade_passou"]
    ]
    LOGGER.info(
        "teste absoluto: %d passaram, %d reprovados; dedup geográfica: %d duplicatas suprimidas",
        cand["teste_absoluto_passou"].sum(), len(reprovados), cand["duplicata_geografica"].sum(),
    )

    # G2 — cap de MAX_POR_BAIRRO_TOP10 candidatos por bairro: o entregável serve pra visitar
    # 10 lugares diferentes, não 10 esquinas de 5 bairros. Excedentes bloqueados só pelo cap
    # (não por score) viram a seção "outros pontos do mesmo bairro" na ficha (Fase 9).
    top10, excedentes_bairro = aplicar_cap_bairro(aprovados, "bairro", MAX_POR_BAIRRO_TOP10, 10)
    top10 = top10.copy()
    cand["excedente_cap_bairro"] = cand.index.isin(excedentes_bairro.index)
    if len(top10) < 10:
        LOGGER.warning(
            "ACEITE — só %d candidatos sobraram pro Top 10 depois do cap de %d/bairro (esperado 10; "
            "%d excedentes bloqueados só pelo cap, não por score)",
            len(top10), MAX_POR_BAIRRO_TOP10, len(excedentes_bairro),
        )
    else:
        LOGGER.info(
            "ACEITE — Top 10 completo com 10 candidatos aprovados, %d bairros distintos, "
            "cap de %d/bairro respeitado (%d excedentes bloqueados só pelo cap)",
            top10["bairro"].nunique(), MAX_POR_BAIRRO_TOP10, len(excedentes_bairro),
        )

    # checagem de aceite: nenhum a menos de 1,5km de rede (já é garantido pela Fase 4, aqui é só auditoria)
    raio_bloqueio = cfg["concorrencia"]["raio_bloqueio_rede_m"]
    if (top10["dist_rede_mais_perto"] < raio_bloqueio).any():
        LOGGER.warning("ACEITE — existe candidato no Top 10 a menos de %dm de uma rede (não deveria, checar Fase 4)", raio_bloqueio)
    else:
        LOGGER.info("ACEITE — nenhum candidato do Top 10 a menos de %dm de rede", raio_bloqueio)

    # --- saídas -------------------------------------------------------
    colunas_saida = [
        "candidato_id", "bairro",
        "domicilios_captacao_efetivo", "pct_apartamento_captacao_efetivo", "renda_media_captacao_efetivo",
        "domicilios_efetivo", "pct_apartamento_efetivo", "renda_media_efetivo",
        "suspeita_estudantil", "dist_campus_km",
        "taxa_mortalidade", "vitalidade_comercial_score",
        "n_concorrentes_15min", "forca_concorrencia", "n_clinicas_sem_loja",
        "saturacao", "potencial_mensal", "demanda_capturada", "oferta_imovel_disponivel", "aluguel_estimado_regiao",
        "aluguel_e_estimado", "custo_fixo_mensal", "sacos_breakeven",
        "domicilios_entrega_viavel", "custo_por_entrega",
        "excluido_conhecimento_local", "fator_penalizacao_conhecimento_local", "motivo_penalizacao_conhecimento_local",
        "score_final_bruto", "acesso_score", "score_final", "teste_absoluto_passou", "teste_absoluto_motivo",
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
