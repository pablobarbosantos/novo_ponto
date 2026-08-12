"""
Fase 2b — Vitalidade comercial (C1, CORRECOES_2.md).

O modelo original mede quem MORA no bairro (IBGE) e quem CONCORRE no bairro
(Places/OSM), mas não se o comércio ali está vivo. Bairro pode ter domicílio e
renda razoáveis e ainda ser cemitério de loja. Três indicadores:

C1.1 — Taxa de mortalidade empresarial (CNPJs da Receita Federal, comércio
       varejista em geral, divisão 47 do CNAE — não só pet).
C1.2 — Taxa de estabelecimentos permanentemente fechados no Google Places
       (business_status == CLOSED_PERMANENTLY), calculada em `_taxa_fechamento_maps`
       aqui, mas o dado bruto (business_status) é lido de concorrentes.gpkg,
       que a Fase 2 passou a preservar (C1.2).
C1.3 — Recência das avaliações (ver f2_concorrentes.py, GOOGLE_FIELD_MASK com
       places.reviews e cache data/raw/google_places_v2/).

DESVIO DOCUMENTADO — fonte dos dados de CNPJ: a spec (CLAUDE.md/PLANO) aponta
`https://dadosabertos.rfb.gov.br` como fonte oficial. Esse host está
inacessível desta rede (ConnectTimeout confirmado em HTTP e HTTPS, repetido,
enquanto outros hosts — IBGE, Google — respondem normalmente; não é falha
transitória). Mesmo padrão de degradação já usado no projeto para a URL do
mapa de zoneamento (f3_vias_zoneamento.py): tentar uma fonte espelho
conhecida-atual. Usado o mirror comunitário `casadosdados.com.br`, que serve
os mesmos arquivos mensais da Receita Federal (mesmo layout de colunas,
mesmo índice estilo Apache que o IBGE já usa) — franqueado publicamente,
citado pelo próprio Portal de Dados Abertos como uma cópia dos arquivos
oficiais. Se um dia o host oficial voltar a responder, só RFB_MIRROR_BASE
precisa mudar.

Saída: data/processed/vitalidade_bairro.csv
Roda sozinho: `python pipeline/f2b_vitalidade.py`
"""

from __future__ import annotations

import re
import sys
import unicodedata
import zipfile
from datetime import date, datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_INTERIM, DATA_PROCESSED, DATA_RAW,
    atribuir_bairro, download_if_needed, get_logger, load_config, log_resumo_fase,
)

LOGGER = get_logger("fase2b_vitalidade")

# Host oficial (dadosabertos.rfb.gov.br) inacessível desta rede — ver docstring acima.
RFB_MIRROR_BASE = "https://dados-abertos-rf-cnpj.casadosdados.com.br/arquivos/"
RFB_RAW_DIR = DATA_RAW / "rfb"
MUNICIPIO_UBERLANDIA_RFB = None  # descoberto em runtime via Municipios.zip, não hardcodado

COLS_ESTABELECIMENTOS = [
    "cnpj_basico", "cnpj_ordem", "cnpj_dv", "identificador_matriz_filial", "nome_fantasia",
    "situacao_cadastral", "data_situacao_cadastral", "motivo_situacao_cadastral",
    "nome_cidade_exterior", "pais", "data_inicio_atividade", "cnae_fiscal_principal",
    "cnae_fiscal_secundaria", "tipo_logradouro", "logradouro", "numero", "complemento",
    "bairro", "cep", "uf", "municipio", "ddd1", "telefone1", "ddd2", "telefone2",
    "ddd_fax", "fax", "correio_eletronico", "situacao_especial", "data_situacao_especial",
]
USECOLS_ESTABELECIMENTOS = [
    "situacao_cadastral", "data_situacao_cadastral", "data_inicio_atividade",
    "cnae_fiscal_principal", "bairro", "uf", "municipio",
]

SITUACAO_ATIVA = "02"
SITUACAO_BAIXADA = "08"
CNAE_DIVISAO_COMERCIO = "47"


def _normalizar_bairro(nome: str) -> str:
    """Maiúsculo, sem acento, espaços colapsados — chave de match entre o texto livre
    do endereço da Receita e NM_BAIRRO do IBGE (nenhuma das duas fontes garante grafia
    idêntica; sem biblioteca de fuzzy match no projeto, normalização + match exato)."""
    if not isinstance(nome, str) or not nome.strip():
        return ""
    s = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"\s+", " ", s).strip().upper()
    return s


