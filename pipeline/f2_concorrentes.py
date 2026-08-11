"""
Fase 2 — Concorrência (spec ESPEC_CLAUDE_CODE.md §6).

2.1 OpenStreetMap via Overpass (sempre) — pet shops, agropecuárias, clínicas
    veterinárias.
2.2 Google Places Text Search em grade sobre a mancha urbana (se houver
    GOOGLE_PLACES_API_KEY) — mesmos tipos + termos de busca em português.
2.3 Classificação (tipo, vende_racao).
2.4 Geocodificação das redes conhecidas (config.yaml) e marcação tipo=rede.

Saída: data/processed/concorrentes.gpkg
Roda sozinho: `python pipeline/f2_concorrentes.py`
"""

from __future__ import annotations

import hashlib
import json
import os
import re as _re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import Point

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_PROCESSED, DATA_RAW, DEFAULT_HEADERS,
    get_logger, load_config, log_resumo_fase,
)

LOGGER = get_logger("fase2_concorrentes")

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Places API (New) — a legada (maps.googleapis.com/maps/api/place/textsearch) não está habilitada
# neste projeto (testado em runtime: REQUEST_DENIED "switch to Places API (New)").
GOOGLE_TEXTSEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
GOOGLE_FIELD_MASK = "places.id,places.displayName,places.formattedAddress,places.location,places.rating,places.userRatingCount,places.types,places.businessStatus"
GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

OSM_RAW_DIR = DATA_RAW / "osm"
GOOGLE_RAW_DIR = DATA_RAW / "google_places"
GEOCODE_RAW_DIR = DATA_RAW / "geocode"

GOOGLE_TERMOS = ["pet shop", "agropecuária", "casa de ração", "clínica veterinária", "banho e tosa"]

# heurística de nome pra inferir se uma clínica veterinária também vende ração
# (spec 2.3: "quando indeterminado, marcar NULL e listar para verificação manual")
KEYWORDS_VENDE_RACAO = ["pet shop", "petshop", "pet center", "petcenter", "agro", "raç", "ração", "rural"]


# ---------------------------------------------------------------------------
# 2.1 — OpenStreetMap / Overpass
# ---------------------------------------------------------------------------

OVERPASS_QUERY = """
[out:json][timeout:90];
area["name"="Uberlândia"]["admin_level"="8"]->.a;
(
  node["shop"="pet"](area.a);
  way["shop"="pet"](area.a);
  node["shop"="agrarian"](area.a);
  way["shop"="agrarian"](area.a);
  node["amenity"="veterinary"](area.a);
  way["amenity"="veterinary"](area.a);
);
out center;
""".strip()


