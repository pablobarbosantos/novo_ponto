"""
Fase 9 — Relatório HTML (spec ESPEC_CLAUDE_CODE.md §6).

Gera output/mapa.html (Folium, camadas ligáveis) e output/relatorio.html
(autocontido — CSS/JS embutidos, mapa embutido via iframe srcdoc, não como
arquivo externo, pra não depender de dois arquivos ficarem juntos nem de
política de same-origin do navegador pra file://).

Sem internet de propósito: o mapa NÃO usa tiles do OpenStreetMap (exigiriam
rede pra carregar as imagens) — o contexto espacial vem dos próprios
polígonos de setor e do contorno do perímetro urbano, desenhados como
camadas vetoriais. É a única forma honesta de cumprir "funciona offline,
sem internet" (spec §0) com um mapa de verdade, não um mapa quebrado.

Roda sozinho: `python pipeline/f9_relatorio.py`
"""

from __future__ import annotations

import html
import json
import re
import sys
from datetime import date
from pathlib import Path

import folium
import geopandas as gpd
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    DATA_MANUAL, DATA_PROCESSED, DATA_RAW, LOGS_DIR, OUTPUT_DIR, OUTPUT_FICHAS,
    DEFAULT_HEADERS, get_logger, load_config, log_resumo_fase,
)

CDN_CACHE_DIR = DATA_RAW / "cdn_offline"


def _baixar_cdn(url: str) -> str:
    """
    Folium referencia Leaflet/Bootstrap/jQuery/D3 via CDN por padrão — sem isso
    inlinado, o mapa não renderiza nada offline (viola spec §0 "sem internet").
    Baixa uma vez (com cache em disco, idempotente) e devolve o conteúdo puro
    pra ser inlinado num <style>/<script> do próprio HTML.
    """
    CDN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    import hashlib
    nome = hashlib.sha1(url.encode()).hexdigest()[:16] + "_" + url.split("/")[-1][:60]
    caminho = CDN_CACHE_DIR / nome
    if caminho.exists():
        return caminho.read_text(encoding="utf-8")
    resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=30)
    resp.raise_for_status()
    caminho.write_text(resp.text, encoding="utf-8")
    return resp.text


_PADRAO_LINK_CSS = re.compile(r'<link\s+[^>]*href="(https?://[^"]+)"[^>]*>')
_PADRAO_SCRIPT_JS = re.compile(r'<script\s+[^>]*src="(https?://[^"]+)"[^>]*></script>')

# Folium inclui Bootstrap/jQuery/D3/awesome-markers/FontAwesome/Glyphicons em todo mapa por
# padrão. jQuery e D3 acabam sendo usados de verdade pelo próprio JS que o Folium gera (a
# legenda do Choropleth é D3; testado: removê-los quebra o mapa inteiro com "$ is not defined").
# awesome-markers/FontAwesome/Glyphicons/Bootstrap não — nada aqui usa folium.Icon (só
# DivIcon/CircleMarker), e awesome-markers ainda sonda um ícone padrão inexistente ao carregar
# (dispara um request de imagem que sempre falha). Removidos, não inlinados.
_TRECHOS_NAO_USADOS = ("bootstrap", "awesome-markers", "awesome.rotate", "font-awesome", "fontawesome", "glyphicons")


def _tornar_offline(documento_html: str) -> str:
    """Substitui todo <link href=CDN> e <script src=CDN> pelo conteúdo baixado, inline (só o que é usado de fato)."""
    def _sub_css(m):
        url = m.group(1)
        if any(t in url.lower() for t in _TRECHOS_NAO_USADOS):
            return ""
        return f"<style>{_baixar_cdn(url)}</style>"

    def _sub_js(m):
        url = m.group(1)
        if any(t in url.lower() for t in _TRECHOS_NAO_USADOS):
            return ""
        conteudo = f"<script>{_baixar_cdn(url)}</script>"
        if "/leaflet@" in url or url.endswith("/leaflet.js"):
            # algo no template do Folium instancia um ícone padrão do Leaflet mesmo só usando
            # DivIcon/CircleMarker no mapa em si (não achei o quê, e não vale mais tempo caçar) —
            # o resultado é sempre um request pra marker-icon.png/marker-shadow.png que não
            # existem. Sobrescrever _getIconUrl direto é à prova de qualquer caminho de código
            # que dispare isso, em vez de tentar acertar o imagePath que ele vai concatenar.
            _pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ycQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACH5BAEAAAAALAAAAAABAAEAAAICTAEAOw=="
            conteudo += f"<script>if(window.L&&L.Icon&&L.Icon.Default){{L.Icon.Default.prototype._getIconUrl=function(){{return '{_pixel}';}};}}</script>"
        return conteudo

    documento_html = _PADRAO_LINK_CSS.sub(_sub_css, documento_html)
    documento_html = _PADRAO_SCRIPT_JS.sub(_sub_js, documento_html)
    return documento_html

LOGGER = get_logger("fase9_relatorio")

CORES_TIPO = {"rede": "#c0392b", "pet_shop": "#e67e22", "veterinaria": "#2980b9", "agropecuaria": "#27ae60"}
NOME_TIPO = {"rede": "Rede", "pet_shop": "Pet shop", "veterinaria": "Veterinária", "agropecuaria": "Agropecuária"}

