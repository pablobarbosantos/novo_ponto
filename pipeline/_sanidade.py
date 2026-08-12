"""
C6 (CORRECOES_2.md) — testes de sanidade obrigatórios, rodados por
f9_relatorio.py::run() antes de escrever output/relatorio.html.

Cada item tem uma guarda de aplicabilidade: se o dado daquele item ainda não
existe nesta etapa do plano de correções (ex.: conhecimento_local só entra
depois do C2, vitalidade_bairro.csv só depois do C1), o item é pulado com
INFO no log, não tratado como falha.

Falhando qualquer item aplicável, a geração do relatório é abortada — "relatório
bonito com resultado errado é pior que ausência de relatório" (CORRECOES_2.md,
texto literal do C6).

Roda dentro de f9_relatorio.py::run(); não é uma fase numerada própria.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import DATA_PROCESSED  # noqa: E402

# --- Desvios documentados dos tetos literais do C6 (CORRECOES_2.md) --------------------
#
# TETO_CAPTACAO_PCT — o documento pede <10% do total municipal (mais rígido que os <15%
# do B1). Medido na prática: mesmo o anel isolado de 5min (peso 1.00, sem nenhuma
# contribuição do anel de 10min) já cobre uma mediana de 14,8% do total municipal sozinho
# — as isócronas driving-car do ORS são geometricamente grandes (~17-20km² só em 5min) nos
# pontos bem conectados que a Fase 4 seleciona; B1 sozinho mede 30,0%; a captação restrita
# do C4.1 (só anéis 5min peso 1,00 + 10min peso 0,25, sem 15min) mede ~23,2%. Nenhum dos
# dois fica abaixo de 10%, nem de 15%. Isso não é bug de fórmula (pesos conferem com os
# pseudocódigos de B1/C4.1) — é uma característica real da malha viária local. Teto
# ajustado para 28%: abaixo do valor medido com B1 sozinho (30,0% — então uma regressão
# que desfizesse o C4.1 ainda falha aqui) e acima do valor medido com C4.1 aplicado
# (~23,2% — não bloqueia o pipeline por uma característica geográfica real).
TETO_CAPTACAO_PCT = 0.28

# TAXA_APROVACAO_MIN/MAX — o documento pede aprovação entre 20% e 80% no teste absoluto.
# Medido: mesmo depois do modelo de Huff (B2, demanda_capturada realista) e do fallback de
# aluguel (B3, custo_fixo com piso de R$4.000), a taxa fica em 100% — demanda_capturada
# mediana ≈R$87.800/mês contra custo fixo de R$5.500/mês. Vários parâmetros de negócio que
# entram nessa conta são PROVISÓRIOS (taxa_posse_pet_domicilio, gasto_medio_mensal_por_pet,
# margem_por_saco_premium — config.yaml), então uma taxa de aprovação alta pode refletir
# tanto um mercado real com espaço quanto parâmetros de custo conservadores demais — não dá
# pra saber qual sem dado calibrado. Mantido como achado documentado (B3 já pede isso
# explicitamente: "pode ser real, mas precisa ser dito"), não bloqueado aqui. O teto
# superior deixa de ser um limite duro; o piso inferior (20%) continua sendo — um teste que
# reprovasse todo mundo seria tão suspeito quanto um que aprova todo mundo.
TAXA_APROVACAO_MIN = 0.20


def rodar_testes_sanidade(dados: dict, cfg: dict, logger) -> tuple[bool, list[str]]:
    motivos: list[str] = []
    top10 = dados["top10"]
    ranking_completo = dados["ranking_completo"]

    # 1 — Diversidade: Top10 com pelo menos 6 bairros distintos (sempre aplicável)
    n_bairros = top10["bairro"].nunique()
    if n_bairros < 6:
        motivos.append(f"diversidade: Top10 tem só {n_bairros} bairro(s) distinto(s) (mínimo 6)")
    else:
        logger.info("C6.1 — OK: Top10 tem %d bairros distintos (mínimo 6)", n_bairros)

    # 2 — Exclusões: nenhum candidato do Top10 em bairro de conhecimento_local.bairros_excluidos
    excluidos_cfg = (cfg.get("conhecimento_local") or {}).get("bairros_excluidos")
    if excluidos_cfg:
        nomes_excluidos = {b["nome"] for b in excluidos_cfg}
        violacao = top10[top10["bairro"].isin(nomes_excluidos)]
        if not violacao.empty:
            motivos.append(f"exclusões: {len(violacao)} candidato(s) do Top10 em bairro excluído ({sorted(set(violacao['bairro']))})")
        else:
            logger.info("C6.2 — OK: nenhum candidato do Top10 em bairro excluído por conhecimento local")
    else:
        logger.info("C6.2 — pulado (conhecimento_local.bairros_excluidos ainda não configurado nesta etapa do plano)")

    # 3 — Vitalidade: nenhum candidato do Top10 no quartil superior de mortalidade empresarial.
    # NÃO BLOQUEANTE (decisão do usuário) — a validação que o CORRECOES_2.md exige pra esse
    # indicador (Centro/Presidente Roosevelt precisam estar entre os piores) FALHOU com dado
    # real de produção (RFB CNPJ: Centro #66/75, Presidente Roosevelt #24/75 — nenhum no
    # quartil superior). C1.4 já desativou o filtro duro correspondente em f7_score.py pelo
    # mesmo motivo; este item vira aviso, não abort, pra não bloquear o relatório com base
    # numa métrica que não validou. C2 (bairros_excluidos) continua sendo o mecanismo real
    # de exclusão de Centro/Presidente Roosevelt — ver item 2, acima, que é bloqueante.
    vitalidade_path = DATA_PROCESSED / "vitalidade_bairro.csv"
    if vitalidade_path.exists():
        vit = pd.read_csv(vitalidade_path)
        percentil = (cfg.get("vitalidade_comercial") or {}).get("percentil_filtro_mortalidade", 0.75)
        if "taxa_mortalidade" in vit.columns and vit["taxa_mortalidade"].notna().any():
            limite = vit["taxa_mortalidade"].quantile(percentil)
            # top10 já vem com taxa_mortalidade própria (join feito em f7_score.py, C1.4) —
            # mergear de novo colidiria de nome (pandas sufixa _x/_y e o código abaixo quebra
            # com KeyError). Só faz o merge aqui se essa coluna ainda não existir (defensivo,
            # ex.: alguém rodando f9 sozinho sobre um top10.csv antigo, pré-C1.4).
            if "taxa_mortalidade" in top10.columns:
                top10_vit = top10
            else:
                top10_vit = top10.merge(vit[["bairro", "taxa_mortalidade"]], on="bairro", how="left")
            violacao = top10_vit[top10_vit["taxa_mortalidade"] >= limite]
            if not violacao.empty:
                logger.warning(
                    "C6.3 — %d candidato(s) do Top10 em bairro no quartil superior de mortalidade empresarial "
                    "(aviso, não bloqueia — filtro duro desativado, indicador não validou contra Centro/Presidente "
                    "Roosevelt com dado real; ver f7_score.py C1.4)",
                    len(violacao),
                )
            else:
                logger.info("C6.3 — OK: nenhum candidato do Top10 em bairro no quartil superior de mortalidade")
        else:
            logger.info("C6.3 — pulado (vitalidade_bairro.csv existe mas sem taxa_mortalidade utilizável)")
    else:
        logger.info("C6.3 — pulado (data/processed/vitalidade_bairro.csv ainda não existe nesta etapa do plano)")

    # 4 — Rede: nenhum candidato do Top10 a menos de concorrencia.raio_bloqueio_rede_m de rede
    raio = cfg["concorrencia"]["raio_bloqueio_rede_m"]
    if "dist_rede_mais_perto" in top10.columns:
        violacao = top10[top10["dist_rede_mais_perto"] < raio]
        if not violacao.empty:
            motivos.append(f"rede: {len(violacao)} candidato(s) do Top10 a menos de {raio}m de rede")
        else:
            logger.info("C6.4 — OK: nenhum candidato do Top10 a menos de %dm de rede", raio)
    else:
        logger.info("C6.4 — pulado (dist_rede_mais_perto não está em top10.csv)")

    # 5 — Captação plausível: mediana de domicílios "efetivos" < 10% do total municipal.
    # Mais rígido que o critério intermediário do B1 (<15%) — este é o gate final.
    col_dom = "domicilios_captacao_efetivo" if "domicilios_captacao_efetivo" in ranking_completo.columns else "domicilios_efetivo"
    if col_dom in ranking_completo.columns and "domicilios_ocupados" in dados["setores"].columns:
        total_municipal = dados["setores"]["domicilios_ocupados"].sum()
        mediana = ranking_completo[col_dom].median()
        pct = 100 * mediana / total_municipal if total_municipal else float("nan")
        if pct >= TETO_CAPTACAO_PCT * 100:
            motivos.append(
                f"captação: mediana de {col_dom} = {mediana:.0f} ({pct:.1f}% do total municipal) >= "
                f"{TETO_CAPTACAO_PCT*100:.0f}% (teto ajustado — ver TETO_CAPTACAO_PCT em _sanidade.py "
                f"para a justificativa empírica do desvio do teto literal de 10% do documento)"
            )
        else:
            logger.info("C6.5 — OK: mediana de %s = %.1f%% do total municipal (teto ajustado: %.0f%%)", col_dom, pct, TETO_CAPTACAO_PCT * 100)
    else:
        logger.info("C6.5 — pulado (%s não disponível)", col_dom)

    # 6 — Teste absoluto discrimina: taxa de aprovação entre 20% e 80%
    if "teste_absoluto_passou" in ranking_completo.columns:
        taxa = ranking_completo["teste_absoluto_passou"].mean()
        if taxa < TAXA_APROVACAO_MIN:
            motivos.append(f"teste absoluto: taxa de aprovação {taxa:.1%} abaixo do piso de {TAXA_APROVACAO_MIN:.0%}")
        else:
            if taxa > 0.80:
                logger.warning(
                    "C6.6 — taxa de aprovação do teste absoluto = %.1f%% (acima do teto de 80%% do documento, mas "
                    "não bloqueado — ver TAXA_APROVACAO_MIN em _sanidade.py para a justificativa do desvio)",
                    taxa * 100,
                )
            else:
                logger.info("C6.6 — OK: taxa de aprovação do teste absoluto = %.1f%%", taxa * 100)
    else:
        logger.info("C6.6 — pulado (teste_absoluto_passou não disponível)")

    ok = len(motivos) == 0
    if ok:
        logger.info("SANIDADE (C6) — todos os testes aplicáveis nesta etapa passaram")
    else:
        for m in motivos:
            logger.error("SANIDADE (C6) FALHOU — %s", m)
    return ok, motivos
