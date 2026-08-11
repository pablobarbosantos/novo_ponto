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
    # NOTA: usa domicilios_efetivo/pct_apartamento_efetivo — o combinado genérico de 3
    # anéis do B1 (CORRECOES.md). Substituído pela captação de balcão específica do C4.1
    # (CORRECOES_2.md, só anéis 5/10min) mais adiante no plano de correções.
    taxa_posse = cfg["negocio"]["taxa_posse_pet_domicilio"]
    gasto_medio = cfg["negocio"]["gasto_medio_mensal_por_pet"]
    fator_verticalizacao = 1.0 + 0.4 * cand["pct_apartamento_efetivo"].fillna(0)
    potencial = cand["domicilios_efetivo"] * taxa_posse * fator_verticalizacao * gasto_medio
    return potencial.where(cand["domicilios_efetivo"].notna())  # sem isócrona => sem demanda calculável, não 0


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

PESO_ANEL_CAPTACAO = {5: 1.00, 10: 0.35, 15: 0.10}  # espelha PESO_ANEL do B1 (f5) — trocado por C4.1 mais adiante
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

    cand = gpd.read_file(DATA_PROCESSED / "candidatos_com_demanda.gpkg", layer="candidatos_com_demanda")
    concorrentes = gpd.read_file(DATA_PROCESSED / "concorrentes.gpkg")
    vias = gpd.read_file(DATA_PROCESSED / "vias.gpkg")
    setores = gpd.read_file(DATA_PROCESSED / "setores.gpkg")
    n_entrada = len(cand)

    # 7.1
    cand["potencial_mensal"] = _demanda_estimada(cand, cfg)

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
        "candidato_id", "bairro", "domicilios_efetivo", "pct_apartamento_efetivo", "renda_media_efetivo",
        "n_concorrentes_15min", "forca_concorrencia", "n_clinicas_sem_loja",
        "saturacao", "potencial_mensal", "demanda_capturada", "oferta_imovel_disponivel", "aluguel_estimado_regiao",
        "aluguel_e_estimado", "custo_fixo_mensal", "sacos_breakeven",
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