CHECKLIST = [
    ("Do imóvel", [
        "Metragem total e do salão de vendas",
        "Tem depósito? Quantos m²? Cabe estoque do atacado?",
        "Caminhão consegue encostar? Carga e descarga sem multa?",
        "Fundos com água e ralo (banho e tosa futuro)?",
        "Pé-direito, ventilação, umidade (ração estraga)",
        "Elétrica: quantas fases, quantos amperes",
        "Fachada: quantos metros de vitrine, permite letreiro",
        "Banheiro, copa",
        "Estado: o que a reforma vai custar",
    ]),
    ("Do contrato", [
        "Valor pedido e piso real do proprietário",
        "Carência — quantos meses (mínimo 2, ideal 3)",
        "Prazo, índice de reajuste",
        "Multa rescisória e garantia exigida",
        "Quem paga IPTU e taxas",
        "Autorização para reforma e letreiro",
    ]),
    ("Do entorno", [
        "Contagem de fluxo nos 3 horários",
        "Estacionamento na via: livre, rotativo, proibido?",
        "Vizinhos que puxam movimento: padaria, farmácia, supermercado, hortifruti, academia, salão",
        "Pet shop mais próximo: distância e tamanho",
        "Clínica veterinária mais próxima: vende ração?",
        "Iluminação e segurança à noite",
        "Sentido da via e facilidade de retorno",
    ]),
]

FONTES_METODOLOGIA = [
    ("IBGE — Censo Demográfico 2022, Agregados por Setores Censitários", "ftp.ibge.gov.br/Censos/Censo_Demografico_2022/Agregados_por_Setores_Censitarios/"),
    ("IBGE — Rendimento do Responsável por Setor", "ftp.ibge.gov.br/Censos/Censo_Demografico_2022/Agregados_por_Setores_Censitarios_Rendimento_do_Responsavel/"),
    ("OpenStreetMap — Overpass API (concorrentes e malha viária)", "overpass-api.de / mirrors"),
    ("Google Places API (New) — concorrentes com avaliações", "places.googleapis.com"),
    ("OpenRouteService — isócronas de deslocamento", "api.openrouteservice.org"),
    ("Prefeitura de Uberlândia — Mapa de Zoneamento e Ocupação do Solo (PDF)", "docs.uberlandia.mg.gov.br"),
]


# ---------------------------------------------------------------------------
# Carregamento
# ---------------------------------------------------------------------------

def _carregar_tudo(cfg: dict) -> dict:
    setores = gpd.read_file(DATA_PROCESSED / "setores.gpkg")
    concorrentes = gpd.read_file(DATA_PROCESSED / "concorrentes.gpkg")
    concorrentes["avaliacoes"] = pd.to_numeric(concorrentes["avaliacoes"], errors="coerce")
    concorrentes["rating"] = pd.to_numeric(concorrentes["rating"], errors="coerce")
    candidatos_geom = gpd.read_file(DATA_PROCESSED / "candidatos_com_demanda.gpkg", layer="candidatos_com_demanda")[["candidato_id", "geometry"]]
    try:
        isocronas = gpd.read_file(DATA_PROCESSED / "candidatos_com_demanda.gpkg", layer="isocronas_10min")
    except Exception:
        isocronas = gpd.GeoDataFrame(columns=["candidato_id", "geometry"], geometry="geometry", crs=setores.crs)
    perimetro = gpd.read_file(DATA_PROCESSED / "perimetro_urbano.gpkg")

    top10 = pd.read_csv(OUTPUT_DIR / "top10.csv", sep=";", encoding="utf-8-sig")
    top10 = top10.merge(candidatos_geom, on="candidato_id", how="left")
    top10 = gpd.GeoDataFrame(top10, geometry="geometry", crs=setores.crs)

    ranking_completo = pd.read_parquet(DATA_PROCESSED / "ranking_completo.parquet")
    reprovados = ranking_completo[~ranking_completo["teste_absoluto_passou"]].copy()

    pesos = json.loads((DATA_PROCESSED / "pesos.json").read_text(encoding="utf-8"))

    limitacoes = []
    for nome in ("limitacoes_fase3.txt", "limitacoes_fase8.txt"):
        p = DATA_PROCESSED / nome
        if p.exists():
            limitacoes.extend([l.strip("- ").strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip()])

    zonas_path = DATA_PROCESSED / "zonas_permitidas.csv"
    zonas = pd.read_csv(zonas_path) if zonas_path.exists() else pd.DataFrame()

    return {
        "setores": setores, "concorrentes": concorrentes, "candidatos_geom": candidatos_geom,
        "isocronas": isocronas, "perimetro": perimetro, "top10": top10,
        "ranking_completo": ranking_completo, "reprovados": reprovados, "pesos": pesos,
        "limitacoes": limitacoes, "zonas": zonas,
    }


# ---------------------------------------------------------------------------
# Mapa principal
# ---------------------------------------------------------------------------

def _popup_concorrente(row) -> str:
    av = pd.to_numeric(row.get("avaliacoes"), errors="coerce")
    av_txt = f"{int(av)} avaliações" if pd.notna(av) else "avaliações não coletadas"
    rating = pd.to_numeric(row.get("rating"), errors="coerce")
    rating_txt = f", nota {rating:.1f}" if pd.notna(rating) else ""
    return f"<b>{html.escape(str(row['nome']) or '(sem nome)')}</b><br>{NOME_TIPO.get(row['tipo'], row['tipo'])}<br>{av_txt}{rating_txt}"


