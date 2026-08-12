"""
Fase 4 — Geração de candidatos (spec ESPEC_CLAUDE_CODE.md §6).

Não usa pontos escolhidos à mão: gera pontos a cada 400m ao longo das vias
secondary/tertiary/residential de maior conectividade dentro do perímetro
urbano, descarta por regras duras (rede, bairro vetado, setor sem domicílio/
renda baixa, buffer de domicílios abaixo do piso) e mantém os melhores por um
score preliminar multidimensional barato (buffer de 1,5km), com cota por
bairro pra não deixar um punhado de bairros densos ocupar o pool inteiro.

Saída: data/processed/candidatos.gpkg, data/processed/cobertura_bairros.csv
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

# A2 (CORRECOES_3.md) — cota por bairro no corte PRELIMINAR (pool de 300 candidatos que a
# Fase 7 vai avaliar). Não confundir com MAX_POR_BAIRRO_TOP10 em _common.py, que é o cap do
# TOP 10 final — dois problemas diferentes: esse aqui evita que um bairro denso monopolize o
# orçamento de candidatos antes mesmo da avaliação; aquele evita que o Top10 final concentre.
CANDIDATOS_POR_BAIRRO_MAX = 12

# A3 (CORRECOES_3.md) — validação obrigatória: estes três bairros foram identificados como
# eliminados antes de serem avaliados na execução que motivou o CORRECOES_3.md. Rastreados
# em cada etapa do funil pra confirmar que a correção resolveu o problema — e, se algum ainda
# sumir, pra dizer exatamente em que etapa e por quê, em vez de só constatar a ausência.
BAIRROS_VALIDACAO_A3 = ["Granja Marileusa", "Umuarama", "Alto Umuarama"]

# A5 (CORRECOES_3.md) — funil de etapas rastreado pra seção "Cobertura da análise" do
# relatório (f9_relatorio.py) e pra validação do A3 acima. Cada etapa é (chave, motivo
# atribuído a um bairro que estava presente na etapa anterior e não está mais nesta).
ETAPAS_COBERTURA = [
    ("pontos_gerados", None),
    ("apos_filtro_rede", "todos os pontos do bairro ficaram perto demais de uma rede conhecida (Petz/Cobasi)"),
    ("apos_exclusao_conhecimento_local", "bairro vetado por conhecimento local (C2)"),
    ("apos_filtro_setor", "setor sem domicílio ocupado, ou renda do responsável abaixo do piso"),
    ("apos_piso_domicilios_buffer", "abaixo do piso de domicílios no buffer de 1500m (A4, CORRECOES_3.md)"),
    ("apos_cota_bairro", "não entrou no corte final de candidatos por score preliminar/cota por bairro"),
]


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
# A1 (CORRECOES_3.md) + filtros duros
# ---------------------------------------------------------------------------

def _filtrar_candidatos(
    pontos_com_setor: gpd.GeoDataFrame,
    concorrentes: gpd.GeoDataFrame,
    cfg: dict,
    etapas: dict[str, set],
) -> tuple[gpd.GeoDataFrame, dict]:
    """pontos_com_setor já vem com NM_BAIRRO/CD_SETOR/domicilios_ocupados/renda_media_responsavel
    (join feito em run() ANTES de qualquer filtro, pra permitir rastrear cobertura por bairro
    desde o primeiro ponto gerado — ver A5)."""
    log_filtros = {"entrada": len(pontos_com_setor)}

    redes = concorrentes[concorrentes["tipo"] == "rede"]
    raio_bloqueio = cfg["concorrencia"]["raio_bloqueio_rede_m"]
    if not redes.empty:
        dist_rede = np.minimum.reduce([pontos_com_setor.geometry.distance(r.geometry).values for _, r in redes.iterrows()])
    else:
        dist_rede = np.full(len(pontos_com_setor), np.inf)
    pontos_com_setor = pontos_com_setor.assign(dist_rede_mais_perto=dist_rede)
    sobrou_rede = pontos_com_setor[pontos_com_setor["dist_rede_mais_perto"] >= raio_bloqueio].copy()
    log_filtros["apos_filtro_rede"] = len(sobrou_rede)
    etapas["apos_filtro_rede"] = set(sobrou_rede["NM_BAIRRO"].dropna().unique())
    LOGGER.info("filtro rede (>=%dm): %d -> %d", raio_bloqueio, len(pontos_com_setor), len(sobrou_rede))

    # zoneamento: sem camada vetorial (Fase 3 registrou a limitação) — não dá pra filtrar
    # por zona incompatível sem inventar polígono (spec §3.2/§6). Pendência manual por candidato.
    sobrou_rede["zoneamento_verificado"] = False

    # A1 (CORRECOES_3.md) — exclusões de conhecimento local ANTES do corte preliminar, não
    # só na Fase 7. Antes, um bairro vetado (ex.: Presidente Roosevelt) consumia até um terço
    # do orçamento de 300 candidatos com isócrona/score calculados sem necessidade, e só era
    # removido na Fase 7 — tarde demais pra sobrar orçamento pros bairros que a exclusão não
    # tocava.
    excluidos_cfg = {b["nome"] for b in (cfg.get("conhecimento_local") or {}).get("bairros_excluidos", [])}
    if excluidos_cfg:
        antes = len(sobrou_rede)
        sobrou_a1 = sobrou_rede[~sobrou_rede["NM_BAIRRO"].isin(excluidos_cfg)].copy()
        LOGGER.info(
            "A1 — conhecimento local: removidos %d pontos em %d bairro(s) vetado(s) (%s) — %d -> %d",
            antes - len(sobrou_a1), len(excluidos_cfg), sorted(excluidos_cfg), antes, len(sobrou_a1),
        )
    else:
        sobrou_a1 = sobrou_rede
    log_filtros["apos_exclusao_conhecimento_local"] = len(sobrou_a1)
    etapas["apos_exclusao_conhecimento_local"] = set(sobrou_a1["NM_BAIRRO"].dropna().unique())

    tem_domicilio = sobrou_a1["domicilios_ocupados"].fillna(0) > 0
    piso_renda = cfg["candidatos"]["renda_minima_responsavel"]
    renda_ok = sobrou_a1["renda_media_responsavel"].isna() | (sobrou_a1["renda_media_responsavel"] >= piso_renda)
    sobrou_setor = sobrou_a1[tem_domicilio & renda_ok].copy()
    log_filtros["apos_filtro_setor"] = len(sobrou_setor)
    etapas["apos_filtro_setor"] = set(sobrou_setor["NM_BAIRRO"].dropna().unique())
    LOGGER.info(
        "filtro setor (domicílios>0 e renda>=%.0f ou desconhecida): %d -> %d",
        piso_renda, len(sobrou_a1), len(sobrou_setor),
    )

    return sobrou_setor, log_filtros


# ---------------------------------------------------------------------------
# A3 (CORRECOES_3.md) — métricas de buffer euclidiano de 1,5km (base do piso de
# domicílios A4 e do score preliminar multidimensional)
# ---------------------------------------------------------------------------

def _metricas_buffer(pontos: gpd.GeoDataFrame, setores: gpd.GeoDataFrame, concorrentes: gpd.GeoDataFrame, buffer_m: float) -> gpd.GeoDataFrame:
    setores_centroides = setores.copy()
    setores_centroides["geometry"] = setores_centroides.geometry.centroid
    sindex = setores_centroides.sindex
    conc_sindex = concorrentes.sindex if len(concorrentes) else None

    dom_list, renda_list, pct_apto_list, n_conc_list = [], [], [], []
    for geom in pontos.geometry:
        buffer = geom.buffer(buffer_m)
        idx = list(sindex.query(buffer, predicate="intersects"))
        if idx:
            sub = setores_centroides.iloc[idx]
            dentro = sub[sub.geometry.within(buffer)]
            dom = float(dentro["domicilios_ocupados"].fillna(0).sum())
            if dom > 0:
                pesos = dentro["domicilios_ocupados"].fillna(0)
                renda = float((dentro["renda_media_responsavel"].fillna(0) * pesos).sum() / dom)
                pct_apto = float((dentro["pct_apartamento"].fillna(0) * pesos).sum() / dom)
            else:
                renda, pct_apto = np.nan, np.nan
        else:
            dom, renda, pct_apto = 0.0, np.nan, np.nan
        dom_list.append(dom)
        renda_list.append(renda)
        pct_apto_list.append(pct_apto)

        if conc_sindex is not None:
            cidx = list(conc_sindex.query(buffer, predicate="intersects"))
            n_conc = int(concorrentes.iloc[cidx].geometry.within(buffer).sum()) if cidx else 0
        else:
            n_conc = 0
        n_conc_list.append(n_conc)

    return pontos.assign(
        domicilios_1500m=dom_list,
        renda_media_1500m=renda_list,
        pct_apartamento_1500m=pct_apto_list,
        n_concorrentes_1500m=n_conc_list,
    )


def _score_preliminar(pontos: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """A3 (CORRECOES_3.md) — score preliminar multidimensional, percentil dentro do próprio
    pool sendo avaliado nesta etapa (não do município inteiro). Antes era só soma de
    domicílios no buffer — densidade pura, sem renda/verticalização/concorrência, o que
    apagava bairros de renda alta e densidade média (ex.: Granja Marileusa) antes da
    avaliação real. Ainda barato: só buffer euclidiano, sem isócrona."""
    pct_dom = pontos["domicilios_1500m"].rank(pct=True, na_option="bottom")
    pct_renda = pontos["renda_media_1500m"].rank(pct=True, na_option="bottom")
    pct_apto = pontos["pct_apartamento_1500m"].rank(pct=True, na_option="bottom")
    pct_conc = (1.0 / (1.0 + pontos["n_concorrentes_1500m"])).rank(pct=True, na_option="bottom")
    pontos = pontos.assign(score_preliminar=(
        0.35 * pct_dom + 0.30 * pct_renda + 0.20 * pct_apto + 0.15 * pct_conc
    ))
    return pontos


# ---------------------------------------------------------------------------
# A2 (CORRECOES_3.md) — cota por bairro no corte final do pool preliminar
# ---------------------------------------------------------------------------

def _selecionar_com_cota_bairro(sobrou: gpd.GeoDataFrame, top_n: int, max_por_bairro: int) -> gpd.GeoDataFrame:
    sobrou = sobrou.sort_values("score_preliminar", ascending=False)
    # 1ª passada: até max_por_bairro por bairro, os melhores de cada
    top = sobrou.groupby("NM_BAIRRO", group_keys=False).head(max_por_bairro)
    if len(top) < top_n:
        # 2ª passada: completa com os melhores globais ainda não escolhidos
        resto = sobrou.drop(index=top.index)
        top = pd.concat([top, resto.head(top_n - len(top))])
    else:
        top = top.sort_values("score_preliminar", ascending=False).head(top_n)
    top = top.sort_values("score_preliminar", ascending=False)
    LOGGER.info(
        "A2 — cota por bairro: %d candidatos em %d bairros distintos (máx %d por bairro)",
        len(top), top["NM_BAIRRO"].nunique(), max_por_bairro,
    )
    return top


# ---------------------------------------------------------------------------
# A3 — validação obrigatória (bairros que motivaram o CORRECOES_3.md)
# ---------------------------------------------------------------------------

def _validar_bairros_alvo(etapas: dict[str, set]) -> None:
    ordem = [k for k, _ in ETAPAS_COBERTURA]
    for bairro in BAIRROS_VALIDACAO_A3:
        ultima_etapa_presente = None
        etapa_ausencia = None
        for etapa in ordem:
            presentes = etapas.get(etapa, set())
            if bairro in presentes:
                ultima_etapa_presente = etapa
            elif ultima_etapa_presente is not None and etapa_ausencia is None:
                etapa_ausencia = etapa
        if ultima_etapa_presente is None:
            LOGGER.warning(
                "A3 validação — '%s' nunca apareceu em nenhuma etapa do funil (nem em pontos_gerados) — "
                "checar se o nome do bairro em setores.gpkg bate exatamente com este", bairro,
            )
        elif etapa_ausencia is not None:
            motivo = dict(ETAPAS_COBERTURA).get(etapa_ausencia, "motivo desconhecido")
            LOGGER.warning(
                "A3 validação — '%s' desapareceu do pool na etapa '%s' (%s) — presente até '%s'",
                bairro, etapa_ausencia, motivo, ultima_etapa_presente,
            )
        else:
            LOGGER.info("A3 validação — OK: '%s' presente no pool final (%s)", bairro, ordem[-1])


# ---------------------------------------------------------------------------
# A5 — grava cobertura por bairro pra seção do relatório (f9_relatorio.py)
# ---------------------------------------------------------------------------

def _gravar_cobertura_bairros(etapas: dict[str, set], setores: gpd.GeoDataFrame) -> Path:
    bairros_cidade = sorted(setores["NM_BAIRRO"].dropna().unique())
    ordem = [k for k, _ in ETAPAS_COBERTURA]
    motivos = dict(ETAPAS_COBERTURA)

    # contexto por bairro (renda média ponderada por domicílios, domicílios totais) — usado
    # pro relatório destacar bairros de renda alta que não foram avaliados.
    ctx = (
        setores.assign(_peso=setores["domicilios_ocupados"].fillna(0))
        .groupby("NM_BAIRRO")
        .apply(lambda g: pd.Series({
            "domicilios_ocupados_bairro": g["_peso"].sum(),
            "renda_media_responsavel_bairro": (
                (g["renda_media_responsavel"].fillna(0) * g["_peso"]).sum() / g["_peso"].sum()
                if g["_peso"].sum() > 0 else np.nan
            ),
        }), include_groups=False)
    )

    linhas = []
    for bairro in bairros_cidade:
        ultima_etapa_presente = None
        etapa_ausencia = None
        for etapa in ordem:
            presentes = etapas.get(etapa, set())
            if bairro in presentes:
                ultima_etapa_presente = etapa
            elif ultima_etapa_presente is not None and etapa_ausencia is None:
                etapa_ausencia = etapa
        avaliado = ultima_etapa_presente == ordem[-1]
        if avaliado:
            motivo = None
        elif ultima_etapa_presente is None:
            motivo = "sem pontos gerados no bairro (sem vias secundárias/terciárias/residenciais conectadas dentro do perímetro urbano, ou setor sem geometria casada)"
        else:
            motivo = motivos.get(etapa_ausencia, "motivo desconhecido")
        linha = {"bairro": bairro, "avaliado": avaliado, "motivo_nao_avaliado": motivo}
        if bairro in ctx.index:
            linha["domicilios_ocupados_bairro"] = ctx.loc[bairro, "domicilios_ocupados_bairro"]
            linha["renda_media_responsavel_bairro"] = ctx.loc[bairro, "renda_media_responsavel_bairro"]
        else:
            linha["domicilios_ocupados_bairro"] = np.nan
            linha["renda_media_responsavel_bairro"] = np.nan
        linhas.append(linha)

    df = pd.DataFrame(linhas)
    limite_q3 = df["renda_media_responsavel_bairro"].quantile(0.75)
    df["renda_quartil_superior"] = df["renda_media_responsavel_bairro"] >= limite_q3

    out_path = DATA_PROCESSED / "cobertura_bairros.csv"
    df.to_csv(out_path, sep=";", index=False, encoding="utf-8-sig")

    n_avaliados = int(df["avaliado"].sum())
    falsos_negativos = df[~df["avaliado"] & df["renda_quartil_superior"]]
    LOGGER.info(
        "A5 — cobertura: %d/%d bairros da cidade avaliados; %d bairro(s) de renda no quartil "
        "superior (>=R$%.0f) NÃO avaliados: %s",
        n_avaliados, len(df), len(falsos_negativos), limite_q3,
        sorted(falsos_negativos["bairro"]) or "nenhum",
    )
    LOGGER.info("gravado: %s", out_path)
    return out_path


# ---------------------------------------------------------------------------

def run() -> Path:
    cfg = load_config()
    espacamento = cfg["candidatos"]["espacamento_via_m"]
    buffer_m = cfg["candidatos"]["buffer_score_preliminar_m"]
    top_n = cfg["candidatos"]["top_n_preliminar"]
    piso_dom_buffer = cfg["candidatos"].get("domicilios_minimos_buffer_1500m", 0)

    LOGGER.info("=== Fase 4: geração de candidatos ===")

    vias = gpd.read_file(DATA_PROCESSED / "vias.gpkg")
    perimetro = gpd.read_file(DATA_PROCESSED / "perimetro_urbano.gpkg")
    concorrentes = gpd.read_file(DATA_PROCESSED / "concorrentes.gpkg")
    setores = gpd.read_file(DATA_PROCESSED / "setores.gpkg")

    vias_conectadas = _selecionar_vias_conectadas(vias, perimetro)
    pontos = _gerar_pontos_ao_longo(vias_conectadas, espacamento)

    # A5 (CORRECOES_3.md) — join com setor feito logo na geração dos pontos, ANTES de
    # qualquer filtro, pra poder rastrear em qual etapa cada bairro desaparece do funil
    # (base tanto da validação do A3 quanto da seção "Cobertura da análise" do relatório).
    juncao_inicial = gpd.sjoin_nearest(
        pontos, setores[["CD_SETOR", "NM_BAIRRO", "domicilios_ocupados", "renda_media_responsavel", "geometry"]],
        how="left", distance_col="dist_setor",
    )
    juncao_inicial = juncao_inicial[~juncao_inicial.index.duplicated(keep="first")]

    etapas: dict[str, set] = {"pontos_gerados": set(juncao_inicial["NM_BAIRRO"].dropna().unique())}

    sobrou, log_filtros = _filtrar_candidatos(juncao_inicial, concorrentes, cfg, etapas)

    n_bairros = sobrou["NM_BAIRRO"].nunique()
    if len(sobrou) < 150 or n_bairros <= 8:
        LOGGER.warning(
            "ACEITE — só %d candidatos em %d bairros (mínimo: 150 em >8) — afrouxando filtro de renda e tentando de novo",
            len(sobrou), n_bairros,
        )
        cfg_afrouxado = dict(cfg)
        cfg_afrouxado["candidatos"] = {**cfg["candidatos"], "renda_minima_responsavel": 0}
        sobrou, log_filtros = _filtrar_candidatos(juncao_inicial, concorrentes, cfg_afrouxado, etapas)
        n_bairros = sobrou["NM_BAIRRO"].nunique()
        LOGGER.info("após afrouxar: %d candidatos em %d bairros", len(sobrou), n_bairros)

    # A4 (CORRECOES_3.md) — piso de domicílios no buffer de 1,5km, não de renda: o piso de
    # renda_minima_responsavel já barra o extremo pobre, mas nada barrava um ponto isolado
    # em área cara-e-vazia. Calcula as 4 métricas de buffer uma vez só (reaproveitadas pelo
    # score preliminar do A3 logo abaixo) e corta por domicilios_1500m antes de gastar o
    # resto do cálculo em pontos que não sustentam uma loja de rua.
    sobrou = _metricas_buffer(sobrou, setores, concorrentes, buffer_m)
    antes_a4 = sobrou
    sobrou = sobrou[sobrou["domicilios_1500m"] >= piso_dom_buffer].copy()
    descartados_a4 = antes_a4[antes_a4["domicilios_1500m"] < piso_dom_buffer]
    LOGGER.info(
        "A4 — piso de domicílios no buffer de 1500m (>=%d): %d -> %d",
        piso_dom_buffer, len(antes_a4), len(sobrou),
    )
    if not descartados_a4.empty:
        contagem_a4 = descartados_a4["NM_BAIRRO"].value_counts().to_dict()
        LOGGER.info("A4 — %d ponto(s) descartado(s) por bairro: %s", len(descartados_a4), contagem_a4)
    etapas["apos_piso_domicilios_buffer"] = set(sobrou["NM_BAIRRO"].dropna().unique())

    # A3 — score preliminar multidimensional (reaproveita domicilios_1500m já calculado)
    sobrou = _score_preliminar(sobrou)

    # A2 — cota por bairro no corte final
    top = _selecionar_com_cota_bairro(sobrou, top_n, CANDIDATOS_POR_BAIRRO_MAX)
    etapas["apos_cota_bairro"] = set(top["NM_BAIRRO"].dropna().unique())

    top["candidato_id"] = [f"C{i:04d}" for i in range(1, len(top) + 1)]
    top = top[[
        "candidato_id", "NM_BAIRRO", "CD_SETOR", "score_preliminar",
        "dist_rede_mais_perto", "domicilios_ocupados", "renda_media_responsavel",
        "domicilios_1500m", "renda_media_1500m", "pct_apartamento_1500m", "n_concorrentes_1500m",
        "zoneamento_verificado", "geometry",
    ]].rename(columns={"NM_BAIRRO": "bairro"}).reset_index(drop=True)

    n_bairros_final = top["bairro"].nunique()
    maior_bairro = top["bairro"].value_counts().max() if len(top) else 0
    if len(top) < 150:
        LOGGER.warning("ACEITE — mesmo após afrouxar, só %d candidatos sobreviveram (mínimo 150)", len(top))
    if n_bairros_final <= 8:
        LOGGER.warning("ACEITE — candidatos finais cobrem só %d bairros (mínimo >8)", n_bairros_final)
    if len(top) >= 150 and n_bairros_final > 8:
        LOGGER.info("ACEITE — %d candidatos em %d bairros distintos (mínimo 150/>8)", len(top), n_bairros_final)

    # A2/CORRECOES_3.md — aceite final mais rígido: >=25 bairros, nenhum com mais de
    # CANDIDATOS_POR_BAIRRO_MAX, zero candidatos em bairro vetado.
    excluidos_cfg = {b["nome"] for b in (cfg.get("conhecimento_local") or {}).get("bairros_excluidos", [])}
    n_em_vetado = int(top["bairro"].isin(excluidos_cfg).sum())
    if n_bairros_final < 25:
        LOGGER.warning("ACEITE (CORRECOES_3.md) — pool cobre só %d bairros distintos (alvo: >=25)", n_bairros_final)
    else:
        LOGGER.info("ACEITE (CORRECOES_3.md) — pool cobre %d bairros distintos (alvo: >=25)", n_bairros_final)
    if maior_bairro > CANDIDATOS_POR_BAIRRO_MAX:
        LOGGER.warning("ACEITE (CORRECOES_3.md) — algum bairro tem %d candidatos, acima do máximo %d", maior_bairro, CANDIDATOS_POR_BAIRRO_MAX)
    if n_em_vetado > 0:
        LOGGER.warning("ACEITE (CORRECOES_3.md) — %d candidato(s) do pool final ainda em bairro vetado — A1 falhou", n_em_vetado)
    else:
        LOGGER.info("ACEITE (CORRECOES_3.md) — OK: zero candidatos em bairro vetado no pool final")

    _validar_bairros_alvo(etapas)
    _gravar_cobertura_bairros(etapas, setores)

    out_path = DATA_PROCESSED / "candidatos.gpkg"
    top.to_file(out_path, layer="candidatos", driver="GPKG")
    LOGGER.info("gravado: %s", out_path)

    log_resumo_fase(
        LOGGER, entrada=log_filtros["entrada"], saida=len(top),
        descartados=log_filtros["entrada"] - len(top),
        motivo_descarte="fora do perímetro/baixa conectividade, perto de rede, bairro vetado (A1), setor sem domicílio/renda baixa, abaixo do piso de domicílios no buffer (A4), ou fora do corte final/cota por bairro (A2)",
    )
    return out_path


if __name__ == "__main__":
    run()
