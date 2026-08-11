"""
Fase 6 — Calibração por análogos (spec ESPEC_CLAUDE_CODE.md §6).

Substitui os pesos arbitrários (Parte 2 do PLANO_ESCOLHA_DO_PONTO.md: 40%
demanda / 25% saturação / 20% oferta de imóvel / 15% acesso) por pesos
derivados dos dados de Uberlândia: regressão de avaliações (proxy de
movimento) contra as variáveis do entorno de 1km de cada pet shop
independente da cidade.

Como a regressão não tem sinal nenhum sobre oferta de imóvel (não é uma
variável do entorno demográfico/concorrência) nem sobre acesso/hierarquia
viária, esses dois eixos permanecem no valor provisório do plano de negócio
— só a divisão entre "demanda" e "saturação" (65% do total) é recalibrada
pela regressão. Isso fica declarado explicitamente no pesos.json e vai para
a seção Metodologia do relatório (spec: "declarar método e confiança").

Saída: data/processed/pesos.json
Roda sozinho: `python pipeline/f6_calibracao.py`
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import statsmodels.api as sm

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_PROCESSED, get_logger, load_config, log_resumo_fase,
)

LOGGER = get_logger("fase6_calibracao")

RAIO_ENTORNO_M = 1000
N_MINIMO_REGRESSAO = 25
R2_MINIMO_REGRESSAO = 0.25

VARIAVEIS = ["domicilios", "pct_apartamento", "renda", "densidade", "n_concorrentes", "n_clinicas_sem_loja"]
VARIAVEIS_DEMANDA = ["domicilios", "pct_apartamento", "renda", "densidade"]
VARIAVEIS_SATURACAO = ["n_concorrentes", "n_clinicas_sem_loja"]

PESO_OFERTA_IMOVEL_PROVISORIO = 0.20  # Parte 2 do plano — regressão de análogos não cobre isso
PESO_ACESSO_PROVISORIO = 0.15         # idem
SOMA_DEMANDA_SATURACAO = 1.0 - PESO_OFERTA_IMOVEL_PROVISORIO - PESO_ACESSO_PROVISORIO  # 0.65


def _variaveis_entorno(pontos: gpd.GeoDataFrame, setores: gpd.GeoDataFrame, concorrentes: gpd.GeoDataFrame) -> pd.DataFrame:
    setores_centroides = setores.copy()
    setores_centroides["geometry"] = setores_centroides.geometry.centroid
    sindex_setores = setores_centroides.sindex
    sindex_conc = concorrentes.sindex

    linhas = []
    for idx, row in pontos.iterrows():
        buffer = row.geometry.buffer(RAIO_ENTORNO_M)

        idx_setores = list(sindex_setores.query(buffer, predicate="intersects"))
        sub = setores_centroides.iloc[idx_setores]
        dentro = sub[sub.geometry.within(buffer)]
        domicilios = dentro["domicilios_ocupados"].fillna(0).sum()
        peso = dentro["domicilios_ocupados"].fillna(0)
        soma_peso = peso.sum()
        pct_apto = float((dentro["pct_apartamento"].fillna(0) * peso).sum() / soma_peso) if soma_peso > 0 else 0.0
        renda = float((dentro["renda_media_responsavel"].fillna(0) * peso).sum() / soma_peso) if soma_peso > 0 else 0.0
        # densidade = domicílios / área REALMENTE ocupada por setores no buffer (não a área fixa
        # do círculo — senão fica perfeitamente colinear com domicílios, já que todo buffer tem o
        # mesmo raio; setores grandes e vazios dentro do buffer puxam a densidade pra baixo de
        # verdade, o que um denominador constante nunca captura).
        area_setores_m2 = setores.loc[dentro.index, "geometry"].area.sum() if len(dentro) else 0.0
        densidade = domicilios / (area_setores_m2 / 1e6) if area_setores_m2 > 0 else 0.0

        idx_conc = list(sindex_conc.query(buffer, predicate="intersects"))
        conc_sub = concorrentes.iloc[idx_conc]
        conc_sub = conc_sub[conc_sub.geometry.within(buffer) & (conc_sub.index != idx)]
        n_concorrentes = len(conc_sub)
        n_clinicas_sem_loja = len(conc_sub[(conc_sub["tipo"] == "veterinaria") & (conc_sub["vende_racao"] != True)])  # noqa: E712

        linhas.append({
            "domicilios": domicilios, "pct_apartamento": pct_apto, "renda": renda,
            "densidade": densidade, "n_concorrentes": n_concorrentes,
            "n_clinicas_sem_loja": n_clinicas_sem_loja,
        })
    return pd.DataFrame(linhas, index=pontos.index)


def _metodo_regressao(df: pd.DataFrame) -> dict | None:
    X = sm.add_constant(df[VARIAVEIS])
    y = df["log_avaliacoes"]
    modelo = sm.OLS(y, X).fit()

    r2 = modelo.rsquared
    LOGGER.info("regressão OLS: n=%d, R²=%.3f, R² ajustado=%.3f", len(df), r2, modelo.rsquared_adj)
    for v in VARIAVEIS:
        LOGGER.info("  %s: coef=%.5f p=%.3f", v, modelo.params[v], modelo.pvalues[v])

    vif = {}
    from statsmodels.stats.outliers_influence import variance_inflation_factor
    Xv = df[VARIAVEIS].assign(const=1.0)
    for i, v in enumerate(VARIAVEIS):
        try:
            vif[v] = float(variance_inflation_factor(Xv.values, i))
        except Exception as exc:  # colinearidade perfeita etc — registra e segue
            LOGGER.warning("VIF de %s não pôde ser calculado: %s", v, exc)
            vif[v] = None
    LOGGER.info("VIF: %s", vif)

    if len(df) < N_MINIMO_REGRESSAO or r2 < R2_MINIMO_REGRESSAO:
        LOGGER.warning(
            "regressão fraca demais para usar como peso (n=%d < %d ou R²=%.3f < %.2f) — caindo pro método de quartis",
            len(df), N_MINIMO_REGRESSAO, r2, R2_MINIMO_REGRESSAO,
        )
        return None

    coefs = {v: float(modelo.params[v]) for v in VARIAVEIS}
    return {
        "metodo": "regressao_ols",
        "n": len(df),
        "r2": float(r2),
        "r2_ajustado": float(modelo.rsquared_adj),
        "p_valores": {v: float(modelo.pvalues[v]) for v in VARIAVEIS},
        "vif": vif,
        "coeficientes_brutos": coefs,
        "confianca": "alta" if r2 >= 0.5 else "média",
    }


def _metodo_quartis(df: pd.DataFrame) -> dict:
    corte_sup = df["avaliacoes"].quantile(0.75)
    corte_inf = df["avaliacoes"].quantile(0.25)
    superior = df[df["avaliacoes"] >= corte_sup]
    inferior = df[df["avaliacoes"] <= corte_inf]
    LOGGER.info("método de quartis: %d no quartil superior, %d no inferior", len(superior), len(inferior))

    coefs = {}
    for v in VARIAVEIS:
        diff = superior[v].mean() - inferior[v].mean()
        desvio = df[v].std() or 1.0
        coefs[v] = float(diff / desvio)  # diferença normalizada — mesma unidade entre variáveis
    return {
        "metodo": "quartis",
        "n": len(df),
        "n_quartil_superior": len(superior),
        "n_quartil_inferior": len(inferior),
        "coeficientes_brutos": coefs,
        "confianca": "baixa",
    }


def _normalizar_pesos(coeficientes_brutos: dict) -> dict:
    """Só a magnitude importa pra virar peso — normaliza por soma dos valores absolutos."""
    abs_vals = {v: abs(c) for v, c in coeficientes_brutos.items()}
    soma = sum(abs_vals.values()) or 1.0
    return {v: abs_vals[v] / soma for v in coeficientes_brutos}


def _derivar_pesos_eixos(pesos_entorno: dict) -> dict:
    peso_demanda_bruto = sum(pesos_entorno[v] for v in VARIAVEIS_DEMANDA)
    peso_saturacao_bruto = sum(pesos_entorno[v] for v in VARIAVEIS_SATURACAO)
    soma = peso_demanda_bruto + peso_saturacao_bruto
    if soma == 0:
        peso_demanda_bruto = peso_saturacao_bruto = soma = 1.0
    return {
        "demanda_estimada": SOMA_DEMANDA_SATURACAO * (peso_demanda_bruto / soma),
        "saturacao": SOMA_DEMANDA_SATURACAO * (peso_saturacao_bruto / soma),
        "oferta_imovel": PESO_OFERTA_IMOVEL_PROVISORIO,
        "acesso": PESO_ACESSO_PROVISORIO,
    }


def run() -> Path:
    cfg = load_config()  # G3 (CORRECOES.md) — bug: retorno não era atribuído antes; necessário
    # a partir de C1.4 pra ler cfg["vitalidade_comercial"]["peso_eixo"]; sem efeito nesta fase ainda.
    LOGGER.info("=== Fase 6: calibração por análogos ===")

    concorrentes = gpd.read_file(DATA_PROCESSED / "concorrentes.gpkg")
    setores = gpd.read_file(DATA_PROCESSED / "setores.gpkg")

    independentes = concorrentes[
        (concorrentes["tipo"] == "pet_shop") & concorrentes["avaliacoes"].notna()
    ].copy()
    independentes["avaliacoes"] = pd.to_numeric(independentes["avaliacoes"], errors="coerce")
    independentes = independentes.dropna(subset=["avaliacoes"])
    independentes["log_avaliacoes"] = np.log1p(independentes["avaliacoes"])
    LOGGER.info("pet shops independentes com avaliações conhecidas: %d", len(independentes))

    entorno = _variaveis_entorno(independentes, setores, concorrentes)
    df = independentes[["avaliacoes", "log_avaliacoes"]].join(entorno)

    resultado = None
    if len(df) >= N_MINIMO_REGRESSAO:
        resultado = _metodo_regressao(df)
    else:
        LOGGER.warning("amostra pequena demais para regressão (n=%d < %d) — indo direto pro método de quartis", len(df), N_MINIMO_REGRESSAO)
    if resultado is None:
        resultado = _metodo_quartis(df)

    pesos_entorno = _normalizar_pesos(resultado["coeficientes_brutos"])
    pesos_eixos = _derivar_pesos_eixos(pesos_entorno)

    saida = {
        "metodo": resultado["metodo"],
        "confianca": resultado["confianca"],
        "n_amostra": resultado["n"],
        "detalhes_estatisticos": {k: v for k, v in resultado.items() if k not in ("metodo", "confianca", "n", "coeficientes_brutos")},
        "pesos_variaveis_entorno": pesos_entorno,
        "pesos_eixos_score_final": pesos_eixos,
        "nota_metodologica": (
            "A regressão de análogos (avaliações ~ variáveis do entorno de 1km dos pet shops "
            "independentes de Uberlândia) não tem nenhuma variável de oferta de imóvel nem de "
            "hierarquia viária/acesso — por isso só a divisão entre os eixos 'demanda estimada' e "
            "'saturação' (65% do peso total) foi recalibrada pelos dados locais. 'Oferta de imóvel' "
            "(20%) e 'acesso' (15%) permanecem nos valores provisórios do plano de negócio "
            "(PLANO_ESCOLHA_DO_PONTO.md, Parte 2)."
        ),
    }

    out_path = DATA_PROCESSED / "pesos.json"
    out_path.write_text(json.dumps(saida, indent=2, ensure_ascii=False), encoding="utf-8")
    LOGGER.info("gravado: %s — método=%s confiança=%s", out_path, saida["metodo"], saida["confianca"])
    LOGGER.info("pesos dos eixos finais: %s", {k: round(v, 3) for k, v in pesos_eixos.items()})

    log_resumo_fase(LOGGER, entrada=len(independentes), saida=len(df), descartados=len(independentes) - len(df))
    return out_path


if __name__ == "__main__":
    run()