def _descobrir_mes_mais_recente(logger) -> str:
    resp = requests.get(RFB_MIRROR_BASE, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    nomes = re.findall(r'href="(\d{4}-\d{2}-\d{2})/"', resp.text)
    if not nomes:
        raise RuntimeError(f"não encontrei nenhuma pasta de mês em {RFB_MIRROR_BASE}")
    mais_recente = sorted(set(nomes))[-1]
    logger.info("RFB (mirror) — mês mais recente disponível: %s", mais_recente)
    return mais_recente


def _baixar_zip(nome_arquivo: str, mes: str, logger) -> Path:
    url = f"{RFB_MIRROR_BASE}{mes}/{nome_arquivo}"
    dest = RFB_RAW_DIR / mes / nome_arquivo
    path, _ = download_if_needed(url, dest, logger, min_size_bytes=512, timeout=300)
    return path


def _carregar_municipio_uberlandia(mes: str, logger) -> str:
    path = _baixar_zip("Municipios.zip", mes, logger)
    with zipfile.ZipFile(path) as zf:
        data = zf.read(zf.namelist()[0]).decode("latin-1")
    for linha in data.splitlines():
        partes = [p.strip('"') for p in linha.split(";")]
        if len(partes) == 2 and _normalizar_bairro(partes[1]) == "UBERLANDIA":
            logger.info("RFB — código de município para Uberlândia (código interno da Receita, != IBGE): %s", partes[0])
            return partes[0]
    raise RuntimeError("UBERLANDIA não encontrada em Municipios.zip — layout do arquivo pode ter mudado")


def _filtrar_estabelecimentos_uberlandia(mes: str, cod_municipio_rfb: str, logger) -> pd.DataFrame:
    cache_path = DATA_INTERIM / f"rfb_estabelecimentos_uberlandia_{mes}.parquet"
    if cache_path.exists():
        logger.info("cache hit — usando filtro já feito: %s", cache_path)
        return pd.read_parquet(cache_path)

    frames = []
    total_linhas = 0
    for i in range(10):
        nome = f"Estabelecimentos{i}.zip"
        path = _baixar_zip(nome, mes, logger)
        it = pd.read_csv(
            path, sep=";", header=None, names=COLS_ESTABELECIMENTOS, usecols=USECOLS_ESTABELECIMENTOS,
            encoding="latin-1", dtype=str, quotechar='"', chunksize=500_000,
        )
        n_arquivo = 0
        n_match = 0
        for chunk in it:
            n_arquivo += len(chunk)
            sub = chunk[chunk["municipio"] == cod_municipio_rfb]
            if len(sub):
                frames.append(sub)
                n_match += len(sub)
        total_linhas += n_arquivo
        logger.info("%s: %d linhas, %d de Uberlândia", nome, n_arquivo, n_match)

    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=USECOLS_ESTABELECIMENTOS)
    DATA_INTERIM.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path, index=False)
    logger.info(
        "RFB — %d estabelecimentos de Uberlândia (todas as CNAEs) de %d linhas totais nos 10 arquivos; gravado: %s",
        len(df), total_linhas, cache_path,
    )
    return df


def _calcular_mortalidade_por_bairro(df: pd.DataFrame, setores: gpd.GeoDataFrame, cfg: dict, logger) -> pd.DataFrame:
    cnae_divisao = str(cfg.get("vitalidade_comercial", {}).get("cnae_divisao", CNAE_DIVISAO_COMERCIO))
    janela_meses = int(cfg.get("vitalidade_comercial", {}).get("janela_mortalidade_meses", 36))

    comercio = df[df["cnae_fiscal_principal"].fillna("").str.startswith(cnae_divisao)].copy()
    logger.info(
        "RFB — %d de %d estabelecimentos de Uberlândia são de comércio varejista (CNAE divisão %s)",
        len(comercio), len(df), cnae_divisao,
    )

    comercio["bairro_norm"] = comercio["bairro"].map(_normalizar_bairro)
    sem_bairro = (comercio["bairro_norm"] == "").sum()
    if sem_bairro:
        logger.warning("RFB — %d estabelecimento(s) de comércio sem bairro preenchido no endereço, excluído(s) da agregação", sem_bairro)
    comercio = comercio[comercio["bairro_norm"] != ""]

    bairros_setores = {_normalizar_bairro(b): b for b in setores["NM_BAIRRO"].dropna().unique()}
    comercio["bairro_ibge"] = comercio["bairro_norm"].map(bairros_setores)
    n_casados = comercio["bairro_ibge"].notna().sum()
    n_nao_casados = len(comercio) - n_casados
    logger.info(
        "RFB — bairro (texto livre do endereço) casado contra NM_BAIRRO do IBGE (normalização, sem fuzzy match): "
        "%d casados, %d não casados de %d estabelecimentos de comércio",
        n_casados, n_nao_casados, len(comercio),
    )
    comercio = comercio.dropna(subset=["bairro_ibge"])

    data_sit = pd.to_datetime(comercio["data_situacao_cadastral"], format="%Y%m%d", errors="coerce")
    data_inicio = pd.to_datetime(comercio["data_inicio_atividade"], format="%Y%m%d", errors="coerce")
    hoje = pd.Timestamp(date.today())
    corte = hoje - pd.DateOffset(months=janela_meses)

    ativas = comercio["situacao_cadastral"] == SITUACAO_ATIVA
    baixadas_recentes = (comercio["situacao_cadastral"] == SITUACAO_BAIXADA) & (data_sit >= corte)

    linhas = []
    for bairro, grupo in comercio.groupby("bairro_ibge"):
        idx = grupo.index
        n_ativas = int(ativas.loc[idx].sum())
        n_baixadas = int(baixadas_recentes.loc[idx].sum())
        denom = n_ativas + n_baixadas
        taxa = n_baixadas / denom if denom > 0 else None
        idade_media = None
        idx_ativas = grupo.index[ativas.loc[idx]]
        if len(idx_ativas):
            idades_dias = (hoje - data_inicio.loc[idx_ativas]).dt.days
            idades_validas = idades_dias.dropna()
            if len(idades_validas):
                idade_media = float((idades_validas / 365.25).mean())
        linhas.append({
            "bairro": bairro, "n_estabelecimentos_comercio": len(grupo),
            "n_ativas": n_ativas, "n_baixadas_36m": n_baixadas,
            "taxa_mortalidade": taxa, "idade_media_ativas_anos": idade_media,
        })

    resultado = pd.DataFrame(linhas).sort_values("taxa_mortalidade", ascending=False, na_position="last")
    return resultado


