PYTHON := ./.venv/Scripts/python.exe

.PHONY: all clean-processed \
	fase1 fase2 fase3 fase4 fase5 fase6 fase7 fase8 fase9

all: fase1 fase2 fase3 fase4 fase5 fase6 fase7 fase8 fase9

fase1:
	$(PYTHON) pipeline/f1_ibge.py

fase2:
	$(PYTHON) pipeline/f2_concorrentes.py

fase3:
	$(PYTHON) pipeline/f3_vias_zoneamento.py

fase4:
	$(PYTHON) pipeline/f4_candidatos.py

fase5:
	$(PYTHON) pipeline/f5_isocronas.py

fase6:
	$(PYTHON) pipeline/f6_calibracao.py

fase7:
	$(PYTHON) pipeline/f7_score.py

fase8:
	$(PYTHON) pipeline/f8_manuais.py

fase9:
	$(PYTHON) pipeline/f9_relatorio.py

# Limpa derivados (data/processed, output) sem apagar data/raw (cache de downloads) —
# spec ESPEC_CLAUDE_CODE.md §9: "limpa derivados sem apagar data/raw".
clean-processed:
	rm -rf data/processed/*
	rm -rf output/relatorio.html output/mapa.html output/top10.csv output/fichas/*