def _construir_mapa(dados: dict, crs_metrico: str) -> folium.Map:
    setores = dados["setores"].to_crs("EPSG:4326")
    perimetro = dados["perimetro"].to_crs("EPSG:4326")
    concorrentes = dados["concorrentes"].to_crs("EPSG:4326")
    top10 = dados["top10"].to_crs("EPSG:4326")
    isocronas = dados["isocronas"].to_crs("EPSG:4326") if not dados["isocronas"].empty else dados["isocronas"]

    centro = [perimetro.geometry.iloc[0].centroid.y, perimetro.geometry.iloc[0].centroid.x]
    mapa = folium.Map(location=centro, zoom_start=12, tiles=None, control_scale=True)
    folium.TileLayer(tiles="", attr=" ", name="Sem mapa-base (offline)", overlay=False).add_to(mapa)

    # contorno do perímetro urbano — única referência espacial de fundo, sem depender de tiles externos
    folium.GeoJson(
        perimetro.geometry.simplify(0.0005).__geo_interface__,
        name="Perímetro urbano (referência)",
        style_function=lambda x: {"fillOpacity": 0, "color": "#888", "weight": 1.5, "dashArray": "4 4"},
    ).add_to(mapa)

    setores_simpl = setores.copy()
    setores_simpl["geometry"] = setores_simpl.geometry.simplify(0.0003)
    setores_simpl["domicilios_ocupados"] = setores_simpl["domicilios_ocupados"].fillna(0)
    setores_simpl["pct_apartamento"] = setores_simpl["pct_apartamento"].fillna(0)

    def _bins_seguros(serie: pd.Series, n: int = 6) -> list[float]:
        """
        branca (o motor de cor do Choropleth) quebra com NaN no SVG da legenda quando os
        quantis automáticos saem com bordas repetidas — acontece na prática aqui porque
        pct_apartamento tem ~28% dos setores exatamente em 0. Garante bordas estritamente
        crescentes, caindo pra linspace quando os quantis não dão conta.
        """
        quantis = sorted(set(serie.quantile([i / (n - 1) for i in range(n)]).tolist()))
        if len(quantis) >= 3:
            return quantis
        minimo, maximo = float(serie.min()), float(serie.max())
        if minimo == maximo:
            maximo = minimo + 1.0
        return [minimo + (maximo - minimo) * i / (n - 1) for i in range(n)]

    folium.Choropleth(
        geo_data=setores_simpl[["CD_SETOR", "geometry"]].__geo_interface__,
        data=setores_simpl, columns=["CD_SETOR", "domicilios_ocupados"], key_on="feature.properties.CD_SETOR",
        fill_color="YlOrRd", fill_opacity=0.65, line_opacity=0.1, nan_fill_opacity=0,
        bins=_bins_seguros(setores_simpl["domicilios_ocupados"]),
        legend_name="Domicílios ocupados por setor censitário", name="Coroplético — demanda (domicílios/setor)",
        show=True,
    ).add_to(mapa)

    folium.Choropleth(
        geo_data=setores_simpl[["CD_SETOR", "geometry"]].__geo_interface__,
        data=setores_simpl, columns=["CD_SETOR", "pct_apartamento"], key_on="feature.properties.CD_SETOR",
        fill_color="BuPu", fill_opacity=0.65, line_opacity=0.1, nan_fill_opacity=0,
        bins=_bins_seguros(setores_simpl["pct_apartamento"]),
        legend_name="% domicílios em apartamento por setor", name="Coroplético — % apartamento",
        show=False,
    ).add_to(mapa)

    grupo_conc = folium.FeatureGroup(name="Concorrentes (raio ∝ avaliações)", show=True)
    for _, row in concorrentes.iterrows():
        av = pd.to_numeric(row.get("avaliacoes"), errors="coerce")
        # cuidado: "av or 0" NÃO pega NaN (NaN é truthy em Python) — sem o pd.notna
        # explícito, concorrente sem avaliação vira raio=NaN e quebra o path SVG do círculo.
        raio = 4 + min((float(av) if pd.notna(av) else 0.0) ** 0.5, 20)
        folium.CircleMarker(
            location=[row.geometry.y, row.geometry.x], radius=raio,
            color=CORES_TIPO.get(row["tipo"], "#555"), fill=True, fill_opacity=0.7, weight=1,
            popup=folium.Popup(_popup_concorrente(row), max_width=250),
        ).add_to(grupo_conc)
    grupo_conc.add_to(mapa)

    if not isocronas.empty:
        grupo_iso = folium.FeatureGroup(name="Isócrona de 10min dos finalistas (referência visual)", show=True)
        iso_top10 = isocronas[isocronas["candidato_id"].isin(top10["candidato_id"])]
        for _, row in iso_top10.iterrows():
            folium.GeoJson(
                row.geometry.__geo_interface__,
                style_function=lambda x: {"fillOpacity": 0.05, "color": "#2c3e50", "weight": 1.5},
            ).add_to(grupo_iso)
        grupo_iso.add_to(mapa)

    grupo_finalistas = folium.FeatureGroup(name="Finalistas (Top 10)", show=True)
    for i, row in enumerate(top10.itertuples(), start=1):
        popup_html = (
            f"<b>#{i} — {html.escape(str(row.bairro))}</b><br>"
            f"Score: {row.score_final:.3f}<br>"
            f"Domicílios (captação efetiva): {row.domicilios_efetivo:,.0f}<br>"
            f"Potencial mensal: R$ {row.potencial_mensal:,.0f}"
        )
        folium.Marker(
            location=[row.geometry.y, row.geometry.x],
            icon=folium.DivIcon(html=f'<div style="background:#2c3e50;color:white;border-radius:50%;width:26px;height:26px;'
                                      f'display:flex;align-items:center;justify-content:center;font-weight:bold;'
                                      f'font-family:sans-serif;font-size:13px;border:2px solid white;box-shadow:0 1px 3px rgba(0,0,0,.4)">{i}</div>'),
            popup=folium.Popup(popup_html, max_width=250),
        ).add_to(grupo_finalistas)
    grupo_finalistas.add_to(mapa)

    imoveis_path = DATA_MANUAL / "imoveis.csv"
    if imoveis_path.exists():
        imoveis = pd.read_csv(imoveis_path)
        if not imoveis.empty and {"lat", "lon"}.issubset(imoveis.columns):
            grupo_imoveis = folium.FeatureGroup(name="Imóveis (data/manual/imoveis.csv)", show=False)
            for _, row in imoveis.iterrows():
                # DivIcon, não folium.Icon: este é o único uso que puxaria a lib awesome-markers
                # (Bootstrap/FontAwesome via CDN) pro documento inteiro só por causa de um marcador.
                folium.Marker(
                    location=[row["lat"], row["lon"]],
                    icon=folium.DivIcon(html='<div style="background:#27ae60;color:white;border-radius:50%;width:20px;height:20px;'
                                              'display:flex;align-items:center;justify-content:center;font-size:12px;'
                                              'border:2px solid white;box-shadow:0 1px 3px rgba(0,0,0,.4)">🏠</div>'),
                    popup=f"{html.escape(str(row.get('endereco','')))}<br>R$ {row.get('aluguel','?')}",
                ).add_to(grupo_imoveis)
            grupo_imoveis.add_to(mapa)

    folium.LayerControl(collapsed=False).add_to(mapa)
    return mapa