def _overpass_fetch() -> dict:
    OSM_RAW_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = OSM_RAW_DIR / "overpass_concorrentes.json"
    if cache_path.exists() and cache_path.stat().st_size > 100:
        LOGGER.info("cache hit Overpass: %s", cache_path)
        return json.loads(cache_path.read_text(encoding="utf-8"))

    last_error = None
    for endpoint in OVERPASS_ENDPOINTS:
        for attempt in range(4):
            try:
                LOGGER.info("Overpass: tentando %s (tentativa %d)", endpoint, attempt + 1)
                resp = requests.post(endpoint, data={"data": OVERPASS_QUERY}, timeout=100, headers=DEFAULT_HEADERS)
                if resp.status_code == 429 or resp.status_code >= 500:
                    wait = 2.0 ** attempt
                    LOGGER.warning("Overpass %s respondeu %d, aguardando %.1fs", endpoint, resp.status_code, wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                data = resp.json()
                if "elements" not in data:
                    raise ValueError("resposta sem 'elements'")
                cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                LOGGER.info("Overpass OK via %s: %d elementos", endpoint, len(data["elements"]))
                return data
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                wait = 2.0 ** attempt
                LOGGER.warning("Overpass %s falhou (%s), aguardando %.1fs", endpoint, exc, wait)
                time.sleep(wait)
    raise RuntimeError(f"Overpass indisponível em todos os endpoints testados: {last_error}")


def _classificar_osm_tipo(tags: dict) -> str:
    if tags.get("shop") == "pet":
        return "pet_shop"
    if tags.get("shop") == "agrarian":
        return "agropecuaria"
    if tags.get("amenity") == "veterinary":
        return "veterinaria"
    return "desconhecido"


def coletar_osm() -> pd.DataFrame:
    data = _overpass_fetch()
    rows = []
    for el in data["elements"]:
        tags = el.get("tags", {})
        if el["type"] == "node":
            lat, lon = el.get("lat"), el.get("lon")
        else:  # way -> usamos "out center"
            center = el.get("center", {})
            lat, lon = center.get("lat"), center.get("lon")
        if lat is None or lon is None:
            continue
        rows.append({
            "fonte": "osm",
            "osm_id": f"{el['type']}/{el['id']}",
            "nome": tags.get("name", ""),
            "tipo": _classificar_osm_tipo(tags),
            "lat": lat, "lon": lon,
            "avaliacoes": None, "rating": None,
            "endereco": tags.get("addr:street", ""),
        })
    df = pd.DataFrame(rows)
    LOGGER.info("OSM: %d concorrentes brutos coletados", len(df))
    return df


# ---------------------------------------------------------------------------
# 2.2 — Google Places (opcional)
# ---------------------------------------------------------------------------

def _grade_urbana(setores_path: Path, espacamento_m: float = 4500, raio_m: float = 3200) -> list[tuple[float, float, float]]:
    """Grade de pontos (lat, lon, raio_m) cobrindo a mancha urbana (setores SITUACAO=Urbana)."""
    setores = gpd.read_file(setores_path)
    urbana = setores[setores["situacao_setor"].astype(str).str.contains("Urbana", case=False, na=False)]
    if urbana.empty:
        LOGGER.warning("nenhum setor marcado como Urbana — usando todos os setores para a grade")
        urbana = setores
    minx, miny, maxx, maxy = urbana.total_bounds  # já em CRS métrico

    import numpy as np
    xs = np.arange(minx, maxx + espacamento_m, espacamento_m)
    ys = np.arange(miny, maxy + espacamento_m, espacamento_m)
    pts = gpd.GeoSeries(
        [Point(x, y) for x in xs for y in ys], crs=urbana.crs
    )
    # só os pontos de fato dentro (ou perto) da mancha urbana
    urbana_union = urbana.geometry.union_all().buffer(espacamento_m / 2)
    pts = pts[pts.within(urbana_union)]
    pts_wgs = pts.to_crs("EPSG:4326")
    grade = [(p.y, p.x, raio_m) for p in pts_wgs]
    LOGGER.info("grade urbana: %d células (espaçamento=%dm, raio=%dm)", len(grade), espacamento_m, raio_m)
    return grade


class GooglePlacesIndisponivel(Exception):
    """A API (chave/projeto) recusou a primeira chamada — não adianta insistir célula por célula."""


def _google_textsearch_cell(query: str, lat: float, lon: float, radius_m: float, api_key: str) -> list[dict]:
    GOOGLE_RAW_DIR.mkdir(parents=True, exist_ok=True)
    key_hash = hashlib.sha1(f"{query}|{lat:.5f}|{lon:.5f}|{radius_m}".encode()).hexdigest()[:16]
    cache_path = GOOGLE_RAW_DIR / f"{key_hash}.json"
    falha_path = GOOGLE_RAW_DIR / f"{key_hash}.failed"
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    if falha_path.exists():
        # já esgotou tentativas nesta célula/termo numa rodada anterior — não martela de novo
        # (idempotência, spec §1.1); ainda conta como falha pra quem chama, não como zero resultado.
        raise RuntimeError("falha persistente cacheada de execução anterior")

    body = {
        "textQuery": f"{query} em Uberlândia MG",
        "languageCode": "pt-BR",
        "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lon}, "radius": float(radius_m)}},
    }
    headers = {"Content-Type": "application/json", "X-Goog-Api-Key": api_key, "X-Goog-FieldMask": GOOGLE_FIELD_MASK}

    last_exc = None
    for attempt in range(5):
        resp = requests.post(GOOGLE_TEXTSEARCH_URL, headers=headers, json=body, timeout=30)
        if resp.status_code in (401, 403):
            raise GooglePlacesIndisponivel(f"HTTP {resp.status_code}: {resp.text[:300]}")
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = 2.0 ** attempt
            LOGGER.warning("Google Places %d, tentativa %d/5, aguardando %.1fs (célula %.4f,%.4f termo=%r)", resp.status_code, attempt + 1, wait, lat, lon, query)
            time.sleep(wait)
            last_exc = RuntimeError(f"HTTP {resp.status_code}")
            continue
        resp.raise_for_status()
        results = resp.json().get("places", [])
        cache_path.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
        return results
    # esgotou as tentativas — marca falha persistente pra não martelar de novo essa mesma
    # célula/termo em cada rerun; fica registrado como falha (não como zero resultado silencioso).
    falha_path.write_text(str(last_exc), encoding="utf-8")
    raise RuntimeError(f"esgotou tentativas nesta célula/termo: {last_exc}")


def _tipo_por_google(place: dict, termo: str) -> str:
    types = place.get("types", [])
    if "veterinary_care" in types:
        return "veterinaria"
    if "veterinária" in termo or "veterinaria" in termo:
        return "veterinaria"
    if "agropecu" in termo:
        return "agropecuaria"
    return "pet_shop"


def coletar_google(setores_path: Path, api_key: str) -> pd.DataFrame:
    grade = _grade_urbana(setores_path)
    rows = []
    n_chamadas = 0
    n_celulas_puladas = 0
    for lat, lon, raio in grade:
        for termo in GOOGLE_TERMOS:
            try:
                resultados = _google_textsearch_cell(termo, lat, lon, raio, api_key)
            except GooglePlacesIndisponivel:
                # a chave/projeto recusou de cara (permissão/billing) — insistir célula por
                # célula só desperdiça tempo; aborta a fonte inteira, degradação graciosa (spec §1.3)
                LOGGER.warning("Google Places indisponível para este projeto/chave — abortando fonte, seguindo só com OSM")
                raise
            except (requests.RequestException, RuntimeError) as exc:
                # falha só nesta célula/termo (rate limit persistente, timeout) — pula e segue,
                # não descarta o que já foi coletado nas outras células
                LOGGER.warning("célula (%.4f,%.4f) termo=%r esgotou tentativas (%s) — pulando só esta", lat, lon, termo, exc)
                n_celulas_puladas += 1
                time.sleep(1.0)
                continue
            n_chamadas += 1
            time.sleep(0.15)  # respiro entre chamadas para não bater no rate limit de novo
            for r in resultados:
                loc = r.get("location", {})
                rows.append({
                    "fonte": "google",
                    "place_id": r.get("id"),
                    "nome": (r.get("displayName") or {}).get("text", ""),
                    "tipo": _tipo_por_google(r, termo),
                    "lat": loc.get("latitude"), "lon": loc.get("longitude"),
                    "avaliacoes": r.get("userRatingCount"),
                    "rating": r.get("rating"),
                    "endereco": r.get("formattedAddress", ""),
                    "business_status": r.get("businessStatus"),
                    "termo_busca": termo,
                })
    df = pd.DataFrame(rows)
    LOGGER.info(
        "Google Places: %d células x termo consultadas (com cache), %d puladas por falha persistente, %d resultados brutos",
        n_chamadas, n_celulas_puladas, len(df),
    )
    if df.empty:
        return df
    antes = len(df)
    df = df.dropna(subset=["place_id"]).drop_duplicates(subset=["place_id"])
    LOGGER.info("Google Places: dedup por place_id %d -> %d", antes, len(df))
    return df


# ---------------------------------------------------------------------------
# Dedup por proximidade + similaridade de nome (spec 2.2, aplicado também
# entre fontes na etapa final para não contar a mesma loja duas vezes)
# ---------------------------------------------------------------------------

def _nome_similar(a: str, b: str, limiar: float = 0.6) -> bool:
    if not a or not b:
        return False
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio() >= limiar


def dedup_espacial(gdf: gpd.GeoDataFrame, raio_m: float = 30) -> gpd.GeoDataFrame:
    """
    Remove duplicatas: mesmo ponto físico (<raio_m) com nome parecido.
    Quando há conflito entre fontes, mantém o registro do Google (tem
    avaliações) sobre o do OSM.
    """
    # 'google' vem antes de 'osm' alfabeticamente -> ascending=True já deixa Google primeiro,
    # que é o que queremos manter (tem avaliações) quando um par (OSM, Google) colide.
    gdf = gdf.sort_values("fonte", ascending=True).reset_index(drop=True)
    sindex = gdf.sindex
    manter = [True] * len(gdf)
    for i, row in gdf.iterrows():
        if not manter[i]:
            continue
        vizinhos = list(sindex.query(row.geometry.buffer(raio_m)))
        for j in vizinhos:
            if j <= i or not manter[j]:
                continue
            if _nome_similar(row["nome"], gdf.loc[j, "nome"]):
                manter[j] = False  # j é o registro "pior" (posterior na ordenação: OSM se houver Google igual)
    resultado = gdf[manter].reset_index(drop=True)
    LOGGER.info("dedup espacial (<%.0fm + nome similar): %d -> %d", raio_m, len(gdf), len(resultado))
    return resultado


# ---------------------------------------------------------------------------
# 2.4 — Redes conhecidas
# ---------------------------------------------------------------------------

def _geocode(endereco: str, api_key: str | None) -> tuple[float, float] | None:
    GEOCODE_RAW_DIR.mkdir(parents=True, exist_ok=True)
    query = f"{endereco}, Uberlândia, MG, Brasil"
    cache_path = GEOCODE_RAW_DIR / f"{hashlib.sha1(query.encode()).hexdigest()[:16]}.json"
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        return (cached["lat"], cached["lon"]) if cached else None

    resultado = None
    if api_key:
        try:
            resp = requests.get(GOOGLE_GEOCODE_URL, params={"address": query, "region": "br", "key": api_key}, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "OK" and data.get("results"):
                loc = data["results"][0]["geometry"]["location"]
                resultado = (loc["lat"], loc["lng"])
        except requests.RequestException as exc:
            LOGGER.warning("geocoding Google falhou para %r: %s", query, exc)

    if resultado is None:
        try:
            resp = requests.get(
                NOMINATIM_URL,
                params={"q": query, "format": "json", "limit": 1},
                headers={"User-Agent": "pipeline-ponto-pet-uberlandia/1.0 (uso interno, pesquisa de ponto comercial)"},
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            if data:
                resultado = (float(data[0]["lat"]), float(data[0]["lon"]))
            time.sleep(1.0)  # política de uso do Nominatim: máx. 1 req/s
        except requests.RequestException as exc:
            LOGGER.warning("geocoding Nominatim falhou para %r: %s", query, exc)

    cache_path.write_text(
        json.dumps({"lat": resultado[0], "lon": resultado[1]} if resultado else None, ensure_ascii=False),
        encoding="utf-8",
    )
    return resultado


def coletar_redes(cfg: dict, api_key: str | None) -> pd.DataFrame:
    rows = []
    for rede in cfg.get("redes_conhecidas", []):
        coord = _geocode(rede["endereco"], api_key)
        if coord is None:
            LOGGER.warning("não consegui geocodificar rede conhecida: %s (%s) — vai faltar no mapa", rede["nome"], rede["endereco"])
            continue
        lat, lon = coord
        rows.append({
            "fonte": "config_redes_conhecidas", "nome": rede["nome"], "tipo": "rede",
            "lat": lat, "lon": lon, "avaliacoes": None, "rating": None,
            "endereco": rede["endereco"],
        })
    df = pd.DataFrame(rows)
    LOGGER.info("redes conhecidas geocodificadas: %d/%d", len(df), len(cfg.get("redes_conhecidas", [])))
    return df


REDE_KEYWORDS = ["petz", "cobasi"]
RAIO_MATCH_REDE_M = 2000        # teto: um candidato pelo nome só conta se o endereço mais perto estiver até aqui
RAIO_MATCH_REDE_ESTRITO_M = 80  # sem o nome bater, só conta como a mesma loja se for bem perto
PISO_AVALIACOES_REDE = 50       # "Petz Lena" tem 7 avaliações — loja de verdade da rede tem muito mais que isso


def _normalizar(txt: str) -> str:
    import unicodedata
    txt = unicodedata.normalize("NFKD", str(txt)).encode("ascii", "ignore").decode("ascii")
    return txt.lower()


def _match_por_texto_endereco(gdf: gpd.GeoDataFrame, endereco_config: str) -> pd.Index:
    """
    Casa pelo TEXTO do endereço (rua + número) em vez de coordenada geocodificada — o
    geocoding (Nominatim, fallback quando o Google Geocoding não está liberado) devolveu, em
    execução real, a MESMA coordenada para "Av. Rondon Pacheco, 505" e "..., 1001" (imprecisão
    de geocoder de rua longa sem número exato) — inútil pra distinguir as duas lojas. O texto do
    endereço que o próprio Google Places devolve pro concorrente é mais confiável.
    """
    numero = _re.search(r"\d+", endereco_config)
    if numero is None:
        return pd.Index([])
    numero = numero.group(0)
    rua = _re.sub(r",?\s*\d+.*$", "", endereco_config)  # tudo antes do número
    rua_norm = _normalizar(rua)
    rua_tokens = [t for t in _re.split(r"\s+", rua_norm) if len(t) > 2 and t not in ("av", "avenida", "rua", "r")]

    endereco_norm = gdf["endereco"].astype(str).apply(_normalizar)
    bate_numero = endereco_norm.str.contains(rf"\b{numero}\b", regex=True, na=False)
    bate_rua = endereco_norm.apply(lambda e: any(t in e for t in rua_tokens)) if rua_tokens else True
    return gdf.index[bate_numero & bate_rua]


def resolver_redes(gdf: gpd.GeoDataFrame, redes: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Consolida cada endereço de rede conhecido (config.yaml) num único registro tipo=rede,
    escolhendo o melhor candidato entre o que OSM/Google já encontraram (prioridade: 1º casar
    pelo texto do endereço — rua+número —, 2º pelo nome+proximidade da coordenada geocodificada;
    entre Google e OSM prefere Google, que tem avaliações) e descartando os outros duplicados da
    mesma loja física.

    Por que não só distância até a coordenada geocodificada: em Uberlândia os 3 endereços de
    rede do config ficam a menos de 2km entre si (centro da cidade) — e o próprio geocoder já
    devolveu coordenadas imprecisas (chegou a dar o mesmo ponto pra dois endereços diferentes).
    O texto do endereço (que o Google Places retorna pro concorrente de verdade) é o sinal mais
    confiável; a distância geocodificada é só um plano B, com atribuição por vizinho MAIS
    PRÓXIMO — nunca "qualquer um dentro do raio", senão os três colapsam na loja com mais
    avaliações (bug real visto em execução).

    Duas outras armadilhas:
    - "Uai Pet Store", vizinha da Petz num shopping grande, não pode virar rede só por estar
      perto — por isso a proximidade sozinha (sem o nome bater) usa um raio bem mais estrito.
    - "Petz Lena", uma lojinha de bairro sem nada a ver com a rede (7 avaliações), não pode virar
      rede só por ter "petz" no nome — por isso o nome só conta combinado com um piso de avaliações
      (fonte OSM, que não tem avaliação nenhuma, fica isenta desse piso).
    """
    if redes.empty:
        return gdf
    gdf = gdf.copy()
    nome_lower = gdf["nome"].astype(str).str.lower()
    tem_keyword = nome_lower.apply(lambda n: any(k in n for k in REDE_KEYWORDS))
    avaliacoes_num = pd.to_numeric(gdf["avaliacoes"], errors="coerce").fillna(0)
    provavel_por_nome = tem_keyword & ((avaliacoes_num >= PISO_AVALIACOES_REDE) | gdf["fonte"].eq("osm"))

    dist_por_endereco = pd.DataFrame({idx_r: gdf.geometry.distance(r.geometry) for idx_r, r in redes.iterrows()})
    endereco_mais_perto = dist_por_endereco.idxmin(axis=1)
    dist_mais_perto = dist_por_endereco.min(axis=1)

    elegivel_distancia = (provavel_por_nome & (dist_mais_perto < RAIO_MATCH_REDE_M)) | (dist_mais_perto < RAIO_MATCH_REDE_ESTRITO_M)

    prioridade = {"google": 0, "osm": 1}
    vencedores = {}
    descartar: set = set()
    for idx_r in redes.index:
        candidatos_idx = _match_por_texto_endereco(gdf, redes.loc[idx_r, "endereco"])
        origem = "texto do endereço"
        if len(candidatos_idx) > 0:
            # mesmo endereço de texto não basta em shopping: várias lojas dividem o mesmo prédio/CEP
            # (ex.: "Uai Pet Store" e a Petz de verdade, ambas "Av. João Naves de Ávila, 1331") —
            # entre quem bate o endereço, prioriza quem também tem a marca no nome.
            com_nome = [i for i in candidatos_idx if tem_keyword.loc[i]]
            if com_nome:
                candidatos_idx = pd.Index(com_nome)
        if len(candidatos_idx) == 0:
            candidatos_idx = gdf.index[elegivel_distancia & (endereco_mais_perto == idx_r)]
            origem = "nome+proximidade"
        if len(candidatos_idx) == 0:
            LOGGER.warning("nenhum registro OSM/Google encontrado perto de %r — vai entrar só com a coordenada geocodificada", redes.loc[idx_r, "nome"])
            continue
        sub = gdf.loc[candidatos_idx].assign(
            _p=gdf.loc[candidatos_idx, "fonte"].map(prioridade).fillna(2),
            _av=avaliacoes_num.loc[candidatos_idx],
        ).sort_values(["_p", "_av"], ascending=[True, False])
        vencedores[idx_r] = sub.index[0]
        descartar.update(i for i in candidatos_idx if i != sub.index[0])
        LOGGER.info(
            "rede %r resolvida para %r (fonte=%s, casado por %s); %d duplicata(s) da mesma loja removida(s)",
            redes.loc[idx_r, "nome"], gdf.loc[sub.index[0], "nome"], gdf.loc[sub.index[0], "fonte"], origem, len(candidatos_idx) - 1,
        )

    vencedores_idx = set(vencedores.values())
    gdf.loc[list(vencedores_idx), "tipo"] = "rede"
    descartar -= vencedores_idx  # por segurança: nunca descartar quem também venceu outro endereço
    if descartar:
        LOGGER.info("removendo %d registro(s) duplicado(s) da mesma loja física de rede", len(descartar))
        gdf = gdf.drop(index=list(descartar))
    gdf = gdf.reset_index(drop=True)

    sem_candidato = [idx_r for idx_r in redes.index if idx_r not in vencedores]
    if sem_candidato:
        fallback = redes.loc[sem_candidato].copy()
        fallback["tipo"] = "rede"
        gdf = pd.concat([gdf, fallback], ignore_index=True)
        LOGGER.info("%d rede(s) sem par nas fontes automáticas, adicionada(s) só com a coordenada geocodificada: %s",
                    len(fallback), list(fallback["nome"]))
    return gdf


# ---------------------------------------------------------------------------
# Classificação vende_racao
# ---------------------------------------------------------------------------

def _vende_racao(row) -> object:
    if row["tipo"] in ("pet_shop", "agropecuaria", "rede"):
        return True
    if row["tipo"] == "veterinaria":
        nome = str(row.get("nome") or "").lower()
        if any(k in nome for k in KEYWORDS_VENDE_RACAO):
            return True
        return pd.NA  # indeterminado — spec 2.3: marcar NULL para checagem manual
    return pd.NA


def run() -> Path:
    cfg = load_config()
    crs_metrico = cfg["crs"]["metrico"]
    setores_path = DATA_PROCESSED / "setores.gpkg"
    if not setores_path.exists():
        raise FileNotFoundError("data/processed/setores.gpkg não existe — rode a Fase 1 antes.")

    LOGGER.info("=== Fase 2: concorrência ===")

    df_osm = coletar_osm()

    api_key = os.getenv("GOOGLE_PLACES_API_KEY", "").strip()
    if api_key:
        try:
            df_google = coletar_google(setores_path, api_key)
        except (requests.RequestException, GooglePlacesIndisponivel) as exc:
            LOGGER.warning("Google Places falhou por completo (%s) — seguindo só com OSM, conforme degradação graciosa", exc)
            df_google = pd.DataFrame()
    else:
        LOGGER.warning("GOOGLE_PLACES_API_KEY ausente — seguindo só com OSM (spec: opcional)")
        df_google = pd.DataFrame()

    partes = [d for d in (df_osm, df_google) if not d.empty]
    if not partes:
        raise RuntimeError("nenhuma fonte de concorrência disponível (OSM falhou e Google não configurado/falhou)")
    combinado = pd.concat(partes, ignore_index=True)
    n_bruto = len(combinado)

    combinado = combinado.dropna(subset=["lat", "lon"])
    gdf = gpd.GeoDataFrame(
        combinado,
        geometry=gpd.points_from_xy(combinado["lon"], combinado["lat"]),
        crs="EPSG:4326",
    ).to_crs(crs_metrico)

    # filtra pontos fora do polígono municipal (aceite: "nenhum ponto fora do polígono municipal")
    setores = gpd.read_file(setores_path)
    poligono_municipio = setores.geometry.union_all().buffer(200)  # 200m de tolerância p/ imprecisão de geocoding
    dentro = gdf.within(poligono_municipio)
    n_fora = (~dentro).sum()
    if n_fora:
        LOGGER.warning("%d concorrente(s) fora do polígono municipal (buffer 200m) — descartados", n_fora)
    gdf = gdf[dentro].reset_index(drop=True)

    gdf = dedup_espacial(gdf)

    redes = coletar_redes(cfg, api_key or None)
    redes_gdf = gpd.GeoDataFrame(
        redes, geometry=gpd.points_from_xy(redes["lon"], redes["lat"]), crs="EPSG:4326"
    ).to_crs(crs_metrico) if not redes.empty else gpd.GeoDataFrame(columns=["nome", "geometry"], geometry="geometry", crs=crs_metrico)

    gdf = resolver_redes(gdf, redes_gdf)

    gdf["vende_racao"] = gdf.apply(_vende_racao, axis=1)

    n_saida = len(gdf)
    contagem_tipo = gdf["tipo"].value_counts().to_dict()
    LOGGER.info("contagem por tipo: %s", contagem_tipo)

    n_redes = (gdf["tipo"] == "rede").sum()
    if n_redes < len(cfg.get("redes_conhecidas", [])):
        LOGGER.warning("ACEITE — só %d/%d redes conhecidas presentes no resultado final", n_redes, len(cfg.get("redes_conhecidas", [])))
    else:
        LOGGER.info("ACEITE — as %d redes conhecidas estão presentes", n_redes)

    out_path = DATA_PROCESSED / "concorrentes.gpkg"
    gdf_out = gdf.drop(columns=[c for c in ("business_status", "termo_busca") if c in gdf.columns])
    gdf_out.to_file(out_path, layer="concorrentes", driver="GPKG")
    LOGGER.info("gravado: %s", out_path)

    log_resumo_fase(
        LOGGER, entrada=n_bruto, saida=n_saida,
        descartados=n_bruto - n_saida,
        motivo_descarte="fora do polígono municipal e/ou duplicata (proximidade+nome)",
    )
    return out_path


if __name__ == "__main__":
    run()
