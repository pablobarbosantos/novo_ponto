"""
Compara dois output/top10.csv (candidato_id/bairro que entraram/saíram) —
ferramenta de validação do processo de correção (CORRECOES.md / CORRECOES_2.md),
não faz parte das 9 fases do pipeline.

Uso: python scripts/comparar_top10.py <baseline.csv> <novo.csv>
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


def comparar(baseline_path: Path, novo_path: Path) -> None:
    b = pd.read_csv(baseline_path, sep=";", encoding="utf-8-sig")
    n = pd.read_csv(novo_path, sep=";", encoding="utf-8-sig")

    ids_b, ids_n = set(b["candidato_id"]), set(n["candidato_id"])
    bairros_b, bairros_n = set(b["bairro"]), set(n["bairro"])

    print(f"baseline: {baseline_path} ({len(b)} linhas, {len(bairros_b)} bairros distintos)")
    print(f"novo:     {novo_path} ({len(n)} linhas, {len(bairros_n)} bairros distintos)")
    print()
    print(f"candidatos iguais: {len(ids_b & ids_n)}/{max(len(ids_b), len(ids_n))}")
    print(f"candidatos que saíram: {sorted(ids_b - ids_n) or '(nenhum)'}")
    print(f"candidatos que entraram: {sorted(ids_n - ids_b) or '(nenhum)'}")
    print()
    print(f"bairros que saíram: {sorted(bairros_b - bairros_n) or '(nenhum)'}")
    print(f"bairros que entraram: {sorted(bairros_n - bairros_b) or '(nenhum)'}")
    print()
    print("--- baseline (ordem original) ---")
    for i, row in enumerate(b.itertuples(), start=1):
        print(f"  {i:2d}. {row.candidato_id}  {row.bairro}  score={getattr(row, 'score_final', float('nan')):.4f}")
    print("--- novo (ordem atual) ---")
    for i, row in enumerate(n.itertuples(), start=1):
        print(f"  {i:2d}. {row.candidato_id}  {row.bairro}  score={getattr(row, 'score_final', float('nan')):.4f}")

    if ids_b == ids_n:
        print("\nATENÇÃO: os Top10 são IDÊNTICOS (mesmos candidatos) — o próprio CORRECOES.md espera "
              "que o resultado mude; se não mudou, alguma correção pode não ter sido aplicada de verdade.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    comparar(Path(sys.argv[1]), Path(sys.argv[2]))