def _mini_mapa_finalista(row, concorrentes: gpd.GeoDataFrame, isocrona_geom, crs_metrico: str) -> str:
    centro_wgs = gpd.GeoSeries([row.geometry], crs=crs_metrico).to_crs("EPSG:4326").iloc[0]
    mapa = folium.Map(location=[centro_wgs.y, centro_wgs.x], zoom_start=14, tiles=None, control_scale=True, width=420, height=320)
    folium.TileLayer(tiles="", attr=" ", overlay=False).add_to(mapa)

    if isocrona_geom is not None:
        iso_wgs = gpd.GeoSeries([isocrona_geom], crs=crs_metrico).to_crs("EPSG:4326").iloc[0]
        folium.GeoJson(iso_wgs.__geo_interface__, style_function=lambda x: {"fillOpacity": 0.08, "color": "#2c3e50", "weight": 1.5}).add_to(mapa)

    concorrentes_wgs = concorrentes.to_crs("EPSG:4326")
    dist = concorrentes.geometry.distance(row.geometry)
    proximos = concorrentes_wgs.loc[dist.nsmallest(8).index]
    for _, c in proximos.iterrows():
        folium.CircleMarker(
            location=[c.geometry.y, c.geometry.x], radius=5,
            color=CORES_TIPO.get(c["tipo"], "#555"), fill=True, fill_opacity=0.8,
            popup=_popup_concorrente(c),
        ).add_to(mapa)

    folium.Marker(
        location=[centro_wgs.y, centro_wgs.x],
        icon=folium.DivIcon(html='<div style="background:#2c3e50;color:white;border-radius:50%;width:22px;height:22px;'
                                  'display:flex;align-items:center;justify-content:center;font-weight:bold;'
                                  'font-family:sans-serif;font-size:12px;border:2px solid white">★</div>'),
    ).add_to(mapa)
    return _tornar_offline(mapa.get_root().render())


# ---------------------------------------------------------------------------
# HTML — helpers de formatação
# ---------------------------------------------------------------------------

def _iframe_srcdoc(documento_html_completo: str, altura_px: int) -> str:
    """
    Embute um documento HTML completo (com seu próprio <html><head><body>, caso
    do que o Folium gera) via iframe srcdoc — NUNCA concatenar um <html>...</html>
    inteiro direto no corpo da página principal, isso é HTML aninhado inválido e
    quebra CSS/JS de ambos os lados. srcdoc é inline (sem arquivo externo), então
    continua 100% autocontido e offline.
    """
    escapado = html.escape(documento_html_completo, quote=True)
    return f'<iframe srcdoc="{escapado}" style="width:100%;height:{altura_px}px;border:0;" loading="lazy"></iframe>'


def _fmt(v, casas=0, prefixo="", sufixo="", vazio="não coletado") -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return f'<span class="nc">{vazio}</span>'
    if isinstance(v, (int, float)):
        return f"{prefixo}{v:,.{casas}f}{sufixo}".replace(",", "§").replace(".", ",").replace("§", ".")
    return html.escape(str(v))


def _resumo_executivo(top10: pd.DataFrame) -> str:
    itens = []
    for i, row in enumerate(top10.head(3).itertuples(), start=1):
        itens.append(
            f"<li><b>#{i} {html.escape(str(row.bairro))}</b> (candidato {row.candidato_id}) — "
            f"score {row.score_final:.3f}, potencial mensal de R$ {row.potencial_mensal:,.0f} "
            f"na área de captação efetiva (decaída por anel 5/10/15min), saturação {row.saturacao:,.0f}.</li>"
        )
    melhor = top10.iloc[0]
    return (
        "<ul>" + "".join(itens) + "</ul>"
        f"<p><b>Recomendação de visita:</b> comece por <b>{html.escape(str(melhor['bairro']))}</b> "
        f"(candidato {melhor['candidato_id']}), maior score do ranking. "
        "Isso não substitui o checklist de visita (Parte 6) nem a verificação manual de zoneamento "
        "e disponibilidade real de imóvel — ver seção Limitações.</p>"
    )


