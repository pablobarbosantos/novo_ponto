"""
Fase 3 — Vias e zoneamento (spec ESPEC_CLAUDE_CODE.md §6).

3.1 Malha viária — Overpass, highway in primary|secondary|tertiary|residential.
3.2 Zoneamento — verifica se a Prefeitura publica vetorial (dados abertos /
    Mapas e Bairros); se não (e não publica, ver log), baixa o PDF do mapa de
    zoneamento mais recente, converte com markitdown e extrai a legenda de
    zonas. Não inventa polígono de zona quando só há PDF.
3.3 Perímetro urbano — setores IBGE marcados "Urbana" combinados com a
    densidade da malha viária (spec: "não hardcodar raio, usar os dois").

Saídas: data/processed/vias.gpkg, data/processed/perimetro_urbano.gpkg,
data/processed/zonas_permitidas.csv (legenda; zonas.gpkg só se algum dia
aparecer uma fonte vetorial — hoje não existe, ver log/limitações).

Roda sozinho: `python pipeline/f3_vias_zoneamento.py`
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import LineString

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_INTERIM_MD, DATA_PROCESSED, DATA_RAW, DEFAULT_HEADERS,
    download_if_needed, get_logger, load_config, log_resumo_fase,
)

LOGGER = get_logger("fase3_vias_zoneamento")

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
OSM_RAW_DIR = DATA_RAW / "osm"
ZONEAMENTO_RAW_DIR = DATA_RAW / "zoneamento"

# Páginas onde a Prefeitura poderia publicar vetorial de zoneamento (spec 3.2).
# Checadas em tempo de execução; ambas retornaram 403 (bloqueio de bot no
# servidor da Prefeitura, confirmado por HEAD e por fetch de página) durante
# o desenvolvimento deste pipeline — registrado como limitação, não como bug.
PAGINAS_DADOS_ABERTOS = [
    "https://www.uberlandia.mg.gov.br/portal-da-transparencia/dados-abertos/catalogo-de-dados-abertos/",
    "https://www.uberlandia.mg.gov.br/prefeitura/secretarias/planejamento-urbano/mapas-e-bairros/",
]

# PDF do mapa de zoneamento. O link do ESPEC_CLAUDE_CODE.md (2024-FINAL) está
# morto (redireciona pra home). Uma nova Lei Complementar de zoneamento
# (LC 812/2026, sancionada 09/01/2026) trocou o mapa vigente — a lista abaixo
# tenta a partir do mais recente conhecido (fev/2026) e cai para versões
# anteriores. Não há índice de diretório navegável neste servidor (ao
# contrário do FTP do IBGE), então diferente da Fase 1 não dá pra descobrir
# "o mais recente" por listagem — foi localizado por busca no momento da
# implementação. Se todos falharem, a fase registra a limitação e segue.
CANDIDATOS_PDF_ZONEAMENTO = [
    ("2026-02 (Lei 8122/026 e alterações — vigente)",
     "https://docs.uberlandia.mg.gov.br/wp-content/uploads/2026/02/Mapa-Zoneamento-e-Ocupacao-do-Solo-%E2%80%93-Lei-8122_026-e-suas-alteracoes.pdf"),
    ("2025-07", "https://docs.uberlandia.mg.gov.br/wp-content/uploads/2025/07/Mapa-Zoneamento-e-Ocupacao-do-Solo-2025_-1.pdf"),
    ("2025-04", "https://docs.uberlandia.mg.gov.br/wp-content/uploads/2025/04/Mapa-Zoneamento-e-Ocupacao-do-Solo-2025_FINAl.pdf"),
    ("2024-06 (link do ESPEC_CLAUDE_CODE.md, provavelmente morto)",
     "https://docs.uberlandia.mg.gov.br/wp-content/uploads/2024/06/Mapa-Zoneamento-e-Ocupacao-do-Solo-2024-FINAL.pdf"),
]

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

LIMITACOES: list[str] = []


# ---------------------------------------------------------------------------
# 3.1 — Malha viária
# ---------------------------------------------------------------------------

OVERPASS_QUERY_VIAS = """
[out:json][timeout:120];
area["name"="Uberlândia"]["admin_level"="8"]->.a;
(
  way["highway"~"^(primary|secondary|tertiary|residential)$"](area.a);
);
out body;
>;
out skel qt;
""".strip()


def _overpass_fetch_vias() -> dict:
    import json
    import time

    OSM_RAW_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = OSM_RAW_DIR / "overpass_vias.json"
    if cache_path.exists() and cache_path.stat().st_size > 100:
        LOGGER.info("cache hit Overpass (vias): %s", cache_path)
        return json.loads(cache_path.read_text(encoding="utf-8"))

    last_error = None
    for volta in range(2):  # duas voltas completas pelos endpoints antes de desistir de vez
        for endpoint in OVERPASS_ENDPOINTS:
            for attempt in range(4):
                try:
                    LOGGER.info("Overpass (vias): tentando %s (volta %d, tentativa %d)", endpoint, volta + 1, attempt + 1)
                    resp = requests.post(endpoint, data={"data": OVERPASS_QUERY_VIAS}, timeout=150, headers=DEFAULT_HEADERS)
                    if resp.status_code in (406, 429) or resp.status_code >= 500:
                        wait = min(2.0 ** attempt, 30.0)
                        LOGGER.warning("Overpass %s respondeu %d, aguardando %.1fs", endpoint, resp.status_code, wait)
                        time.sleep(wait)
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    if "elements" not in data:
                        raise ValueError("resposta sem 'elements'")
                    cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                    LOGGER.info("Overpass (vias) OK via %s: %d elementos", endpoint, len(data["elements"]))
                    return data
                except (requests.RequestException, ValueError) as exc:
                    last_error = exc
                    wait = min(2.0 ** attempt, 30.0)
                    LOGGER.warning("Overpass (vias) %s falhou (%s), aguardando %.1fs", endpoint, exc, wait)
                    time.sleep(wait)
    raise RuntimeError(f"Overpass (vias) indisponível em todos os endpoints, em duas voltas: {last_error}")


def coletar_vias(crs_metrico: str) -> gpd.GeoDataFrame:
    data = _overpass_fetch_vias()
    nodes = {el["id"]: (el["lon"], el["lat"]) for el in data["elements"] if el["type"] == "node"}
    rows = []
    for el in data["elements"]:
        if el["type"] != "way":
            continue
        tags = el.get("tags", {})
        coords = [nodes[n] for n in el.get("nodes", []) if n in nodes]
        if len(coords) < 2:
            continue
        rows.append({
            "osm_id": el["id"],
            "highway": tags.get("highway"),
            "name": tags.get("name", ""),
            "oneway": tags.get("oneway", "no"),
            "lanes": tags.get("lanes"),
            "geometry": LineString(coords),
        })
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326").to_crs(crs_metrico)
    LOGGER.info("malha viária: %d segmentos coletados", len(gdf))
    contagem = gdf["highway"].value_counts().to_dict()
    LOGGER.info("contagem por classe de via: %s", contagem)
    return gdf


# ---------------------------------------------------------------------------
# 3.2 — Zoneamento
# ---------------------------------------------------------------------------

def _checar_vetorial_disponivel() -> bool:
    """spec 3.2: checar antes se há shapefile/GeoJSON publicado. Registra o resultado real, não assume."""
    algum_acessivel = False
    for url in PAGINAS_DADOS_ABERTOS:
        try:
            resp = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=20)
            if resp.status_code == 200:
                algum_acessivel = True
                achou_vetorial = bool(re.search(r'href="[^"]+\.(geojson|shp|zip|json)"', resp.text, re.IGNORECASE))
                LOGGER.info("%s acessível; vetorial de zoneamento encontrado na página? %s", url, achou_vetorial)
                if achou_vetorial:
                    LIMITACOES.append(
                        f"Página {url} parece ter um arquivo vetorial — pipeline não tentou baixá-lo "
                        "automaticamente (checagem só olha a listagem de links); revisar manualmente."
                    )
            else:
                LOGGER.warning("%s respondeu HTTP %d — não deu pra checar vetorial nessa página", url, resp.status_code)
        except requests.RequestException as exc:
            LOGGER.warning("%s inacessível (%s) — servidor da Prefeitura bloqueia a origem deste pipeline", url, exc)
    if not algum_acessivel:
        LIMITACOES.append(
            "As páginas de dados abertos / Mapas e Bairros da Prefeitura de Uberlândia "
            "retornaram erro (bloqueio de acesso automatizado) durante a coleta — não foi possível "
            "confirmar programaticamente se existe camada vetorial de zoneamento. Usado o mapa em PDF "
            "como Plano B, conforme previsto na spec."
        )
    return False  # nunca confirmamos vetorial acessível nesta execução


def _baixar_pdf_zoneamento() -> Path | None:
    ZONEAMENTO_RAW_DIR.mkdir(parents=True, exist_ok=True)
    for rotulo, url in CANDIDATOS_PDF_ZONEAMENTO:
        dest = ZONEAMENTO_RAW_DIR / (Path(url).name.split("?")[0])
        try:
            path, _ = download_if_needed(url, dest, LOGGER, min_size_bytes=100_000, headers={"User-Agent": BROWSER_UA})
            LOGGER.info("mapa de zoneamento baixado: %s (%s)", path, rotulo)
            return path
        except requests.RequestException as exc:
            LOGGER.warning("falha ao baixar mapa de zoneamento %s (%s): %s", rotulo, url, exc)
    LIMITACOES.append("Não foi possível baixar nenhuma versão do mapa de zoneamento (todos os links testados falharam).")
    return None


_PADRAO_CODIGO_NOME = [
    re.compile(r'\b([A-Z]{1,4}[A-Z0-9]{0,3}(?:[ /][A-Z0-9]{1,4})?)\s*-\s*(Zona[^|\n\r]{0,70})'),
]
_PADRAO_NOME_CODIGO = re.compile(r'(Zona[^|\n\r]{0,70}?)\s*-\s*([A-Z]{1,4}[A-Z0-9]{0,3})(?=\s*[\n\r|]|$)')
_LIMPA_CODIGO = re.compile(r'Z[A-Z0-9/ ]*$')


def _extrair_legenda_zonas(md_text: str) -> pd.DataFrame:
    """
    Extração best-effort da legenda de zonas embutida no texto do PDF do mapa.
    O texto vem embaralhado (rótulos do mapa e da legenda compartilham a
    mesma camada de texto do PDF, sem ordem espacial) — só o PAR (código,
    nome da zona) no fim de cada linha da legenda sobrevive de forma
    confiável. "Usos permitidos" NÃO está no mapa (está na lei complementar,
    que não foi acessível para este pipeline) — fica marcado como pendência,
    nunca inventado.
    """
    achados: dict[str, set[str]] = {}
    for pat in _PADRAO_CODIGO_NOME:
        for m in pat.finditer(md_text):
            codigo, nome = m.group(1).strip(), m.group(2).strip()
            m2 = _LIMPA_CODIGO.search(codigo)
            if m2:
                codigo = m2.group(0).strip()
            achados.setdefault(codigo, set()).add(nome)
    for m in _PADRAO_NOME_CODIGO.finditer(md_text):
        nome, codigo = m.group(1).strip(), m.group(2).strip()
        achados.setdefault(codigo, set()).add(nome)

    linhas = []
    for codigo in sorted(achados):
        if not codigo.startswith("Z") or len(codigo) < 2:
            continue  # sobrou lixo de OCR/garbling que não é um código de zona de verdade
        nome = sorted(achados[codigo], key=len)[-1]  # a variante mais longa costuma ser a mais completa
        linhas.append({
            "zona_codigo": codigo,
            "zona_nome": nome,
            "usos_permitidos": "não extraído automaticamente — ver Lei Complementar 812/2026 (texto não acessível a este pipeline)",
        })
    return pd.DataFrame(linhas)


def processar_zoneamento() -> pd.DataFrame:
    _checar_vetorial_disponivel()  # sempre False hoje — resultado registrado em LIMITACOES

    pdf_path = _baixar_pdf_zoneamento()
    if pdf_path is None:
        LIMITACOES.append("Zoneamento não pôde ser verificado: nenhuma fonte (vetorial ou PDF) ficou disponível. "
                           "Verificação de zoneamento marcada como pendência manual por candidato (spec §3.2).")
        return pd.DataFrame(columns=["zona_codigo", "zona_nome", "usos_permitidos"])

    md_path = DATA_INTERIM_MD / f"{pdf_path.name}.md"
    if not md_path.exists():
        LOGGER.info("convertendo %s com markitdown -> %s", pdf_path, md_path)
        subprocess.run([sys.executable, "-m", "markitdown", str(pdf_path), "-o", str(md_path)], check=True)
    else:
        LOGGER.info("markitdown já convertido, pulando: %s", md_path)

    md_text = md_path.read_text(encoding="utf-8")
    zonas = _extrair_legenda_zonas(md_text)
    LOGGER.info("legenda de zonas extraída do PDF: %d zonas identificadas", len(zonas))
    if zonas.empty:
        LIMITACOES.append("O PDF do mapa de zoneamento foi convertido, mas a extração da legenda não encontrou "
                           "nenhum código de zona reconhecível — texto do PDF provavelmente é majoritariamente "
                           "rasterizado/desordenado. Zoneamento fica como pendência manual.")
    else:
        LIMITACOES.append(
            f"Zoneamento: {len(zonas)} zonas identificadas pelo nome/código a partir do mapa em PDF "
            f"({pdf_path.name}), mas a tabela de USOS PERMITIDOS por zona não pôde ser extraída automaticamente "
            "(está na Lei Complementar 812/2026, cujo texto integral não foi acessível pelas fontes públicas "
            "tentadas — leismunicipais.com.br e leis.org bloquearam o acesso automatizado). "
            "Compatibilidade de zoneamento por candidato fica como verificação manual obrigatória antes de assinar contrato."
        )
    zonas["fonte_arquivo"] = pdf_path.name
    return zonas


# ---------------------------------------------------------------------------
# 3.3 — Perímetro urbano
# ---------------------------------------------------------------------------

def calcular_perimetro_urbano(setores: gpd.GeoDataFrame, vias: gpd.GeoDataFrame, buffer_via_m: float = 300) -> gpd.GeoDataFrame:
    """
    spec: "usar o limite municipal do IBGE combinado com densidade de vias
    para excluir zona rural". Combinação: união dos setores que o IBGE já
    classifica como Urbana + uma faixa em volta da malha viária classificada
    (indício de ocupação/adensamento mesmo fora do polígono formal do IBGE),
    tudo recortado pelo limite municipal.
    """
    municipio = setores.geometry.union_all()
    urbana = setores[setores["situacao_setor"].astype(str).str.contains("Urbana", case=False, na=False)]
    urbana_union = urbana.geometry.union_all() if not urbana.empty else municipio.buffer(0).intersection(municipio.buffer(0))

    faixa_vias = vias.geometry.buffer(buffer_via_m).union_all()
    combinado = urbana_union.union(faixa_vias).intersection(municipio)

    gdf = gpd.GeoDataFrame({"origem": ["setores_urbana+buffer_vias"]}, geometry=[combinado], crs=setores.crs)
    LOGGER.info(
        "perímetro urbano: área setores-Urbana=%.1f km², área c/ vias=%.1f km², combinado=%.1f km² (município=%.1f km²)",
        urbana_union.area / 1e6, faixa_vias.area / 1e6, combinado.area / 1e6, municipio.area / 1e6,
    )
    return gdf


# ---------------------------------------------------------------------------

def run() -> tuple[Path, Path, Path]:
    cfg = load_config()
    crs_metrico = cfg["crs"]["metrico"]
    setores_path = DATA_PROCESSED / "setores.gpkg"
    if not setores_path.exists():
        raise FileNotFoundError("data/processed/setores.gpkg não existe — rode a Fase 1 antes.")
    setores = gpd.read_file(setores_path)

    LOGGER.info("=== Fase 3: vias e zoneamento ===")

    vias = coletar_vias(crs_metrico)
    vias_path = DATA_PROCESSED / "vias.gpkg"
    vias.to_file(vias_path, layer="vias", driver="GPKG")
    LOGGER.info("gravado: %s", vias_path)

    zonas = processar_zoneamento()
    zonas_path = DATA_PROCESSED / "zonas_permitidas.csv"
    zonas.to_csv(zonas_path, index=False, encoding="utf-8-sig")
    LOGGER.info("gravado: %s (%d linhas)", zonas_path, len(zonas))

    perimetro = calcular_perimetro_urbano(setores, vias)
    perimetro_path = DATA_PROCESSED / "perimetro_urbano.gpkg"
    perimetro.to_file(perimetro_path, layer="perimetro_urbano", driver="GPKG")
    LOGGER.info("gravado: %s", perimetro_path)

    if LIMITACOES:
        limitacoes_path = DATA_PROCESSED / "limitacoes_fase3.txt"
        limitacoes_path.write_text("\n".join(f"- {l}" for l in LIMITACOES), encoding="utf-8")
        LOGGER.info("gravado: %s (%d limitação(ões) registrada(s), vão para a seção Limitações do relatório)", limitacoes_path, len(LIMITACOES))
        for l in LIMITACOES:
            LOGGER.warning("LIMITAÇÃO — %s", l)

    log_resumo_fase(LOGGER, entrada=len(vias), saida=len(vias), descartados=0)
    return vias_path, zonas_path, perimetro_path


if __name__ == "__main__":
    run()
