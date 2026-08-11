# ESPECIFICAÇÃO PARA O CLAUDE CODE
## Pipeline de escolha de ponto comercial — loja pet, Uberlândia/MG

Documento de execução. O agente deve seguir as fases na ordem, respeitando os critérios de aceite de cada uma.
Documento de referência (contexto de negócio): `PLANO_ESCOLHA_DO_PONTO.md` — deve estar na raiz do repositório.

---

# 0. OBJETIVO

Produzir, de forma autônoma, um relatório **HTML autocontido** que apresente as **10 microrregiões candidatas** de Uberlândia/MG para abertura de uma loja de varejo pet, ranqueadas por um score calibrado com dados locais, com mapa interativo, fichas individuais e checklist de visita imprimível.

**Entregável final:** `output/relatorio.html` — abre no navegador sem servidor, sem internet, sem dependências externas.

---

# 1. REGRAS DE EXECUÇÃO

1. **Idempotência.** Rodar duas vezes não refaz downloads nem recalcula o que já está em cache. Todo download vai para `data/raw/` e é verificado por existência + tamanho antes de baixar de novo.
2. **Fases isoladas.** Cada fase é um script independente em `pipeline/`, executável sozinho, que lê de `data/` e escreve em `data/`. Nenhuma fase depende de variável em memória de outra.
3. **Degradação graciosa.** Se uma entrada manual (seção 5) não existir, a fase correspondente é pulada com aviso no log e o relatório final marca aquele indicador como "não coletado". **O pipeline nunca aborta por falta de dado manual.**
4. **Log estruturado.** Tudo em `logs/pipeline.log` com timestamp, fase, ação e contagem de registros. Ao final de cada fase, imprimir um resumo: quantas linhas entraram, quantas saíram, quantas foram descartadas e por quê.
5. **Sem dado inventado.** Se uma fonte falhar, registrar a falha e seguir. **Nunca preencher com estimativa silenciosa.** Todo número no relatório final precisa ter origem rastreável.
6. **Commit por fase.** Ao concluir cada fase com sucesso, commitar com mensagem `fase N: <descrição> — <n> registros`.

---

# 2. AMBIENTE

```bash
python -m venv .venv && source .venv/bin/activate

pip install \
  pandas geopandas shapely pyproj fiona rtree \
  requests folium branca jinja2 \
  statsmodels scikit-learn \
  openpyxl chardet tqdm python-dotenv \
  'markitdown[all]'
```