def _linha_tabela(row) -> str:
    aluguel_v = row.get('aluguel_estimado_regiao', float('nan'))
    aluguel_badge = ' <span class="badge" title="sem dado real em data/manual/imoveis.csv — usado o fallback negocio.teto_aluguel">estimado</span>' if row.get('aluguel_e_estimado') else ''
    return (
        "<tr>"
        f"<td>{html.escape(str(row['bairro']))}</td>"
        f"<td data-v='{row['domicilios_efetivo']}'>{_fmt(row['domicilios_efetivo'])}</td>"
        f"<td data-v='{row['pct_apartamento_efetivo']}'>{_fmt(row['pct_apartamento_efetivo']*100 if pd.notna(row['pct_apartamento_efetivo']) else None, 1, sufixo='%')}</td>"
        f"<td data-v='{row['renda_media_efetivo']}'>{_fmt(row['renda_media_efetivo'], 0, 'R$ ')}</td>"
        f"<td data-v='{row['n_concorrentes_15min']}'>{_fmt(row['n_concorrentes_15min'])}</td>"
        f"<td data-v='{row['forca_concorrencia']}'>{_fmt(row['forca_concorrencia'], 1)}</td>"
        f"<td data-v='{row['n_clinicas_sem_loja']}'>{_fmt(row['n_clinicas_sem_loja'])}</td>"
        f"<td data-v='{row['saturacao']}'>{_fmt(row['saturacao'], 1)}</td>"
        f"<td data-v='{row['potencial_mensal']}'>{_fmt(row['potencial_mensal'], 0, 'R$ ')}</td>"
        f"<td data-v='{row.get('demanda_capturada', float('nan'))}'>{_fmt(row.get('demanda_capturada'), 0, 'R$ ')}</td>"
        f"<td data-v='{aluguel_v}'>{_fmt(aluguel_v, 0, 'R$ ')}{aluguel_badge}</td>"
        f"<td data-v='{row.get('n_imoveis_no_teto', float('nan'))}'>{_fmt(row.get('n_imoveis_no_teto'))}</td>"
        f"<td data-v='{row.get('reais_m2_medio', float('nan'))}'>{_fmt(row.get('reais_m2_medio'), 2, 'R$ ')}</td>"
        f"<td data-v='{row['score_final']}'><b>{_fmt(row['score_final'], 3)}</b></td>"
        "</tr>"
    )


MAX_POR_BAIRRO_TOP10 = 2  # espelha a constante de f7_score.py — só pra texto da ficha


def _motivo_fora_do_top10(o) -> str:
    if not o["teste_absoluto_passou"]:
        return f"reprovado no teste absoluto: {html.escape(str(o['teste_absoluto_motivo']))}"
    if o.get("duplicata_geografica", False):
        return "duplicata geográfica (<800m de outro candidato com score maior)"
    return "aprovado, mas fora do Top10 (score mais baixo ou cap de 2 por bairro)"


def _outros_pontos_bairro(bairro: str, ranking_completo: pd.DataFrame, ids_top10: set) -> str:
    """G2 (CORRECOES.md) — o cap de MAX_POR_BAIRRO_TOP10 bloqueia candidatos bons só por já
    haver 2 do mesmo bairro no Top10. Em vez de escondê-los, listam-se aqui (até 5, por
    score) pra quem for visitar o bairro poder considerar mais de uma esquina."""
    outros = ranking_completo[
        (ranking_completo["bairro"] == bairro) & (~ranking_completo["candidato_id"].isin(ids_top10))
    ].sort_values("score_final", ascending=False).head(5)
    if outros.empty:
        return ""
    linhas = "".join(
        f"<tr><td>{html.escape(str(o['candidato_id']))}</td><td>{_fmt(o['score_final'], 3)}</td>"
        f"<td>{_motivo_fora_do_top10(o)}</td></tr>"
        for _, o in outros.iterrows()
    )
    return f"""
      <h4>Outros pontos no mesmo bairro (fora do Top10 pelo cap de {MAX_POR_BAIRRO_TOP10}/bairro, dedup ou score)</h4>
      <table class="tabela-ficha"><tr><th>Candidato</th><th>Score</th><th>Por que não está no Top10</th></tr>{linhas}</table>
    """


