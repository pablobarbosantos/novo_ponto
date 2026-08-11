"""
Teste G3 (CORRECOES.md) — "escrever um teste unitário que fixe todas as variáveis e
varie apenas o número de concorrentes. O score final tem que cair monotonicamente. Se
não cair, corrigir a composição dos eixos."

`forca_concorrencia` é o insumo real da Fase 7 (soma de avaliações/distância dos
concorrentes na isócrona de 15min) — este teste varia esse valor (proxy direto de
"número/força de concorrentes"), recalcula saturacao=potencial/força e o percentil
correspondente exatamente como pipeline/f7_score.py faz, e checa que score_final
(via pipeline.f7_score._compor_score_final, a mesma função pura usada em produção)
nunca sobe quando a concorrência aumenta.

Roda com: python -m unittest tests/test_g3_monotonicidade.py
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
from f7_score import _compor_score_final  # noqa: E402

DATA_PROCESSED = Path(__file__).resolve().parent.parent / "data" / "processed"


def _scores_variando_forca(pesos: dict, forcas: list[float], potencial_mensal: float = 100_000.0) -> list[float]:
    """Tudo fixo (potencial_mensal, acesso, oferta) exceto forca_concorrencia — igual ao
    que o CORRECOES.md pede. saturacao e seu percentil recalculados com a mesma fórmula
    de f7_score.py (saturacao = potencial_mensal / max(forca, 0.01); rank(pct=True))."""
    saturacoes = pd.Series([potencial_mensal / max(f, 0.01) for f in forcas])
    pct_saturacao = saturacoes.rank(pct=True)

    scores = []
    for pct_sat in pct_saturacao:
        percentis = {
            "demanda_estimada": 0.5,  # fixo — só a força de concorrência varia neste teste
            "saturacao": float(pct_sat),
            "acesso": 0.6,
            "oferta_imovel": 0.0,
        }
        scores.append(_compor_score_final(percentis, pesos))
    return scores


class TestMonotonicidadeConcorrencia(unittest.TestCase):
    FORCAS_CRESCENTES = [0.5, 1.0, 10.0, 50.0, 100.0, 300.0, 1000.0, 5000.0]

    def test_score_cai_com_pesos_sinteticos(self):
        """Pesos de eixo positivos genéricos — garantia estrutural independente de
        qualquer rodada específica da Fase 6."""
        pesos = {"demanda_estimada": 0.35, "saturacao": 0.30, "oferta_imovel": 0.20, "acesso": 0.15}
        scores = _scores_variando_forca(pesos, self.FORCAS_CRESCENTES)
        for i, (a, b) in enumerate(zip(scores, scores[1:])):
            self.assertGreaterEqual(
                a, b,
                f"score final subiu ao aumentar forca_concorrencia (índice {i}->{i+1}, "
                f"forcas={self.FORCAS_CRESCENTES[i]}->{self.FORCAS_CRESCENTES[i+1]}, "
                f"scores={a:.6f}->{b:.6f})",
            )

    def test_score_cai_com_pesos_reais_calibrados(self):
        """Regressão contra o pesos.json real gravado pela Fase 6 (se existir) — não só
        um cenário sintético, mas o peso efetivamente em uso no pipeline agora."""
        pesos_path = DATA_PROCESSED / "pesos.json"
        if not pesos_path.exists():
            self.skipTest("data/processed/pesos.json não existe ainda — rode f6_calibracao.py primeiro")
        pesos = json.loads(pesos_path.read_text(encoding="utf-8"))["pesos_eixos_score_final"]
        scores = _scores_variando_forca(pesos, self.FORCAS_CRESCENTES)
        for i, (a, b) in enumerate(zip(scores, scores[1:])):
            self.assertGreaterEqual(
                a, b,
                f"[pesos reais {pesos}] score final subiu ao aumentar forca_concorrencia "
                f"(índice {i}->{i+1}, scores={a:.6f}->{b:.6f})",
            )


if __name__ == "__main__":
    unittest.main()
