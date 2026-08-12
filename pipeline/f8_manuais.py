"""
Fase 8 — Incorporação das entradas manuais (spec ESPEC_CLAUDE_CODE.md §6).

Lê tudo que existir em data/manual/, casa por candidato_id ou bairro, e
anexa como colunas extras no ranking. Nenhuma delas é obrigatória (spec §5)
— nesta rodada todas estão vazias (só cabeçalho), então tudo fica marcado
"não coletado" no relatório. O pipeline não aborta por isso (spec §1.3); só
registra a pendência e segue. Quando o usuário preencher os CSVs, rodar de
novo enriquece o ranking sem refazer nenhuma fase anterior.

Quando geoteste.csv tiver dado, reordena os 5 primeiros do Top 10 por custo
por conversa — é o único indicador medido, não estimado (spec §6 Fase 8).

Saída: output/top10.csv (enriquecido), data/processed/ranking_completo.parquet (idem)
Roda sozinho: `python pipeline/f8_manuais.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_MANUAL, DATA_PROCESSED, OUTPUT_DIR,
    aplicar_cap_bairro, get_logger, load_config, log_resumo_fase,
)

LOGGER = get_logger("fase8_manuais")
MAX_POR_BAIRRO_TOP10 = 2  # G2 (CORRECOES.md) — mesmo cap da Fase 7; tem que replicar aqui,
# senão o repique do Top10 depois das entradas manuais reintroduz a concentração por bairro.

ARQUIVOS_MANUAIS = [
    "imoveis.csv", "ifood.csv", "meta_publico.csv", "google_buscas.csv",
    "campo_fluxo.csv", "cliente_oculto.csv", "geoteste.csv", "marcas_carteira.csv",
]


def _ler_manual(nome: str) -> pd.DataFrame | None:
    path = DATA_MANUAL / nome
    if not path.exists():
        LOGGER.warning("%s não existe — pendência (spec cria o template vazio no bootstrap)", nome)
        return None
    df = pd.read_csv(path)
    if df.empty:
        LOGGER.info("%s existe mas está vazio (só cabeçalho) — pendente de preenchimento manual", nome)
        return None
    LOGGER.info("%s preenchido: %d linha(s)", nome, len(df))
    return df


def _anexar_imoveis(ranking: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    df = _ler_manual("imoveis.csv")
    ranking["n_imoveis_no_teto"] = pd.NA
    ranking["reais_m2_medio"] = pd.NA
    ranking["aluguel_mediano_bairro"] = pd.NA
    if df is None:
        return ranking
    teto = cfg["negocio"]["teto_aluguel"]
    df["reais_m2"] = df["aluguel"] / df["area_m2"]
    agrupado = df.groupby("bairro").agg(
        n_imoveis_no_teto=("aluguel", lambda s: (s <= teto).sum()),
        reais_m2_medio=("reais_m2", "median"),
        aluguel_mediano_bairro=("aluguel", "median"),
    )
    ranking = ranking.drop(columns=["n_imoveis_no_teto", "reais_m2_medio", "aluguel_mediano_bairro"]).merge(
        agrupado, on="bairro", how="left"
    )
    return ranking


def _anexar_por_candidato_id(ranking: pd.DataFrame, nome_arquivo: str, colunas_agregar: dict) -> pd.DataFrame:
    df = _ler_manual(nome_arquivo)
    for col in colunas_agregar:
        ranking[col] = pd.NA
    if df is None or "candidato_id" not in df.columns:
        if df is not None:
            LOGGER.warning("%s preenchido mas sem coluna candidato_id — não dá pra casar, ignorando", nome_arquivo)
        return ranking
    agrupado = df.groupby("candidato_id").agg(**colunas_agregar)
    ranking = ranking.drop(columns=list(colunas_agregar)).merge(agrupado, on="candidato_id", how="left")
    return ranking


def _anexar_geoteste(ranking: pd.DataFrame) -> tuple[pd.DataFrame, bool]:
    df = _ler_manual("geoteste.csv")
    ranking["custo_por_conversa"] = pd.NA
    if df is None or "regiao" not in df.columns:
        return ranking, False
    agrupado = df.groupby("regiao").agg(custo_por_conversa=("custo_por_conversa", "mean"))
    agrupado.index.name = "bairro"
    ranking = ranking.drop(columns=["custo_por_conversa"]).merge(agrupado, on="bairro", how="left")
    return ranking, True


def _anexar_marcas_bloqueadas(ranking: pd.DataFrame) -> pd.DataFrame:
    df = _ler_manual("marcas_carteira.csv")
    ranking["marcas_bloqueadas_no_bairro"] = pd.NA
    if df is None or "regiao_bloqueada" not in df.columns:
        return ranking
    por_bairro = df.groupby("regiao_bloqueada")["marca"].apply(lambda s: ", ".join(sorted(set(s))))
    por_bairro.index.name = "bairro"
    ranking = ranking.drop(columns=["marcas_bloqueadas_no_bairro"]).merge(
        por_bairro.rename("marcas_bloqueadas_no_bairro"), on="bairro", how="left"
    )
    return ranking


def run() -> tuple[Path, Path]:
    cfg = load_config()
    LOGGER.info("=== Fase 8: entradas manuais ===")

    ranking_path = DATA_PROCESSED / "ranking_completo.parquet"
    ranking = pd.read_parquet(ranking_path)
    n_entrada = len(ranking)

    pendentes, preenchidos = [], []
    for nome in ARQUIVOS_MANUAIS:
        (preenchidos if _ler_manual(nome) is not None else pendentes).append(nome)

    ranking = _anexar_imoveis(ranking, cfg)
    ranking = _anexar_por_candidato_id(ranking, "meta_publico.csv", {"publico_estimado_meta": ("publico_estimado", "mean")})
    ranking = _anexar_por_candidato_id(ranking, "google_buscas.csv", {"volume_busca_mensal": ("volume_mensal", "sum")})
    ranking = _anexar_por_candidato_id(
        ranking, "campo_fluxo.csv",
        {"pedestres_15min_medio": ("pedestres_15min", "mean"), "carros_15min_medio": ("carros_15min", "mean")},
    )
    ranking, geoteste_preenchido = _anexar_geoteste(ranking)
    ranking = _anexar_marcas_bloqueadas(ranking)

    # ifood.csv e cliente_oculto.csv são referência de mercado (não casam 1:1 com candidato_id/bairro
    # de forma direta e confiável sem normalização de nome de região) — ficam disponíveis para a
    # Fase 9 citar como fonte na seção de metodologia/limitações, não como coluna do ranking.

    # a Fase 7 já decidiu quem é aprovado e sobrevive à dedup geográfica de 800m — reaplicar aqui
    # é obrigatório, senão candidatos vizinhos suprimidos como duplicata voltam a aparecer só
    # porque o ranking completo (300 linhas) inclui todo mundo, não só quem passou nos filtros.
    # O mesmo vale pro cap de bairro (G2) e pro filtro de força de concorrência (C5): sem
    # reaplicar, o repique abaixo reintroduz o que os filtros da Fase 7 já tinham resolvido.
    # filtro_vitalidade_passou (C1.4) e excluido_conhecimento_local (C2) só existem depois
    # que essas correções rodarem em f7 — checagem defensiva pra este arquivo continuar
    # funcionando em qualquer etapa do plano de correções.
    filtro_forca = ranking["filtro_forca_passou"] if "filtro_forca_passou" in ranking.columns else True
    filtro_vitalidade = ranking["filtro_vitalidade_passou"] if "filtro_vitalidade_passou" in ranking.columns else True
    nao_excluido_local = ~ranking["excluido_conhecimento_local"] if "excluido_conhecimento_local" in ranking.columns else True
    aprovados = ranking[
        ranking["teste_absoluto_passou"] & ~ranking["duplicata_geografica"] & filtro_forca
        & filtro_vitalidade & nao_excluido_local
    ].sort_values("score_final", ascending=False)
    top10_capado, excedentes_bairro = aplicar_cap_bairro(aprovados, "bairro", MAX_POR_BAIRRO_TOP10, 10)
    LOGGER.info(
        "G2 — cap de %d/bairro reaplicado: %d candidatos no Top10, %d bairros distintos, %d excedentes",
        MAX_POR_BAIRRO_TOP10, len(top10_capado), top10_capado["bairro"].nunique(), len(excedentes_bairro),
    )

    if geoteste_preenchido:
        top5_reordenado = top10_capado.head(5).sort_values("custo_por_conversa", ascending=True, na_position="last")
        resto = top10_capado.iloc[5:10]
        top10_final = pd.concat([top5_reordenado, resto], ignore_index=True)
        LOGGER.info("geoteste.csv preenchido — Top 5 reordenado por custo por conversa (único dado medido, não estimado)")
    else:
        top10_final = top10_capado

    ranking.to_parquet(ranking_path, index=False)
    LOGGER.info("gravado (enriquecido): %s", ranking_path)

    top10_path = OUTPUT_DIR / "top10.csv"
    top10_final.to_csv(top10_path, index=False, sep=";", encoding="utf-8-sig")
    LOGGER.info("gravado (enriquecido): %s", top10_path)

    if pendentes:
        limitacoes_path = DATA_PROCESSED / "limitacoes_fase8.txt"
        linhas = [f"- data/manual/{n}: pendente de preenchimento manual (indicadores derivados ficam \"não coletado\" no relatório)" for n in pendentes]
        limitacoes_path.write_text("\n".join(linhas), encoding="utf-8")
        LOGGER.info("gravado: %s (%d entrada(s) manual(is) pendente(s))", limitacoes_path, len(pendentes))
    LOGGER.info("resumo: %d preenchido(s) %s, %d pendente(s) %s", len(preenchidos), preenchidos, len(pendentes), pendentes)

    log_resumo_fase(LOGGER, entrada=n_entrada, saida=len(ranking), descartados=0)
    return top10_path, ranking_path


if __name__ == "__main__":
    run()