def _ficha_finalista(i: int, row, mini_mapa_html: str, concorrentes: gpd.GeoDataFrame, crs_metrico: str,
                      ranking_completo: pd.DataFrame | None = None, ids_top10: set | None = None) -> str:
    dist = concorrentes.geometry.distance(row.geometry) / 1000.0
    proximos = concorrentes.assign(_dist=dist).nsmallest(5, "_dist")
    linhas_prox = "".join(
        f"<tr><td>{html.escape(str(c['nome']) or '(sem nome)')}</td><td>{NOME_TIPO.get(c['tipo'], c['tipo'])}</td>"
        f"<td>{c['_dist']:.2f} km</td><td>{_fmt(c['avaliacoes'])}</td></tr>"
        for _, c in proximos.iterrows()
    )
    outros_bairro_html = (
        _outros_pontos_bairro(row["bairro"], ranking_completo, ids_top10)
        if ranking_completo is not None and ids_top10 is not None else ""
    )
    checklist_html = ""
    for secao, itens in CHECKLIST:
        itens_html = "".join(f'<li><label><input type="checkbox"> {html.escape(item)}</label></li>' for item in itens)
        checklist_html += f"<h4>{html.escape(secao)}</h4><ul class='checklist'>{itens_html}</ul>"

    motivo_teste = html.escape(str(row.get("teste_absoluto_motivo", "")))
    return f"""
    <section class="ficha" id="ficha-{row['candidato_id']}">
      <h3>#{i} — {html.escape(str(row['bairro']))} <span class="badge">{row['candidato_id']}</span></h3>
      <div class="ficha-grid">
        <div class="ficha-mapa">{_iframe_srcdoc(mini_mapa_html, 320)}</div>
        <div class="ficha-indicadores">
          <table class="tabela-ficha">
            <tr><th>Score final</th><td>{_fmt(row['score_final'], 3)}</td></tr>
            <tr><th>Domicílios (captação efetiva)</th><td>{_fmt(row['domicilios_efetivo'])}</td></tr>
            <tr><th>% apartamento (captação efetiva)</th><td>{_fmt(row['pct_apartamento_efetivo']*100 if pd.notna(row['pct_apartamento_efetivo']) else None, 1, sufixo='%')}</td></tr>
            <tr><th>Renda média do responsável (captação efetiva)</th><td>{_fmt(row['renda_media_efetivo'], 0, 'R$ ')}</td></tr>
            <tr><th>Potencial mensal da área</th><td>{_fmt(row['potencial_mensal'], 0, 'R$ ')}</td></tr>
            <tr><th>Concorrentes (15min)</th><td>{_fmt(row['n_concorrentes_15min'])}</td></tr>
            <tr><th>Força da concorrência</th><td>{_fmt(row['forca_concorrencia'], 1)}</td></tr>
            <tr><th>Clínicas veterinárias sem loja</th><td>{_fmt(row['n_clinicas_sem_loja'])}</td></tr>
            <tr><th>Saturação</th><td>{_fmt(row['saturacao'], 1)}</td></tr>
            <tr><th>Demanda capturada (modelo de Huff)</th><td>{_fmt(row.get('demanda_capturada'), 0, 'R$ ')}/mês</td></tr>
            <tr><th>Aluguel estimado da região{' <span class="badge" title="sem dado real em data/manual/imoveis.csv — usado o fallback negocio.teto_aluguel">estimado</span>' if row.get('aluguel_e_estimado') else ''}</th><td>{_fmt(row.get('aluguel_estimado_regiao'), 0, 'R$ ')}</td></tr>
            <tr><th>Custo fixo mensal (aluguel + custo extra)</th><td>{_fmt(row.get('custo_fixo_mensal'), 0, 'R$ ')}</td></tr>
            <tr><th>Sacos de ração p/ break-even</th><td>{_fmt(row.get('sacos_breakeven'), 1)}</td></tr>
            <tr><th>Imóveis no teto (data/manual/imoveis.csv)</th><td>{_fmt(row.get('n_imoveis_no_teto'))}</td></tr>
            <tr><th>R$/m² médio</th><td>{_fmt(row.get('reais_m2_medio'), 2, 'R$ ')}</td></tr>
            <tr><th>Teste absoluto</th><td>{'✅ passou' if row['teste_absoluto_passou'] else '❌ reprovado'} — {motivo_teste}</td></tr>
          </table>
        </div>
      </div>
      <h4>5 concorrentes mais próximos</h4>
      <table class="tabela-ficha"><tr><th>Nome</th><th>Tipo</th><th>Distância</th><th>Avaliações</th></tr>{linhas_prox}</table>
      {outros_bairro_html}
      <div class="checklist-container">
        <h4>Checklist de visita (imprimir)</h4>
        {checklist_html}
      </div>
    </section>
    """


# ---------------------------------------------------------------------------