def _taxa_fechamento_maps(setores: gpd.GeoDataFrame, logger) -> pd.DataFrame:
    """C1.2 — taxa de CLOSED_PERMANENTLY no Google Places por bairro. business_status só
    existe pra concorrentes fonte=google (a Fase 2 passou a manter essa coluna — antes era
    descartada ao gravar concorrentes.gpkg)."""
    concorrentes_path = DATA_PROCESSED / "concorrentes.gpkg"
    if not concorrentes_path.exists():
        logger.warning("C1.2 — data/processed/concorrentes.gpkg não existe ainda — pulado")
        return pd.DataFrame(columns=["bairro", "taxa_fechamento_maps", "n_operacional", "n_fechado_permanente"])
    concorrentes = gpd.read_file(concorrentes_path)
    if "business_status" not in concorrentes.columns:
        logger.warning("C1.2 — concorrentes.gpkg sem coluna business_status (rode f2_concorrentes.py de novo) — pulado")
        return pd.DataFrame(columns=["bairro", "taxa_fechamento_maps", "n_operacional", "n_fechado_permanente"])

    conc_google = concorrentes[concorrentes["fonte"] == "google"].copy()
    conc_google["bairro"] = atribuir_bairro(conc_google, setores)
    contagem = conc_google.groupby(["bairro", "business_status"]).size().unstack(fill_value=0)
    fechados = contagem.get("CLOSED_PERMANENTLY", pd.Series(0, index=contagem.index))
    operacionais = contagem.get("OPERATIONAL", pd.Series(0, index=contagem.index))
    denom = fechados + operacionais
    taxa = (fechados / denom.replace(0, pd.NA)).astype(float)
    resultado = pd.DataFrame({
        "bairro": contagem.index, "taxa_fechamento_maps": taxa.values,
        "n_operacional": operacionais.values, "n_fechado_permanente": fechados.values,
    })
    logger.info("C1.2 — taxa de fechamento (Google Places) calculada para %d bairros (%d concorrentes fonte=google, %d fechados permanentemente)",
                len(resultado), len(conc_google), int(fechados.sum()))
    return resultado


