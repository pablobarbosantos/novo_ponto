"""
Fase 1 — Base demográfica (IBGE), spec ESPEC_CLAUDE_CODE.md §6.

Lê a malha de setores censitários de MG (com atributos), as tabelas de
domicílios/demografia/renda do Censo 2022, filtra por Uberlândia
(CD_MUN=3170206), junta tudo por CD_SETOR e grava data/processed/setores.gpkg
já reprojetado para o CRS métrico.

Roda sozinho: `python pipeline/f1_ibge.py`
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_RAW, DATA_INTERIM_MD, DATA_PROCESSED,
    clean_numeric, download_if_needed, find_latest, get_logger,
    list_remote_dir, load_config, log_resumo_fase, read_ibge_csv,
)

LOGGER = get_logger("fase1_ibge")

IBGE_SETORES_BASE = "https://ftp.ibge.gov.br/Censos/Censo_Demografico_2022/Agregados_por_Setores_Censitarios"
IBGE_RENDA_BASE = "https://ftp.ibge.gov.br/Censos/Censo_Demografico_2022/Agregados_por_Setores_Censitarios_Rendimento_do_Responsavel"

RAW_DIR = DATA_RAW / "ibge"
EXTRACTED_DIR = RAW_DIR / "extracted"

# Códigos de variável descobertos via markitdown nos dicionários oficiais
# (data/interim/md/dicionario_agregados_setores.xlsx.md e
# dicionario_renda_responsavel.xlsx.md) — nunca adivinhados. Ver comentário
# ao lado de cada um.
VAR_DOMICILIOS_OCUPADOS = "V00001"   # Domicílios Particulares Permanentes Ocupados (DPPO)
VAR_DOMICILIOS_IMPROV = "V00002"     # Domicílios Particulares Improvisados Ocupados
VAR_MORADORES_DPPO = "V00005"        # DPPO, quantidade de moradores
VAR_CASA = "V00047"                  # DPPO, tipo casa
VAR_CASA_VILA = "V00048"             # DPPO, tipo casa de vila/condomínio
VAR_APARTAMENTO = "V00049"           # DPPO, tipo apartamento
VAR_RENDA_N_RESPONSAVEIS = "V06001"  # pessoas responsáveis com rendimento
VAR_RENDA_MEDIA = "V06004"           # rendimento nominal médio mensal do responsável
VAR_RENDA_MEDIANA = "V06006"         # rendimento nominal mediano mensal do responsável
VAR_POP_TOTAL_DEMOGRAFIA = "V01006"  # total de moradores (tabela demografia)
FAIXAS_ETARIAS = {
    "V01031": "idade_0_4", "V01032": "idade_5_9", "V01033": "idade_10_14",
    "V01034": "idade_15_19", "V01035": "idade_20_24", "V01036": "idade_25_29",
    "V01037": "idade_30_39", "V01038": "idade_40_49", "V01039": "idade_50_59",
    "V01040": "idade_60_69", "V01041": "idade_70_mais",
}


def _discover_urls(cfg: dict) -> dict:
    """Lista os diretórios do FTP e pega os arquivos mais recentes — nunca hardcoda nome com data."""
    uf = cfg["municipio"]["uf"]

    setor_csv_dir = f"{IBGE_SETORES_BASE}/Agregados_por_Setor_csv"
    names_setor = list_remote_dir(setor_csv_dir + "/", LOGGER)
    dicionario_root = list_remote_dir(IBGE_SETORES_BASE + "/", LOGGER)
    malha_dir = f"{IBGE_SETORES_BASE}/malha_com_atributos/setores/gpkg/UF/{uf}"
    names_malha = list_remote_dir(malha_dir + "/", LOGGER)
    renda_dir = IBGE_RENDA_BASE
    names_renda = list_remote_dir(renda_dir + "/", LOGGER)

    basico = find_latest(names_setor, "Agregados_por_setores_basico_BR", ".zip")
    domicilio1 = find_latest(names_setor, "Agregados_por_setores_caracteristicas_domicilio1_BR", ".zip")
    demografia = find_latest(names_setor, "Agregados_por_setores_demografia_BR", ".zip")
    dicionario = find_latest(dicionario_root, "dicionario_de_dados_agregados_por_setores_censitarios", ".xlsx")
    malha = find_latest(names_malha, f"{uf}_setores_CD", ".gpkg")
    renda_zip = find_latest(names_renda, "Agregados_por_setores_renda_responsavel_BR", ".zip")
    renda_dic = find_latest(names_renda, "dicionario_de_dados_renda_responsavel", ".xlsx")

    missing = [
        n for n, v in [
            ("basico", basico), ("domicilio1", domicilio1), ("demografia", demografia),
            ("dicionario", dicionario), ("malha", malha),
            ("renda_zip", renda_zip), ("renda_dicionario", renda_dic),
        ] if v is None
    ]
    if missing:
        raise RuntimeError(f"Não encontrei no FTP do IBGE: {missing}. A estrutura do FTP pode ter mudado.")

    return {
        "basico": f"{setor_csv_dir}/{basico}",
        "domicilio1": f"{setor_csv_dir}/{domicilio1}",
        "demografia": f"{setor_csv_dir}/{demografia}",
        "dicionario": f"{IBGE_SETORES_BASE}/{dicionario}",
        "malha": f"{malha_dir}/{malha}",
        "renda_zip": f"{renda_dir}/{renda_zip}",
        "renda_dicionario": f"{renda_dir}/{renda_dic}",
    }


def _download_all(urls: dict) -> dict:
    paths = {}
    for key, url in urls.items():
        dest = RAW_DIR / Path(url).name
        path, _ = download_if_needed(url, dest, LOGGER, min_size_bytes=1024)
        paths[key] = path
    return paths


def _convert_dicionarios_markitdown(paths: dict) -> None:
    """spec §2 — todo .xlsx não tabular passa por markitdown antes de ser lido."""
    import subprocess

    for key in ("dicionario", "renda_dicionario"):
        src = paths[key]
        dest = DATA_INTERIM_MD / f"{src.name}.md"
        if dest.exists():
            LOGGER.info("markitdown já convertido, pulando: %s", dest)
            continue
        LOGGER.info("convertendo %s com markitdown -> %s", src, dest)
        subprocess.run(
            [sys.executable, "-m", "markitdown", str(src), "-o", str(dest)],
            check=True,
        )


def _extract_zip(zip_path: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    marker = out_dir / ".extracted_ok"
    if marker.exists():
        LOGGER.info("já extraído, pulando: %s", zip_path.name)
    else:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(out_dir)
        marker.write_text("ok", encoding="utf-8")
        LOGGER.info("extraído: %s -> %s", zip_path.name, out_dir)
    csvs = list(out_dir.glob("*.csv"))
    if not csvs:
        raise RuntimeError(f"nenhum CSV encontrado em {out_dir} após extrair {zip_path}")
    return csvs[0]


def _filtrar_uberlandia(df: pd.DataFrame, cd_setor_col: str, cd_mun: str) -> pd.DataFrame:
    """CD_SETOR = UF(2)+MUN(5)+... — filtra por prefixo, sem depender de haver coluna CD_MUN."""
    df = df.copy()
    df[cd_setor_col] = df[cd_setor_col].astype(str).str.strip().str.zfill(15)
    return df[df[cd_setor_col].str.startswith(cd_mun)]


def run() -> Path:
    cfg = load_config()
    cd_mun = cfg["municipio"]["codigo_ibge"]
    crs_geografico = cfg["crs"]["geografico"]
    crs_metrico = cfg["crs"]["metrico"]

    LOGGER.info("=== Fase 1: base demográfica IBGE — município %s ===", cd_mun)

    urls = _discover_urls(cfg)
    for k, u in urls.items():
        LOGGER.info("URL mais recente para %s: %s", k, u)
    paths = _download_all(urls)
    _convert_dicionarios_markitdown(paths)

    # --- malha geográfica -------------------------------------------------
    LOGGER.info("lendo malha geográfica de MG (pode demorar, é um gpkg grande)")
    malha = gpd.read_file(paths["malha"])
    malha["CD_SETOR"] = malha["CD_SETOR"].astype("int64").astype(str).str.zfill(15)
    malha["CD_MUN"] = malha["CD_MUN"].astype("int64").astype(str).str.zfill(7)
    n_malha_total = len(malha)
    malha = malha[malha["CD_MUN"] == cd_mun].copy()
    LOGGER.info("malha: %d setores em MG, %d em Uberlândia (CD_MUN=%s)", n_malha_total, len(malha), cd_mun)
    if malha.crs is None:
        LOGGER.warning("malha sem CRS declarado no gpkg — assumindo %s conforme spec", crs_geografico)
        malha = malha.set_crs(crs_geografico)
    elif str(malha.crs).upper() != crs_geografico.upper():
        LOGGER.warning("CRS da malha é %s, não %s — reprojetando de qualquer forma", malha.crs, crs_geografico)

    # --- tabelas de atributos ---------------------------------------------
    csv_basico = _extract_zip(paths["basico"], EXTRACTED_DIR / "basico")
    csv_dom1 = _extract_zip(paths["domicilio1"], EXTRACTED_DIR / "domicilio1")
    csv_demog = _extract_zip(paths["demografia"], EXTRACTED_DIR / "demografia")
    csv_renda = _extract_zip(paths["renda_zip"], EXTRACTED_DIR / "renda")

    df_basico, dec_basico = read_ibge_csv(csv_basico, LOGGER)
    df_basico = _filtrar_uberlandia(df_basico, "CD_SETOR", cd_mun)

    df_dom1, dec_dom1 = read_ibge_csv(csv_dom1, LOGGER)
    cd_setor_col_dom1 = "CD_setor" if "CD_setor" in df_dom1.columns else "CD_SETOR"
    df_dom1 = _filtrar_uberlandia(df_dom1, cd_setor_col_dom1, cd_mun).rename(columns={cd_setor_col_dom1: "CD_SETOR"})

    df_demog, dec_demog = read_ibge_csv(csv_demog, LOGGER)
    cd_setor_col_demog = "CD_setor" if "CD_setor" in df_demog.columns else "CD_SETOR"
    df_demog = _filtrar_uberlandia(df_demog, cd_setor_col_demog, cd_mun).rename(columns={cd_setor_col_demog: "CD_SETOR"})

    df_renda, dec_renda = read_ibge_csv(csv_renda, LOGGER)
    df_renda = _filtrar_uberlandia(df_renda, "CD_SETOR", cd_mun)

    LOGGER.info(
        "linhas filtradas para Uberlândia: básico=%d domicilio1=%d demografia=%d renda=%d",
        len(df_basico), len(df_dom1), len(df_demog), len(df_renda),
    )

    # --- variáveis derivadas -----------------------------------------------
    dom1 = pd.DataFrame({"CD_SETOR": df_dom1["CD_SETOR"]})
    dom1["domicilios_ocupados"] = clean_numeric(df_dom1[VAR_DOMICILIOS_OCUPADOS], dec_dom1)
    dom1["domicilios_improvisados"] = clean_numeric(df_dom1[VAR_DOMICILIOS_IMPROV], dec_dom1)
    dom1["moradores_dppo"] = clean_numeric(df_dom1[VAR_MORADORES_DPPO], dec_dom1)
    casa = clean_numeric(df_dom1[VAR_CASA], dec_dom1).fillna(0)
    casa_vila = clean_numeric(df_dom1[VAR_CASA_VILA], dec_dom1).fillna(0)
    dom1["domicilios_casa"] = casa + casa_vila
    dom1["domicilios_apartamento"] = clean_numeric(df_dom1[VAR_APARTAMENTO], dec_dom1)
    dom1["pct_apartamento"] = (
        dom1["domicilios_apartamento"] / dom1["domicilios_ocupados"]
    ).clip(lower=0, upper=1)

    demog = pd.DataFrame({"CD_SETOR": df_demog["CD_SETOR"]})
    demog["populacao_demografia"] = clean_numeric(df_demog[VAR_POP_TOTAL_DEMOGRAFIA], dec_demog)
    for var, nome in FAIXAS_ETARIAS.items():
        demog[nome] = clean_numeric(df_demog[var], dec_demog)

    renda = pd.DataFrame({"CD_SETOR": df_renda["CD_SETOR"]})
    renda["n_responsaveis_com_renda"] = clean_numeric(df_renda[VAR_RENDA_N_RESPONSAVEIS], dec_renda)
    renda["renda_media_responsavel"] = clean_numeric(df_renda[VAR_RENDA_MEDIA], dec_renda)
    renda["renda_mediana_responsavel"] = clean_numeric(df_renda[VAR_RENDA_MEDIANA], dec_renda)

    basico = pd.DataFrame({"CD_SETOR": df_basico["CD_SETOR"]})
    basico["populacao_total_setor"] = clean_numeric(df_basico["v0001"], dec_basico)
    # NM_BAIRRO já vem da malha (mesma origem IBGE) — só trazemos o que a malha não tem.
    basico["situacao_setor"] = df_basico.get("SITUACAO")

    # --- junção --------------------------------------------------------
    setores = malha[["CD_SETOR", "NM_BAIRRO", "NM_DIST", "AREA_KM2", "geometry"]].copy()
    n_entrada = len(setores)
    for extra, nome in [(basico, "básico"), (dom1, "domicilio1"), (demog, "demografia"), (renda, "renda")]:
        antes = len(setores)
        chave_checagem = extra.columns[1]  # primeira coluna de dado depois de CD_SETOR
        setores = setores.merge(extra, on="CD_SETOR", how="left")
        nao_casou = setores[chave_checagem].isna().sum()
        LOGGER.info("merge com %s: %d->%d linhas, %d setores sem correspondência nessa tabela", nome, antes, len(setores), nao_casou)

    setores = setores.to_crs(crs_metrico)
    LOGGER.info("reprojetado de %s para %s (spec: sempre métrico antes de cálculo de distância/área)", crs_geografico, crs_metrico)

    # --- critérios de aceite (spec §6 Fase 1) -------------------------
    n_saida = len(setores)
    problemas = []
    if not (1000 <= n_saida <= 3000):
        problemas.append(f"número de setores fora da faixa esperada para Uberlândia: {n_saida}")
    if setores["CD_SETOR"].isna().any():
        problemas.append("existe CD_SETOR nulo")
    pct_fora_faixa = setores["pct_apartamento"].dropna()
    if not pct_fora_faixa.between(0, 1).all():
        problemas.append("existe pct_apartamento fora de [0,1]")
    soma_domicilios = setores["domicilios_ocupados"].sum()
    if not (50_000 <= soma_domicilios <= 500_000):
        problemas.append(f"soma de domicílios fora do plausível para o município: {soma_domicilios}")

    for p in problemas:
        LOGGER.warning("ACEITE — %s", p)
    if not problemas:
        LOGGER.info("ACEITE — todos os critérios da Fase 1 passaram (n_setores=%d, soma_domicilios=%d)", n_saida, soma_domicilios)

    out_path = DATA_PROCESSED / "setores.gpkg"
    setores.to_file(out_path, layer="setores", driver="GPKG")
    LOGGER.info("gravado: %s", out_path)

    log_resumo_fase(
        LOGGER, entrada=n_entrada, saida=n_saida,
        descartados=n_entrada - n_saida,
        motivo_descarte="setores fora de Uberlândia (filtro CD_MUN) já removidos na malha de origem" if n_entrada == n_saida else None,
    )
    return out_path


if __name__ == "__main__":
    run()