**markitdown** (https://github.com/microsoft/markitdown) é a ferramenta padrão de leitura de qualquer arquivo não-tabular do projeto. Sempre que o pipeline baixar ou receber um PDF, DOCX, XLSX ou PPTX, converter para Markdown antes de ler:

```bash
markitdown data/raw/<arquivo> -o data/interim/md/<arquivo>.md
```

Uso obrigatório em:
- dicionário de dados do IBGE (`.xlsx`) → para o agente descobrir os nomes reais das variáveis em vez de adivinhar
- mapa e lei de zoneamento de Uberlândia (`.pdf`) → para extrair a lista de zonas e os usos permitidos
- qualquer documento que o usuário largar em `data/inbox/`

---

# 3. ESTRUTURA DE PASTAS

```
projeto-ponto-pet/
├── CLAUDE.md
├── PLANO_ESCOLHA_DO_PONTO.md
├── config.yaml
├── .env                      # chaves de API (não versionar)
├── data/
│   ├── inbox/                # usuário larga arquivos aqui
│   ├── raw/                  # downloads originais, nunca editados
│   ├── interim/
│   │   └── md/               # saídas do markitdown
│   ├── manual/               # CSVs preenchidos à mão (seção 5)
│   └── processed/            # camadas prontas (.gpkg / .parquet)
├── pipeline/
│   ├── f1_ibge.py
│   ├── f2_concorrentes.py
│   ├── f3_vias_zoneamento.py
│   ├── f4_candidatos.py
│   ├── f5_isocronas.py
│   ├── f6_calibracao.py
│   ├── f7_score.py
│   ├── f8_manuais.py
│   └── f9_relatorio.py
├── output/
│   ├── relatorio.html
│   ├── mapa.html
│   ├── top10.csv
│   └── fichas/
└── logs/
```

---

# 4. CONFIGURAÇÃO

`config.yaml`:

```yaml
municipio:
  nome: "Uberlândia"
  uf: "MG"
  codigo_ibge: "3170206"

crs:
  geografico: "EPSG:4674"     # SIRGAS 2000
  metrico: "EPSG:31982"       # SIRGAS 2000 / UTM 22S — validar contra a extensão do município

negocio:
  teto_aluguel: 4000
  limite_aluguel_com_carencia: 5000
  custo_fixo_extra_mensal: 1500
  margem_por_saco_premium: 35        # PROVISÓRIO — substituir pelos dados dos representantes
  taxa_posse_pet_domicilio: 0.55     # ajustar com fonte Abinpet/IBGE
  gasto_medio_mensal_por_pet: 120    # ajustar
  captura_min: 0.05
  captura_max: 0.15
  multiplo_minimo_breakeven: 2.5

isocronas:
  minutos: [5, 10, 15]
  modo: "driving-car"

concorrencia:
  raio_bloqueio_rede_m: 1500
  peso_rede: 3.0
  peso_independente: 1.0

redes_conhecidas:
  - {nome: "Petz Rondon Pacheco", endereco: "Av. Rondon Pacheco, 505"}
  - {nome: "Petz Center Shopping", endereco: "Av. João Naves de Ávila, 1331"}
  - {nome: "Cobasi Rondon Pacheco", endereco: "Av. Rondon Pacheco, 1001"}
```

`.env`:
```
ORS_API_KEY=...              # openrouteservice.org — gratuito
GOOGLE_PLACES_API_KEY=...    # opcional; sem ele o pipeline usa só OSM
```

---

# 5. ENTRADAS MANUAIS

O agente cria os templates vazios com cabeçalho na primeira execução e avisa no log quais estão pendentes.

| Arquivo | Preenchido por | Conteúdo |
|---|---|---|
| `data/manual/imoveis.csv` | usuário | `bairro,endereco,lat,lon,area_m2,aluguel,tem_deposito,tem_vaga_carga,fonte,link` |
| `data/manual/ifood.csv` | usuário | `regiao,loja,avaliacoes,tempo_entrega_min,pedido_minimo` |
| `data/manual/meta_publico.csv` | usuário | `candidato_id,lat,lon,publico_estimado` |
| `data/manual/google_buscas.csv` | usuário | `candidato_id,palavra,volume_mensal` |
| `data/manual/campo_fluxo.csv` | usuário | `candidato_id,data,horario,pedestres_15min,carros_15min` |
| `data/manual/cliente_oculto.csv` | usuário | `loja,regiao,preco_golden_15kg,preco_premier_15kg,tem_nexgard,preco_nexgard,clientes_15min,funcionarios,tem_banho_tosa,prateleira_acessorio_1a5,destaque_sacos_pequenos` |
| `data/manual/geoteste.csv` | usuário | `regiao,verba,conversas,custo_por_conversa` |
| `data/manual/marcas_carteira.csv` | usuário | `marca,regiao_bloqueada,contato,observacao` |

**Nenhuma destas é obrigatória para gerar o Top 10.** Todas enriquecem o relatório quando presentes.

---

# 6. FASES

## FASE 1 — Base demográfica (IBGE)

**Fonte:**
```
https://ftp.ibge.gov.br/Censos/Censo_Demografico_2022/Agregados_por_Setores_Censitarios/
├── malha_com_atributos/                    → geometria dos setores, já com atributos
├── Agregados_por_Setor_csv/
│   ├── Agregados_por_setores_basico_BR_*.zip
│   ├── Agregados_por_setores_caracteristicas_domicilio1_BR.zip
│   ├── Agregados_por_setores_caracteristicas_domicilio2_BR_*.zip
│   ├── Agregados_por_setores_caracteristicas_domicilio3_BR_*.zip
│   ├── Agregados_por_setores_demografia_BR.zip
│   └── dicionario_de_dados_agregados_por_setores_censitarios_*.xlsx
```

**Passos:**
1. Listar o diretório do FTP via HTTP e **pegar os arquivos com a data mais recente** — os nomes têm sufixo de data e mudam. Não hardcodar nome de arquivo.
2. Baixar com barra de progresso, salvar em `data/raw/ibge/`.
3. Converter o dicionário `.xlsx` com markitdown e **ler o Markdown para descobrir os códigos reais das variáveis**. Não adivinhar nome de coluna.
4. Extrair e filtrar por `CD_MUN == "3170206"`.
5. Unir malha + tabelas por `CD_SETOR`.

**Armadilhas conhecidas — tratar explicitamente:**
- `CD_SETOR` e `CD_MUN` são **strings com zeros à esquerda**. Ler com `dtype=str`. Se virar inteiro, o join falha silenciosamente.
- CSVs do IBGE vêm em **latin-1/cp1252**, separador `;`, decimal `,`. Detectar com `chardet` e não confiar em UTF-8.
- Valores especiais (`X`, `-`, vazio) significam dado suprimido por sigilo, **não zero**. Converter para `NaN`.
- A malha vem em EPSG:4674. **Reprojetar para o CRS métrico antes de qualquer cálculo de distância ou área.**

**Variáveis a extrair (localizar os códigos no dicionário):**
- domicílios particulares permanentes ocupados
- moradores
- domicílios do tipo casa
- domicílios do tipo apartamento
- rendimento médio mensal do responsável pelo domicílio
- distribuição por faixa etária

**Saída:** `data/processed/setores.gpkg`

**Aceite:** número de setores compatível com Uberlândia; soma de domicílios coerente com a população municipal; nenhum `CD_SETOR` nulo; `% apartamento` entre 0 e 1 em todas as linhas.

---

## FASE 2 — Concorrência

**2.1 OpenStreetMap (sempre)** — Overpass API:

```
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
```
Implementar retry com backoff exponencial — o Overpass devolve 429 com frequência. Cachear a resposta crua em `data/raw/osm/`.

**2.2 Google Places (se houver chave)** — Text Search em grade sobre a mancha urbana, termos: `pet shop`, `agropecuária`, `casa de ração`, `clínica veterinária`, `banho e tosa`.
Campos a guardar: `place_id`, nome, endereço, lat, lon, `rating`, `user_ratings_total`, `business_status`.
Deduplicar por `place_id` e depois por proximidade (< 30 m) + similaridade de nome.

**2.3 Classificação.** Cada concorrente recebe:
- `tipo`: pet_shop | agropecuaria | veterinaria | rede
- `vende_racao`: booleano — para veterinária, inferir do nome e das categorias; quando indeterminado, marcar `NULL` e listar no relatório para verificação manual
- `forca = (avaliacoes / max(distancia_km, 0.15)) * peso_tipo` — calculado depois, por candidato

**2.4 Redes.** Geocodificar os três endereços de `config.yaml` e marcar `tipo=rede`.

**Saída:** `data/processed/concorrentes.gpkg`

**Aceite:** as três lojas de rede presentes e com coordenada plausível; nenhum ponto fora do polígono municipal; contagem por tipo registrada no log.

---

## FASE 3 — Vias e zoneamento

**3.1 Malha viária** — Overpass, `highway` em `primary|secondary|tertiary|residential`, com `oneway` e `lanes`.

**3.2 Zoneamento:**
- Baixar `https://docs.uberlandia.mg.gov.br/wp-content/uploads/2024/06/Mapa-Zoneamento-e-Ocupacao-do-Solo-2024-FINAL.pdf`
- Converter com markitdown → `data/interim/md/zoneamento.md`
- Extrair a legenda de zonas e os usos permitidos, gravando em `data/processed/zonas_permitidas.csv`
- **Verificar antes** se a Prefeitura publica shapefile ou GeoJSON do zoneamento no catálogo de dados abertos (`https://www.uberlandia.mg.gov.br/portal-da-transparencia/dados-abertos/catalogo-de-dados-abertos/`) e em Mapas e Bairros (`https://www.uberlandia.mg.gov.br/prefeitura/secretarias/planejamento-urbano/mapas-e-bairros/`, que oferece PDF e DWG). Se houver vetorial, usar; PDF é o plano B.
- Se só houver PDF ou DWG não conversível, **registrar como limitação no relatório** e marcar a verificação de zoneamento como pendência manual por candidato. Não inventar polígono de zona.

**3.3 Perímetro urbano** — usar o limite municipal do IBGE combinado com densidade de vias para excluir zona rural.

**Saída:** `data/processed/vias.gpkg`, `data/processed/zonas.gpkg` (quando existir vetorial)

---

## FASE 4 — Geração de candidatos

Não usar pontos escolhidos à mão. Gerar e deixar o computador ranquear.

**Passos:**
1. Selecionar vias de classe `secondary`, `tertiary` e `residential` de maior conectividade dentro do perímetro urbano.
2. Gerar pontos a cada **400 m** ao longo dessas vias → esperado alguns milhares.
3. Descartar pontos a menos de `raio_bloqueio_rede_m` de qualquer rede.
4. Descartar pontos em zona incompatível (quando houver camada vetorial).
5. Descartar pontos em setores sem domicílio ou com renda do responsável abaixo do piso configurado.
6. Calcular um score preliminar barato (buffer euclidiano de 1,5 km) e manter os **300 melhores** — só eles vão para a fase de isócronas, que é cara.

**Saída:** `data/processed/candidatos.gpkg`

**Aceite:** ao menos 150 candidatos sobreviventes distribuídos por mais de 8 bairros distintos. Se sobrar menos que isso, afrouxar filtros e registrar no log.

---

## FASE 5 — Isócronas

**Fonte:** OpenRouteService, endpoint de isócronas, `driving-car`, intervalos de 5/10/15 min.

**Cuidados:**
- O plano gratuito tem limite diário e limite de locations por requisição. **Enviar em lotes, respeitar o rate limit, implementar retry com backoff e cachear cada resposta por hash das coordenadas** em `data/raw/isocronas/`. O pipeline precisa poder ser interrompido e retomado sem perder o que já baixou.
- Se a cota estourar, salvar o progresso, avisar no log quantos faltam e encerrar a fase com status parcial.
- Alternativa se a cota for insuficiente: subir OSRM local com o extrato do Sudeste do Geofabrik.

**Cálculo:** para cada candidato, interseção da isócrona de 10 min com os setores censitários, com **rateio proporcional à área** para setores parcialmente cobertos (não contar o setor inteiro se só metade está dentro).

**Saída:** `data/processed/candidatos_com_demanda.gpkg`

---

## FASE 6 — Calibração por análogos

Esta fase substitui pesos arbitrários por pesos derivados dos dados de Uberlândia.

**Passos:**
1. Para cada concorrente **pet shop independente** com `user_ratings_total` conhecido, calcular as variáveis do entorno de 1 km: domicílios, `% apartamento`, renda do responsável, densidade, nº de concorrentes, nº de clínicas veterinárias sem loja.
2. Variável dependente: `log(1 + avaliações)`.
3. Ajustar OLS com `statsmodels`. Registrar R², p-valores e VIF.
4. **Se n < 25 ou R² < 0,25:** não usar a regressão. Cair no método de quartis — comparar a média de cada variável no quartil superior de avaliações contra o inferior — e usar as diferenças normalizadas como pesos.
5. Gravar os pesos finais e o método usado em `data/processed/pesos.json`.

**Regra:** o relatório final **deve declarar** qual método foi usado e com que confiança. Peso derivado de regressão fraca apresentado como se fosse sólido é pior que peso assumido.

---

## FASE 7 — Score e filtros

**7.1 Demanda estimada por candidato:**
```
domicilios_10min
  × taxa_posse_pet_domicilio
  × fator_verticalizacao        # 1,0 + 0,4 × (% apartamento)
  × gasto_medio_mensal_por_pet
  = potencial_mensal_R$
```

**7.2 Força da concorrência:** somar, para cada concorrente dentro da isócrona de 15 min:
```
forca = (avaliacoes / max(dist_km, 0.15)) × peso_tipo
```

**7.3 Saturação:** `potencial_mensal / soma_forcas`

**7.4 Ponto de equilíbrio:**
```
custo_fixo = aluguel_estimado_regiao + custo_fixo_extra_mensal
sacos_breakeven = custo_fixo / margem_por_saco_premium
```
`aluguel_estimado_regiao` sai da mediana de `imoveis.csv` do bairro; se não houver dado, marcar `NULL` e sinalizar no relatório.

**7.5 Teste absoluto (filtro duro):**
```
potencial_mensal × captura_min  ≥  multiplo_minimo_breakeven × custo_fixo
```
Quem não passa é excluído do Top 10, **independente do score**, e listado numa seção separada "reprovados no teste absoluto" com o motivo.

**7.6 Score final:** combinação normalizada por percentil dos eixos, com os pesos da Fase 6:
- demanda estimada
- saturação
- oferta de imóvel dentro do teto (de `imoveis.csv`; ausente = penalidade explícita, não zero silencioso)
- acesso e parada rápida (hierarquia da via, mão dupla, ausência de canteiro central)

**7.7 Deduplicação geográfica:** clusterizar candidatos a menos de 800 m entre si e manter apenas o melhor de cada cluster. Sem isso, o Top 10 vira dez pontos da mesma avenida.

**Saída:** `output/top10.csv` e `data/processed/ranking_completo.parquet`

---

## FASE 8 — Incorporação das entradas manuais

Ler tudo que existir em `data/manual/`, casar por `candidato_id` ou por bairro, e anexar como colunas extras. Cada indicador ausente vira `"não coletado"` no relatório — nunca zero, nunca estimativa.

Quando `geoteste.csv` existir, ele **reordena os 5 primeiros** por custo por conversa, porque é o único dado medido em vez de estimado. Deixar isso explícito no relatório.

---

## FASE 9 — Relatório HTML

**`output/mapa.html`** — Folium, camadas ligáveis:
- coropleto de demanda por setor
- coropleto de `% apartamento`
- concorrentes (círculo proporcional a avaliações; vermelho = rede, laranja = pet shop, azul = veterinária)
- isócronas de 10 min dos 10 finalistas
- marcadores numerados dos finalistas com popup contendo os principais indicadores
- imóveis de `imoveis.csv`, quando houver

**`output/relatorio.html`** — autocontido (CSS e JS embutidos, mapa via iframe do `mapa.html` ou merge do HTML do Folium), com:

1. **Resumo executivo** — os 3 melhores em uma frase cada, e a recomendação de qual visitar primeiro.
2. **Tabela Top 10**, ordenável por coluna: bairro/eixo, domicílios 10min, % apto, renda do responsável, nº de concorrentes, força da concorrência, clínicas sem loja, saturação, potencial mensal, imóveis no teto, R$/m² médio, score.
3. **Ficha individual** de cada finalista: mapa recortado, indicadores, os 5 concorrentes mais próximos com distância e avaliações, e o **checklist de visita imprimível** (Parte 6 do `PLANO_ESCOLHA_DO_PONTO.md`).
4. **Metodologia** — pesos usados, método de calibração, R² quando aplicável, e cada fonte com data de download.
5. **Limitações** — seção obrigatória, listando toda camada faltante, toda entrada manual não preenchida e todo filtro que não pôde ser aplicado.
6. **Reprovados no teste absoluto** — candidatos com bom score que não passaram, com o motivo.

**Requisitos:** abre com duplo clique, funciona offline, imprime bem em A4, legível no celular.

---

# 7. O QUE NÃO AUTOMATIZAR

O agente **não deve tentar**, e deve marcar como pendência manual:

| Tarefa | Motivo |
|---|---|
| Scraping do Google Maps | Viola os termos. Usar Places API ou OSM |
| Scraping de portais imobiliários | Frágil e juridicamente cinzento. Usar `imoveis.csv` preenchido à mão |
| Estimador de público do Meta | Exige login. Coleta manual → `meta_publico.csv` |
| Planejador de palavras do Google Ads | Exige conta ativa. Coleta manual → `google_buscas.csv` |
| Dados do iFood | Sem API pública. Coleta manual → `ifood.csv` |
| Contagem de fluxo, cliente oculto, pesquisa de origem | Trabalho de campo |
| Ligação para representantes de ração | Trabalho humano, e é o dado que mais destrava o modelo |

---

# 8. CRITÉRIOS DE ACEITE DO PIPELINE

Antes de declarar a execução concluída, o agente valida:

- [ ] `output/relatorio.html` abre no navegador sem servidor e sem erro de console
- [ ] Top 10 com exatamente 10 linhas, sem duplicata geográfica a menos de 800 m
- [ ] Nenhum candidato do Top 10 a menos de 1,5 km de rede
- [ ] Todo número do relatório rastreável a uma fonte listada na seção Metodologia
- [ ] Seção Limitações preenchida com o que faltou (se nada faltou, dizer isso explicitamente)
- [ ] `logs/pipeline.log` sem exceção não tratada
- [ ] Rodar o pipeline uma segunda vez não redownloada nada e produz resultado idêntico
- [ ] `output/top10.csv` abre corretamente no Excel (UTF-8 com BOM, separador `;`)

---

# 9. CLAUDE.md SUGERIDO

```markdown
# Projeto: escolha de ponto comercial — loja pet, Uberlândia/MG

Ler `ESPEC_CLAUDE_CODE.md` antes de qualquer coisa.
Contexto de negócio em `PLANO_ESCOLHA_DO_PONTO.md`.

## Regras
- Executar as fases em ordem; cada uma é um script independente em `pipeline/`.
- Nunca refazer download já presente em `data/raw/`.
- Nunca inventar dado. Fonte que falha vira registro em log e linha na seção Limitações.
- Códigos de setor censitário são STRING com zeros à esquerda.
- CSVs do IBGE: latin-1, separador `;`, decimal `,`. Detectar encoding, não presumir.
- Reprojetar para EPSG:31982 antes de qualquer cálculo métrico.
- Usar markitdown para todo PDF/XLSX/DOCX antes de tentar ler.
- Commitar ao fim de cada fase.

## Comandos
- `python pipeline/f1_ibge.py` … `python pipeline/f9_relatorio.py`
- `make all` roda tudo em ordem
- `make clean-processed` limpa derivados sem apagar `data/raw/`
```

---

# 10. ORDEM SUGERIDA DE TRABALHO

1. Fases 1 a 4 — não dependem de chave de API nenhuma. Rodar primeiro e conferir o resultado.
2. Obter a chave gratuita do OpenRouteService e rodar a Fase 5.
3. Fases 6 e 7 — o Top 10 nasce aqui.
4. Gerar o relatório (Fase 9) mesmo sem nenhuma entrada manual. O primeiro HTML já serve para decidir onde ir olhar.
5. Preencher os CSVs manuais ao longo das semanas seguintes e **rodar de novo**. O relatório melhora a cada rodada, sem retrabalho.