def run() -> Path:
    cfg = load_config()
    crs_metrico = cfg["crs"]["metrico"]
    LOGGER.info("=== Fase 9: relatório HTML ===")

    dados = _carregar_tudo(cfg)
    top10 = dados["top10"]

    mapa = _construir_mapa(dados, crs_metrico)
    mapa_html_embutido = _tornar_offline(mapa.get_root().render())
    mapa_path = OUTPUT_DIR / "mapa.html"
    mapa_path.write_text(mapa_html_embutido, encoding="utf-8")
    LOGGER.info("gravado: %s (recursos externos inlinados — funciona sem internet)", mapa_path)

    OUTPUT_FICHAS.mkdir(parents=True, exist_ok=True)
    isocronas_por_id = {row["candidato_id"]: row.geometry for _, row in dados["isocronas"].iterrows()} if not dados["isocronas"].empty else {}
    ids_top10 = set(top10["candidato_id"])
    fichas_html = []
    for i, row in enumerate(top10.itertuples(name="Row"), start=1):
        row_dict = top10.iloc[i - 1]
        mini_mapa = _mini_mapa_finalista(row, dados["concorrentes"], isocronas_por_id.get(row.candidato_id), crs_metrico)
        fichas_html.append(_ficha_finalista(
            i, row_dict, mini_mapa, dados["concorrentes"], crs_metrico,
            ranking_completo=dados["ranking_completo"], ids_top10=ids_top10,
        ))
    LOGGER.info("fichas individuais geradas: %d", len(fichas_html))

    linhas_top10 = "".join(_linha_tabela(top10.iloc[i]) for i in range(len(top10)))

    reprovados = dados["reprovados"]
    if reprovados.empty:
        reprovados_html = (
            "<p><i>Nenhum candidato foi reprovado no teste absoluto nesta rodada — "
            "mas isso é otimista demais pra confiar de olhos fechados: o custo fixo usado no teste "
            "ainda não inclui aluguel real (data/manual/imoveis.csv está vazio). "
            "Rode a Fase 7 de novo depois de preencher os imóveis pra um teste absoluto que valha a pena.</i></p>"
        )
    else:
        linhas_rep = "".join(
            f"<tr><td>{r['candidato_id']}</td><td>{html.escape(str(r['bairro']))}</td>"
            f"<td>{_fmt(r['score_final'], 3)}</td><td>{html.escape(str(r['teste_absoluto_motivo']))}</td></tr>"
            for _, r in reprovados.sort_values("score_final", ascending=False).head(30).iterrows()
        )
        reprovados_html = f"<table class='tabela-ficha'><tr><th>Candidato</th><th>Bairro</th><th>Score</th><th>Motivo</th></tr>{linhas_rep}</table>"

    pesos = dados["pesos"]
    metodologia_html = f"""
    <p><b>Método de calibração:</b> {html.escape(pesos['metodo'])} — confiança <b>{html.escape(pesos['confianca'])}</b>
    (n={pesos['n_amostra']}{f", R²={pesos['detalhes_estatisticos'].get('r2'):.3f}" if 'r2' in pesos.get('detalhes_estatisticos', {}) else ''}).</p>
    <p>{html.escape(pesos['nota_metodologica'])}</p>
    <p><b>Pesos finais dos eixos do score:</b></p>
    <ul>{''.join(f"<li>{html.escape(k)}: {v*100:.1f}%</li>" for k, v in pesos['pesos_eixos_score_final'].items())}</ul>
    <p><b>Fontes de dados</b> (baixadas em {date.today().isoformat()}):</p>
    <ul>{''.join(f"<li>{html.escape(nome)} — <code>{html.escape(url)}</code></li>" for nome, url in FONTES_METODOLOGIA)}</ul>
    """

    limitacoes = dados["limitacoes"]
    if not limitacoes:
        limitacoes_html = "<p>Nenhuma limitação registrada nas fases automáticas.</p>"
    else:
        limitacoes_html = "<ul>" + "".join(f"<li>{html.escape(l)}</li>" for l in limitacoes) + "</ul>"
    limitacoes_html += (
        "<p><b>Entradas manuais ainda não preenchidas</b> (spec §5 — nenhuma é obrigatória pro Top 10, "
        "todas enriquecem quando existirem): ifood.csv, meta_publico.csv, google_buscas.csv, "
        "campo_fluxo.csv, cliente_oculto.csv, marcas_carteira.csv, geoteste.csv, imoveis.csv "
        "(ver data/manual/ e PLANO_ESCOLHA_DO_PONTO.md Bloco F/G para como coletar cada uma).</p>"
    )

    html_final = _montar_html(
        mapa_html_embutido=mapa_html_embutido,
        resumo_html=_resumo_executivo(top10),
        linhas_top10=linhas_top10,
        fichas_html="".join(fichas_html),
        metodologia_html=metodologia_html,
        limitacoes_html=limitacoes_html,
        reprovados_html=reprovados_html,
        n_reprovados=len(reprovados),
    )

    relatorio_path = OUTPUT_DIR / "relatorio.html"
    relatorio_path.write_text(html_final, encoding="utf-8")
    LOGGER.info("gravado: %s (%.1f MB)", relatorio_path, relatorio_path.stat().st_size / 1e6)

    log_resumo_fase(LOGGER, entrada=len(top10), saida=len(top10), descartados=0)
    return relatorio_path


def _montar_html(**kw) -> str:
    hoje = date.today().strftime("%d/%m/%Y")
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Escolha de ponto — loja pet, Uberlândia/MG</title>
<style>
{_CSS}
</style>
</head>
<body>
<header class="topo">
  <h1>Escolha de ponto comercial — loja pet, Uberlândia/MG</h1>
  <p class="subtitulo">Relatório gerado em {hoje} · pipeline automatizado a partir de dados públicos + Google Places + OpenRouteService</p>
  <nav class="indice">
    <a href="#resumo">Resumo executivo</a>
    <a href="#top10">Top 10</a>
    <a href="#mapa">Mapa</a>
    <a href="#fichas">Fichas individuais</a>
    <a href="#metodologia">Metodologia</a>
    <a href="#limitacoes">Limitações</a>
    <a href="#reprovados">Reprovados no teste absoluto</a>
  </nav>
</header>

<main>
  <section id="resumo">
    <h2>1. Resumo executivo</h2>
    {kw['resumo_html']}
  </section>

  <section id="top10">
    <h2>2. Top 10 — ranqueado por score</h2>
    <div class="tabela-scroll">
    <table id="tabela-top10" class="ordenavel">
      <thead><tr>
        <th data-tipo="texto">Bairro / eixo</th>
        <th data-tipo="num">Domicílios (captação efetiva)</th>
        <th data-tipo="num">% apto</th>
        <th data-tipo="num">Renda resp.</th>
        <th data-tipo="num">Concorrentes</th>
        <th data-tipo="num">Força concorrência</th>
        <th data-tipo="num">Clínicas s/ loja</th>
        <th data-tipo="num">Saturação</th>
        <th data-tipo="num">Potencial mensal</th>
        <th data-tipo="num">Demanda capturada</th>
        <th data-tipo="num">Aluguel estimado</th>
        <th data-tipo="num">Imóveis no teto</th>
        <th data-tipo="num">R$/m² médio</th>
        <th data-tipo="num">Score</th>
      </tr></thead>
      <tbody>{kw['linhas_top10']}</tbody>
    </table>
    </div>
    <p class="nota-tabela">Clique no cabeçalho pra ordenar por qualquer coluna. "não coletado" = dado que depende de entrada manual ainda não preenchida (ver Limitações).</p>
  </section>

  <section id="mapa">
    <h2>Mapa interativo</h2>
    <p>Sem tiles de internet de propósito (spec: relatório funciona offline) — o contorno cinza tracejado é o perímetro urbano, e os polígonos coloridos são os setores censitários. Ligue/desligue camadas no controle do canto superior direito.</p>
    <div class="mapa-container">{_iframe_srcdoc(kw['mapa_html_embutido'], 640)}</div>
  </section>

  <section id="fichas">
    <h2>3. Fichas individuais dos finalistas</h2>
    {kw['fichas_html']}
  </section>

  <section id="metodologia">
    <h2>4. Metodologia</h2>
    {kw['metodologia_html']}
  </section>

  <section id="limitacoes">
    <h2>5. Limitações</h2>
    {kw['limitacoes_html']}
  </section>

  <section id="reprovados">
    <h2>6. Reprovados no teste absoluto ({kw['n_reprovados']})</h2>
    {kw['reprovados_html']}
  </section>