def _recencia_avaliacoes_por_bairro(setores: gpd.GeoDataFrame, logger) -> pd.DataFrame:
    """C1.3 — mediana da data de avaliação mais recente por bairro, a partir de
    data_ultima_avaliacao (f2_concorrentes.py, Place Details/places.reviews). Só entra se
    a Fase 2 já tiver rodado com C1.3 aplicado."""
    concorrentes_path = DATA_PROCESSED / "concorrentes.gpkg"
    if not concorrentes_path.exists():
        return pd.DataFrame(columns=["bairro", "meses_desde_ultima_avaliacao_mediana", "comercio_local_baixo_movimento"])
    concorrentes = gpd.read_file(concorrentes_path)
    if "data_ultima_avaliacao" not in concorrentes.columns or concorrentes["data_ultima_avaliacao"].isna().all():
        logger.warning(
            "C1.3 — concorrentes.gpkg sem nenhum data_ultima_avaliacao preenchido — confirmado em runtime "
            "(ver f2_concorrentes.py) que a chave/projeto não tem acesso ao campo places.reviews do Place "
            "Details (retorna 200 OK mas só {\"id\":...}, sem \"reviews\", em 113/113 respostas testadas) — "
            "limitação de API, não falta de configuração. C1.3 pulado, fica 'não coletado'."
        )
        return pd.DataFrame(columns=["bairro", "meses_desde_ultima_avaliacao_mediana", "comercio_local_baixo_movimento"])

    conc = concorrentes.dropna(subset=["data_ultima_avaliacao"]).copy()
    conc["bairro"] = atribuir_bairro(conc, setores)
    conc["data_ultima_avaliacao_dt"] = pd.to_datetime(conc["data_ultima_avaliacao"], errors="coerce", utc=True)
    hoje = pd.Timestamp(date.today(), tz="UTC")
    conc["meses_desde"] = (hoje - conc["data_ultima_avaliacao_dt"]).dt.days / 30.44

    agrupado = conc.groupby("bairro")["meses_desde"].median().rename("meses_desde_ultima_avaliacao_mediana").reset_index()
    agrupado["comercio_local_baixo_movimento"] = agrupado["meses_desde_ultima_avaliacao_mediana"] > 6
    logger.info("C1.3 — recência de avaliações agregada para %d bairros (%d concorrentes com data)", len(agrupado), len(conc))
    return agrupado


def run() -> Path:
    cfg = load_config()
    LOGGER.info("=== Fase 2b: vitalidade comercial (C1.1 — mortalidade empresarial via RFB) ===")

    RFB_RAW_DIR.mkdir(parents=True, exist_ok=True)
    mes = _descobrir_mes_mais_recente(LOGGER)
    cod_municipio = _carregar_municipio_uberlandia(mes, LOGGER)

    df_uberlandia = _filtrar_estabelecimentos_uberlandia(mes, cod_municipio, LOGGER)
    setores = gpd.read_file(DATA_PROCESSED / "setores.gpkg")

    vitalidade = _calcular_mortalidade_por_bairro(df_uberlandia, setores, cfg, LOGGER)

    # C1.2 e C1.3 — mescla por bairro (mesma fonte de nome — NM_BAIRRO do IBGE via
    # atribuir_bairro — então não precisa de normalização adicional pra casar com C1.1)
    fechamento = _taxa_fechamento_maps(setores, LOGGER)
    recencia = _recencia_avaliacoes_por_bairro(setores, LOGGER)
    vitalidade = vitalidade.merge(fechamento, on="bairro", how="outer").merge(recencia, on="bairro", how="outer")

    # Validação do doc (CORRECOES_2.md C1.1): Centro e Presidente Roosevelt precisam
    # aparecer entre os piores (quartil superior de mortalidade) — se não aparecerem,
    # o indicador não está capturando o fenômeno. Comparação por bairro normalizado (a
    # coluna "bairro" mantém a grafia original do IBGE, ex. "Presidente Roosevelt").
    if vitalidade["taxa_mortalidade"].notna().any():
        limite_superior = vitalidade["taxa_mortalidade"].quantile(0.75)
        bairros_norm = {_normalizar_bairro(b): b for b in vitalidade["bairro"].dropna()}
        piores_norm = {_normalizar_bairro(b) for b in vitalidade[vitalidade["taxa_mortalidade"] >= limite_superior]["bairro"]}
        for alvo in ("CENTRO", "PRESIDENTE ROOSEVELT"):
            if alvo in piores_norm:
                LOGGER.info("C1.1 — validação OK: '%s' está no quartil superior de mortalidade, como esperado", alvo)
            elif alvo in bairros_norm:
                LOGGER.warning(
                    "C1.1 — validação FALHOU: '%s' tem dado de mortalidade mas NÃO está no quartil superior — "
                    "investigar antes de confiar no indicador (CORRECOES_2.md)", alvo,
                )
            else:
                LOGGER.warning("C1.1 — '%s' não tem estabelecimentos de comércio casados nesta agregação — sem dado", alvo)

    out_path = DATA_PROCESSED / "vitalidade_bairro.csv"
    vitalidade.to_csv(out_path, index=False, encoding="utf-8-sig")
    LOGGER.info("gravado: %s (%d bairros)", out_path, len(vitalidade))

    log_resumo_fase(
        LOGGER, entrada=len(df_uberlandia), saida=len(vitalidade),
        descartados=len(df_uberlandia) - int(vitalidade["n_estabelecimentos_comercio"].sum()) if len(vitalidade) else len(df_uberlandia),
        motivo_descarte="fora da divisão 47 (comércio varejista) ou sem bairro casado contra o IBGE",
    )
    return out_path


if __name__ == "__main__":
    run()