</main>

<footer><p>Gerado automaticamente. Ver ESPEC_CLAUDE_CODE.md e PLANO_ESCOLHA_DO_PONTO.md no repositório para o método completo.</p></footer>

<script>
{_JS}
</script>
</body>
</html>"""


_CSS = """
:root {
  --bg: #ffffff; --fg: #1a1a1a; --fg-muted: #6b6b6b; --linha: #e2e2e2;
  --acento: #2c3e50; --acento-2: #2980b9; --sucesso: #27ae60; --alerta: #c0392b;
  --fundo-secao: #f7f7f8;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #14171a; --fg: #e8e8e8; --fg-muted: #9a9a9a; --linha: #2c2f33; --fundo-secao: #1c1f22; }
}
* { box-sizing: border-box; }
body { background: var(--bg); color: var(--fg); font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 0; line-height: 1.5; }
header.topo { padding: 24px 20px; border-bottom: 1px solid var(--linha); }
h1 { margin: 0 0 4px; font-size: 1.6rem; }
.subtitulo { color: var(--fg-muted); margin: 0 0 14px; font-size: .9rem; }
nav.indice { display: flex; flex-wrap: wrap; gap: 6px 16px; }
nav.indice a { color: var(--acento-2); text-decoration: none; font-size: .9rem; }
nav.indice a:hover { text-decoration: underline; }
main { max-width: 1100px; margin: 0 auto; padding: 8px 20px 60px; }
section { padding: 28px 0; border-bottom: 1px solid var(--linha); }
h2 { font-size: 1.3rem; border-left: 4px solid var(--acento); padding-left: 10px; }
h3 { font-size: 1.1rem; }
h4 { font-size: .95rem; color: var(--fg-muted); margin-bottom: 6px; }
.badge { font-size: .75rem; background: var(--fundo-secao); border: 1px solid var(--linha); padding: 2px 8px; border-radius: 10px; margin-left: 8px; }
.tabela-scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: .88rem; }
table.tabela-ficha { margin: 10px 0 20px; }
th, td { padding: 7px 10px; border-bottom: 1px solid var(--linha); text-align: left; white-space: nowrap; }
th { background: var(--fundo-secao); cursor: pointer; user-select: none; }
th:hover { color: var(--acento-2); }
tbody tr:hover { background: var(--fundo-secao); }
.nc { color: var(--fg-muted); font-style: italic; }
.nota-tabela { color: var(--fg-muted); font-size: .82rem; }
.mapa-container { border: 1px solid var(--linha); border-radius: 6px; overflow: hidden; }
.mapa-container iframe { width: 100%; height: 640px; border: 0; display: block; }
.ficha { padding: 18px 0; border-top: 1px dashed var(--linha); }
.ficha-grid { display: grid; grid-template-columns: 440px 1fr; gap: 16px; align-items: start; }
.ficha-mapa iframe { border: 1px solid var(--linha); border-radius: 6px; }
@media (max-width: 800px) { .ficha-grid { grid-template-columns: 1fr; } }
ul.checklist { list-style: none; padding-left: 4px; }
ul.checklist li { margin: 4px 0; }
.checklist-container { background: var(--fundo-secao); border: 1px solid var(--linha); border-radius: 6px; padding: 12px 16px; margin-top: 10px; }
footer { text-align: center; color: var(--fg-muted); font-size: .8rem; padding: 20px; }
@media print {
  nav.indice, .mapa-container { display: none; }
  section { break-inside: avoid; border-bottom: none; }
  .ficha { break-before: page; }
  body { font-size: 11pt; }
}
"""

_JS = """
document.querySelectorAll('table.ordenavel th').forEach(function(th, idx) {
  th.addEventListener('click', function() {
    var table = th.closest('table');
    var tbody = table.querySelector('tbody');
    var linhas = Array.from(tbody.querySelectorAll('tr'));
    var tipo = th.getAttribute('data-tipo');
    var asc = th.getAttribute('data-asc') !== 'true';
    linhas.sort(function(a, b) {
      var ca = a.children[idx], cb = b.children[idx];
      var va = ca.getAttribute('data-v'); var vb = cb.getAttribute('data-v');
      if (tipo === 'num') {
        va = va === null || va === 'nan' || va === '' ? -Infinity : parseFloat(va);
        vb = vb === null || vb === 'nan' || vb === '' ? -Infinity : parseFloat(vb);
        return asc ? va - vb : vb - va;
      }
      va = ca.textContent.trim(); vb = cb.textContent.trim();
      return asc ? va.localeCompare(vb) : vb.localeCompare(va);
    });
    table.querySelectorAll('th').forEach(function(h) { h.removeAttribute('data-asc'); });
    th.setAttribute('data-asc', asc);
    linhas.forEach(function(l) { tbody.appendChild(l); });
  });
});
"""


if __name__ == "__main__":
    run()
