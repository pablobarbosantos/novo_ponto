# MANUAL DE ESCOLHA DO PONTO — Loja pet, Uberlândia/MG

**Objetivo único:** chegar a uma lista de **10 microrregiões candidatas**, ranqueadas, com justificativa numérica, para você sair a campo procurar imóvel.

**Premissa:** loja nova, do zero, clientela nova. O que se vende hoje não define o que se venderá lá. O alvo é **mix de margem** — ração premium, antipulga e medicamento (Nexgard, Simparic), acessório e petisco — para público de classe média em bairro adensado.

**Cidade:** Uberlândia/MG — código IBGE **3170206**, UF **MG**.

---

# PARTE 1 — AS CAMADAS DE INFORMAÇÃO

Cada camada tem: **o que é**, **onde pegar**, **por que serve pro seu caso** e **como usar**.

---

## BLOCO A — DEMANDA (quem mora lá e quanto pode gastar)

### Camada 1 — Malha de setores censitários com atributos

**O que é:** o polígono de cada setor censitário de Uberlândia (a menor divisão territorial do IBGE, ~300 domicílios cada), já com os dados demográficos embutidos no arquivo.

**Onde:**
```
https://ftp.ibge.gov.br/Censos/Censo_Demografico_2022/Agregados_por_Setores_Censitarios/malha_com_atributos/
```
Baixe o recorte de MG em `.gpkg` ou `.shp` e filtre pelo código do município 3170206.

**Por que serve:** é a base geográfica de tudo. Sem ela você tem números sem lugar. Setor censitário é granular o bastante para separar um lado da avenida do outro — que é exatamente a escala de decisão de um pet shop de bairro.

**Como usar:** carregue em GeoPandas ou QGIS, filtre Uberlândia, e todas as camadas seguintes se penduram nela por `CD_SETOR`.

---

### Camada 2 — Domicílios e população por setor

**O que é:** contagem de domicílios permanentes ocupados e de moradores por setor.

**Onde:**
```
https://ftp.ibge.gov.br/Censos/Censo_Demografico_2022/Agregados_por_Setores_Censitarios/Agregados_por_Setor_csv/
→ Agregados_por_setores_basico_BR_20260520.zip
```
Dicionário de campos (baixe junto, é indispensável):
```
dicionario_de_dados_agregados_por_setores_censitarios_20260520.xlsx
```

**Por que serve:** domicílio é a unidade de consumo pet, não pessoa. Uma casa com 4 moradores e 1 cachorro consome como uma casa com 2 moradores e 1 cachorro. **Conte domicílios, não gente** — esse é o erro mais comum de quem faz esse estudo.

**Como usar:** é o multiplicador principal da estimativa de demanda (Parte 3).

---

### Camada 3 — Tipo de domicílio: casa x apartamento

**O que é:** quantos domicílios de cada setor são apartamento e quantos são casa.

**Onde:**
```
Agregados_por_setores_caracteristicas_domicilio1_BR.zip
Agregados_por_setores_caracteristicas_domicilio2_BR_20250417.zip
Agregados_por_setores_caracteristicas_domicilio3_BR_20250417.zip
```

**Por que serve — esta é a camada mais importante do estudo inteiro para o seu mix.** Apartamento significa:
- cão de porte pequeno → saco de 1kg, 2,5kg e 3kg, que tem **margem por quilo muito maior** que saco de 15kg
- cão que não tem quintal → mais acessório, mais tapete higiênico, mais petisco, mais brinquedo
- cão que passeia na rua → **muito mais antipulga e vermífugo**, que é a linha de melhor margem
- dono que não tem carro grande nem quer carregar peso → compra menor e mais frequente, o que aumenta visitas por mês
- gato, que é o pet que mais cresce em apartamento e cuja ração tem margem melhor que a de cão

Densidade de casa com quintal indica o oposto: cão grande, saco de 15/20kg, compra mensal, sensibilidade a preço. É o cliente que você está deixando para trás.

**Como usar:** calcule `% apartamento = domicílios em apartamento ÷ domicílios totais` por setor. Esse percentual entra como **multiplicador de valor por domicílio**, não só como contagem.

---

### Camada 4 — Renda do responsável pelo domicílio

**O que é:** rendimento médio mensal do responsável, em salários mínimos, por setor censitário.

**Onde:** dentro do pacote `basico` (variável de renda do responsável, identificada no dicionário de dados). No Censo 2022 esse dado foi divulgado em 30/04/2025.

**Por que serve:** separa quem compra ração de R$ 100 de quem compra ração de R$ 45.

**Cuidado importante:** o Censo 2022 **não** publicou renda domiciliar total por setor, como fazia o Censo 2010 — publicou apenas a renda do *responsável*. Em domicílio com dois assalariados isso subestima o poder de compra, e é justamente o perfil que você quer (casal jovem, apartamento, cachorro pequeno). **Não use renda como eixo pesado do score.** Use como filtro: elimine setores com renda do responsável abaixo de ~2 salários mínimos e não dê mais peso que isso.

---

### Camada 5 — Faixa etária dos moradores

**O que é:** distribuição de idade por setor.

**Onde:** `Agregados_por_setores_demografia_BR.zip`

**Por que serve:** dois perfis compram pet premium com força — adulto de 25-40 anos sem filho pequeno (o "pet como filho" de que você falou) e idoso de classe média (companhia, e costuma ser cliente fiel e recorrente). Setor com muita criança pequena e renda média tende a espremer o orçamento pet.

**Como usar:** ajuste fino, peso baixo. Não invente precisão aqui.

---

## BLOCO B — CONCORRÊNCIA (quem já está lá e quão forte é)

### Camada 6 — Pet shops e agropecuárias georreferenciados

**O que é:** todos os concorrentes de Uberlândia com coordenada, nota e número de avaliações.

**Onde — opção gratuita (OpenStreetMap via Overpass):** acesse `https://overpass-turbo.eu` e rode:

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
Exporte em GeoJSON. Grátis, mas a cobertura do OSM no Brasil é incompleta — use como ponto de partida.

**Onde — opção boa (Google Places API):** crie um projeto no Google Cloud, ative a Places API, e faça buscas de texto em grade sobre a cidade ("pet shop", "agropecuária", "clínica veterinária", "casa de ração"). Retorna nome, endereço, **latitude/longitude, nota e número de avaliações**. Tem crédito mensal gratuito — confira o pricing atual, mas para uma cidade é custo baixo. **Pague a API; não faça scraping do Google Maps.**

**Por que serve:** concorrente é as duas coisas ao mesmo tempo — penalidade *e* prova de que existe demanda. Bairro sem nenhum pet shop geralmente não é oportunidade, é aviso.

**Como usar:** ver Camada 7, que é onde a mágica acontece.

---

### Camada 7 — Número de avaliações como proxy de faturamento

**O que é:** a técnica de **análise de análogos**, que é o que a consultoria de geomarketing faz de mais valioso — adaptada ao que você consegue de graça.

**Por que serve:** você não tem faturamento de lojas comparáveis. Mas número de avaliações no Google e no iFood é proxy razoável de movimento. Uma loja com 600 avaliações e outra com 25 não estão no mesmo patamar.

**Como usar — esta é a etapa que calibra o modelo inteiro:**

1. Levante todos os pet shops de Uberlândia com nº de avaliações e coordenada.
2. Para cada loja, calcule as variáveis do setor censitário dela e do entorno de 1 km: domicílios, % apartamento, renda do responsável, densidade.
3. Rode uma regressão simples (`avaliações ~ domicílios + %apartamento + renda + concorrentes_proximos`).
4. **Os coeficientes que saírem são os pesos do seu score.**

Resultado: os pesos param de ser chute meu e passam a ser o que de fato explica o sucesso das lojas que já operam em Uberlândia. Se aparecer que `% apartamento` explica muito mais que renda, o modelo passa a priorizar verticalização — e você descobre isso com dado local, não com teoria.

---

### Camada 8 — Pet shops no iFood

**O que é:** quais concorrentes já operam delivery, em que região, e com quantas avaliações.

**Onde:** o próprio app/site do iFood, mudando o endereço de entrega para pontos diferentes da cidade e vendo quem aparece.

**Por que serve:** avaliação no iFood é venda que aconteceu. É o termômetro mais direto de demanda pet por delivery, é grátis, e quase ninguém olha. Também mostra **onde o delivery pet ainda não chegou** — que pode ser sua janela.

**Como usar:** anote por região: quantas lojas atendem, quantas avaliações cada uma, tempo de entrega, ticket mínimo.

---

### Camada 9 — As redes (bloqueio de zona)

**O que é:** localização de Petz, Cobasi e outras redes.

**Situação conhecida em Uberlândia:** Petz na Av. Rondon Pacheco, 505; Petz no Center Shopping (Av. João Naves de Ávila, 1331); Cobasi na Av. Rondon Pacheco, 1001. Petz e Cobasi se fundiram e o grupo está em ciclo de expansão.

**Por que serve:** competir de frente com rede na mesma faixa de produto, com seu porte e seu colchão, não é estratégia. E o eixo Rondon Pacheco é exatamente onde o dado de renda vai apontar — é a armadilha do estudo.

**Como usar:** filtro duro. Raio de 1,5 km ao redor de cada uma sai da lista. **Confira também se há loja nova anunciada** — busque no site das redes e em notícias locais antes de fechar contrato.

---

### Camada 10 — Clínicas veterinárias sem loja

**O que é:** veterinários que atendem mas não vendem ração e medicamento.

**Onde:** a mesma consulta da Camada 6 (`amenity=veterinary`), depois verificando no Google Maps quais têm loja.

**Por que serve — este é um sinal que quase ninguém usa.** Clínica veterinária concentra exatamente o seu público-alvo: gente que gasta com pet. Se ela **não vende** ração e medicamento, ela é:
1. prova de demanda qualificada no raio;
2. **parceiro em potencial** — o veterinário receita, você vende, e às vezes indica.

Um bairro com 3 clínicas e nenhum pet shop estruturado é um sinal muito mais forte que um bairro com renda alta e nenhuma clínica.

---

## BLOCO C — ACESSO (quem consegue chegar)

### Camada 11 — Isócronas de deslocamento

**O que é:** a área que se alcança em 5, 10 e 15 minutos de carro a partir de um ponto — em vez de um círculo de raio fixo.

**Onde:** `https://openrouteservice.org` — cadastro gratuito, chave de API, endpoint de isócronas. Alternativa: rodar OSRM local com o extrato do Brasil do Geofabrik.

**Por que serve:** raio de 8 km é ficção. Rio, ferrovia, avenida sem retorno e sentido único fazem 2 km virarem 12 minutos. O cliente de compra rápida (10-15 min de permanência) não atravessa a cidade. Isócrona mede o que importa: **tempo até você.**

**Como usar:** gere isócronas de 10 minutos para cada ponto candidato e some os domicílios dos setores que caem dentro. Esse é o denominador de toda a estimativa de demanda.

---

### Camada 12 — Malha viária e hierarquia de vias

**O que é:** quais ruas são arteriais, coletoras e locais; sentido, mão dupla, canteiro central.

**Onde:** OpenStreetMap (`highway=primary/secondary/tertiary`) via Overpass ou QGIS com plugin QuickOSM. Complementar: mapa do sistema viário da Prefeitura (Seplan).

**Por que serve:** avenida de trânsito rápido com canteiro central é péssima para varejo de conveniência — ninguém para. Via coletora de bairro, mão dupla, com possibilidade de encostar o carro por 10 minutos, é o que você quer. **Fachada boa em avenida onde ninguém consegue parar vale menos que fachada média em rua onde dá pra encostar.**

E lembre do atacado: a rua precisa aguentar caminhão para carga e descarga.

---

## BLOCO D — RESTRIÇÕES LEGAIS (o que elimina antes de visitar)

### Camada 13 — Zoneamento e uso do solo

**O que é:** o que pode funcionar em cada zona da cidade.

**Onde:**
- Mapa de Zoneamento e Ocupação do Solo 2024 (PDF):
  `https://docs.uberlandia.mg.gov.br/wp-content/uploads/2024/06/Mapa-Zoneamento-e-Ocupacao-do-Solo-2024-FINAL.pdf`
- Texto da Lei Complementar de Zoneamento, Uso e Ocupação do Solo:
  `https://leismunicipais.com.br/plano-de-zoneamento-uso-e-ocupacao-do-solo-uberlandia-mg`
- Mapas e bairros oficiais (Seplan), incluindo mapa do sistema viário e de loteamentos, em PDF e DWG:
  `https://www.uberlandia.mg.gov.br/prefeitura/secretarias/planejamento-urbano/mapas-e-bairros/`
- Catálogo de dados abertos da Prefeitura:
  `https://www.uberlandia.mg.gov.br/portal-da-transparencia/dados-abertos/catalogo-de-dados-abertos/`
- Dados geoeconômicos da Secretaria de Desenvolvimento Econômico:
  `https://www.uberlandia.mg.gov.br/prefeitura/secretarias/desenvolvimento-economico-e-inovacao/dados-geoeconomicos/`

**Por que serve:** imóvel lindo em zona que não permite comércio de médio porte, ou que não permite depósito, ou que proíbe banho e tosa por ruído, é imóvel que você não pode alugar. Descobrir isso depois de assinar é o pior erro possível.

**Como usar:** antes de visitar qualquer imóvel, confira a zona. E **confirme na Prefeitura** o uso pretendido — inclusive depósito para o atacado e possibilidade futura de banho e tosa.

---

### Camada 14 — CNPJs ativos por CNAE (validação cruzada)

**O que é:** base pública da Receita Federal com todas as empresas, CNAE principal e secundário, endereço e situação cadastral.

**Onde:** `https://dadosabertos.rfb.gov.br` (arquivos mensais, pesados).

**CNAEs relevantes — não use só o 4789-0/04:**
- 4789-0/04 — comércio varejista de animais vivos e de artigos e alimentos para animais de estimação
- 4623-1/09 — comércio atacadista de alimentos para animais
- 4693-1/00 — comércio atacadista de mercadorias em geral
- 7500-1/00 — atividades veterinárias
- 9609-2/08 — higiene e embelezamento de animais domésticos
- 4771-7/04 — comércio varejista de medicamentos veterinários

**Por que serve:** pega concorrente que o Google não mostra e revela a densidade de empresas do setor por bairro.

**Cuidado:** a base **não tem latitude/longitude**, o endereço é texto sujo, e "situação ativa" não significa que está operando — MEI fantasma infla a contagem e rebaixa injustamente bairros que você deveria olhar. **Use como complemento, nunca como fonte principal.** O validador de presença física é o Google Places.

---

## BLOCO E — OFERTA DE IMÓVEL (a camada que decide sobrevivência)

### Camada 15 — Aluguel comercial real por região

**O que é:** quantos imóveis comerciais existem, de que tamanho, e a que preço, em cada bairro candidato.

**Onde:** VivaReal, ZAP, OLX, Imovelweb, QuintoAndar, e **as imobiliárias locais de Uberlândia** — que costumam ter estoque que não aparece nos portais. Filtro: loja ou salão comercial, aluguel até seu teto.

**Por que serve:** de nada adianta a melhor região do mapa se não existe imóvel lá dentro do seu teto de aluguel, com depósito e vaga de carga. Essa camada não sai de dado público de jeito nenhum — sai de garimpo.

**Como usar:** monte uma planilha com bairro, endereço, m², valor pedido, se tem depósito, se tem vaga. Calcule o **R$/m² médio por bairro** e a **quantidade de opções disponíveis**. Bairro com boa demanda e zero oferta de imóvel dentro do teto sai da lista, por melhor que pontue.

---

## BLOCO F — DEMANDA MEDIDA (a camada que ninguém faz)

Aqui é onde você sai da estimativa e entra na medição. É a parte mais valiosa do plano.

### Camada 16 — Estimador de público do Meta Ads

**O que é:** o Gerenciador de Anúncios mostra o **tamanho estimado do público** para uma segmentação, antes de você publicar ou gastar.

**Como fazer:** crie uma campanha (sem publicar), defina localização por **raio de 1 km** sobre cada ponto candidato, adicione interesses (cães, gatos, animais de estimação, pet shop) e anote o número de pessoas alcançáveis. Repita para os 10 candidatos.

**Por que serve:** grátis, rápido, e comparável entre regiões.

**Limitações honestas:** raio mínimo de 1 km; "interesse em cães" é quem interage com conteúdo de cachorro, o que superestima quem realmente tem pet. Serve para **ranquear regiões**, não para dimensionar mercado. O antigo Audience Insights, que dava perfil demográfico detalhado, foi descontinuado — o estimador dentro do Gerenciador é o que restou.

---

### Camada 17 — Biblioteca de Anúncios do Meta

**O que é:** repositório público de todos os anúncios ativos no Facebook e Instagram. Sem login, sem custo.

**Como fazer:** busque pelos nomes dos pet shops de Uberlândia e por termos gerais.

**Por que serve:**
- mostra **quais concorrentes anunciam** e há quanto tempo — anúncio no ar há meses é anúncio que dá retorno;
- mostra **a oferta que eles usam** (frete grátis, desconto na primeira compra, banho e tosa), ou seja, o que já foi testado e funciona nesse mercado;
- mostra qual região eles miram, pelo texto do anúncio.

Leia todos antes de escrever o seu primeiro anúncio.

---

### Camada 18 — Volume de busca por raio (Google Ads)

**O que é:** o Planejador de Palavras-chave do Google Ads permite segmentar por **raio ao redor de um ponto** e devolve previsão de volume de busca.

**Palavras a testar:** *pet shop perto de mim*, *ração premium*, *nexgard*, *simparic*, *banho e tosa*, *ração para gato castrado*, *ração golden*, *ração premier*.

**Por que serve:** intenção de compra é o sinal mais forte que existe. Quem digita "nexgard" vai comprar nexgard.

**Aviso técnico:** **o Google Trends não desce abaixo de município** no Brasil — não adianta tentar bairro por lá. O Planejador com raio é o caminho, e mesmo ele às vezes se recusa a devolver dado em raio pequeno.

---

### Camada 19 — Campanha geo-segmentada de teste (a mais valiosa de todas)

**O que é:** medir demanda real gastando pouco, antes de assinar qualquer contrato.

**Como fazer:**
1. Escolha as 4 ou 5 melhores regiões do ranking.
2. Crie uma campanha por região, **mesmo anúncio, mesma verba, mesma oferta** — muda só a segmentação geográfica.
3. Verba: R$ 60 a 100 por região, rodando 10 a 14 dias.
4. Destino: WhatsApp.
5. Meça por região: **custo por conversa iniciada** e quantas conversas viraram intenção real de compra.

**Por que serve:** todas as outras camadas *estimam* demanda. Esta *mede*. E o dinheiro não é perdido — cada conversa é um cliente que já sabe que você existe no dia em que a loja abrir, o que encurta a rampa.

**Como usar no score:** o custo por conversa é o desempate final entre os 3 finalistas. Se uma região custa R$ 4 por conversa e outra R$ 15, o mapa perde a discussão para o dado real.

---

## BLOCO G — CAMPO (o que só existe indo lá)

### Camada 20 — Contagem de fluxo padronizada

**Como fazer:** em cada ponto finalista, **três janelas de 15 minutos**: terça 9h, terça 18h, sábado 10h. Conte pedestres e carros **separadamente**, sempre do mesmo jeito, para poder comparar.

**Por que serve:** é primário, é chato, e é o dado mais confiável que você vai ter. Também é o que consultoria cobra caro para fazer.

---

### Camada 21 — Cliente oculto nos concorrentes

**Como fazer:** visite 12 a 15 pet shops das regiões candidatas. Compre algo pequeno. Anote:

| O que anotar | Por que importa |
|---|---|
| Preço de Golden e Premier 15kg | Define sua faixa de preço viável |
| Tem Nexgard/Simparic na prateleira? Preço? | Diz se a região consome medicamento caro |
| Quantos clientes entraram nos 15 min | Movimento real |
| Quantos funcionários | Porte da operação |
| Tem banho e tosa? Cheio? | Se a recorrência já está tomada |
| Prateleira de acessório: cheia ou vazia? | Onde está a margem deles |
| Tamanho dos sacos em destaque (1kg/3kg vs 15kg) | Confirma o perfil apartamento x casa |

Isso é literalmente o que a consultoria entrega. Ninguém faz porque dá trabalho.

---

### Camada 22 — Pesquisa de origem na porta do concorrente

**Como fazer:** fique 30 minutos na saída de um pet shop movimentado e pergunte a quem sai: "mora aqui perto? veio de carro ou a pé?".

**Por que serve:** revela o **raio real de captação** de uma loja como a que você quer abrir, nessa cidade. É o número que corrige todas as suas isócronas.

---

### Camada 23 — Apoio local barato

- **SEBRAE-MG:** consultoria de pesquisa de mercado a custo simbólico para pequeno negócio. Não entrega geomarketing, mas entrega metodologia e às vezes mão de obra.
- **Empresas júnior da UFU** (Administração, Economia, Geografia): contagem de fluxo e pesquisa de campo é exatamente o tipo de projeto que pegam, a preço de estudante. Resolve o trabalho braçal que você não tem tempo de fazer.
- **Cognatis:** faz avaliação de ponto comercial avulsa, por estudo, com base própria (Geopop), avaliando perfil da região, fluxo de pedestre e tráfego. Pedir orçamento custa um e-mail. Se vier na faixa de alguns milhares, compare com semanas do seu tempo — você é o gargalo da operação.

**Ressalva:** o "potencial de consumo por quarteirão" que as plataformas de geomarketing vendem é **modelado, não medido** — sai de IBGE + POF + dados de cartão extrapolados. É melhor que sua estimativa, mas não é verdade revelada. Use como uma camada a mais, nunca como veredito.

---

# PARTE 2 — CALIBRAÇÃO (achar os pesos certos)

Não use pesos inventados. Derive-os dos dados de Uberlândia, com a Camada 7:

1. Monte a tabela: uma linha por pet shop da cidade, com nº de avaliações e as variáveis do entorno de 1 km (domicílios, % apartamento, renda do responsável, nº de concorrentes, nº de clínicas veterinárias).
2. Rode a regressão. Em Python: `statsmodels.OLS`.
3. Normalize os coeficientes significativos e use como pesos.
4. Se a amostra for pequena demais para regressão confiável, faça o simples: **compare a média das variáveis do quartil superior de avaliações contra o quartil inferior.** A diferença mostra o que importa.

**Ponto de partida provisório**, a ser substituído pelo resultado acima:

| Eixo | Peso inicial |
|---|---:|
| Demanda estimada (domicílios × % apartamento) | 40% |
| Saturação (demanda ÷ força da concorrência) | 25% |
| Oferta de imóvel dentro do teto | 20% |
| Acesso e possibilidade de parada rápida | 15% |

---

# PARTE 3 — O CÁLCULO

## 3.1 Demanda estimada por candidato

```
Domicílios na isócrona de 10 min
  × taxa de posse de cão ou gato (fonte: Abinpet / IBGE)
  × fator de verticalização (1,0 a 1,4 conforme % de apartamento)
  × gasto médio mensal por pet no mix alvo
  = potencial mensal da área (R$)
```

## 3.2 Força da concorrência

Some, para cada concorrente dentro da isócrona:

```
força = (avaliações do concorrente ÷ distância em km) × fator de rede
fator de rede = 3 para Petz/Cobasi, 1 para independente
```

## 3.3 Saturação

```
Saturação = potencial mensal da área ÷ soma das forças
```
Saturação alta = demanda mal atendida = oportunidade. Baixa = já tem quem atenda.

## 3.4 Teste absoluto (o que impede erro grave)

Um ranking sempre produz um primeiro colocado — inclusive numa cidade onde nenhuma região serve. Por isso, todo candidato precisa passar por:

```
potencial mensal da área × captura realista (5% a 15%)  ≥  2,5 × seu ponto de equilíbrio
```

Quem não passa sai da lista, independente da posição no ranking.

---

# PARTE 4 — FILTROS DUROS

Aplicar **antes** de pontuar. Eliminam sem discussão:

| Filtro | Critério |
|---|---|
| Distância de rede | Fora de 1,5 km de Petz e Cobasi |
| Zoneamento | Permite comércio + depósito no local |
| Oferta de imóvel | Existe pelo menos 1 opção dentro do teto de aluguel |
| Depósito e carga | Comporta o estoque do atacado e recebe caminhão |
| Banho e tosa futuro | 15-20 m² nos fundos, com água, ralo e ventilação possível |
| Parada rápida | Dá pra encostar o carro por 10 minutos |
| Marca premium | Pelo menos 2 marcas com carteira livre para a região |
| Teste absoluto | Passa no critério da seção 3.4 |

O filtro de marca vem das ligações aos representantes — pergunte sempre se há carteira ou exclusividade por área em Uberlândia e quais bairros já estão atendidos.

---

# PARTE 5 — A ENTREGA

Uma tabela, ordenada por score:

| # | Região / eixo | Domicílios 10min | % apto | Renda resp. (SM) | Concorrentes | Clínicas s/ loja | Saturação | Imóveis no teto | R$/m² médio | Score | Custo/conversa (teste) |
|---|---|---|---|---|---|---|---|---|---|---|---|

Mais um mapa em HTML (Folium) com 4 camadas ligáveis: demanda, concorrentes, isócronas dos candidatos, imóveis disponíveis. É esse mapa que você abre no celular quando estiver na rua.

---

# PARTE 6 — CHECKLIST DE VISITA

Leve impresso. Um por imóvel.

**Do imóvel**
- [ ] Metragem total e do salão de vendas
- [ ] Tem depósito? Quantos m²? Cabe estoque do atacado?
- [ ] Caminhão consegue encostar? Carga e descarga sem multa?
- [ ] Fundos com água e ralo (banho e tosa futuro)?
- [ ] Pé-direito, ventilação, umidade (ração estraga)
- [ ] Elétrica: quantas fases, quantos amperes
- [ ] Fachada: quantos metros de vitrine, permite letreiro
- [ ] Banheiro, copa
- [ ] Estado: o que a reforma vai custar

**Do contrato**
- [ ] Valor pedido e piso real do proprietário
- [ ] **Carência** — quantos meses (mínimo 2, ideal 3)
- [ ] Prazo, índice de reajuste
- [ ] Multa rescisória e garantia exigida
- [ ] Quem paga IPTU e taxas
- [ ] Autorização para reforma e letreiro

**Do entorno**
- [ ] Contagem de fluxo nos 3 horários
- [ ] Estacionamento na via: livre, rotativo, proibido?
- [ ] Vizinhos que puxam movimento: padaria, farmácia, supermercado, hortifruti, academia, salão
- [ ] Pet shop mais próximo: distância e tamanho
- [ ] Clínica veterinária mais próxima: vende ração?
- [ ] Iluminação e segurança à noite
- [ ] Sentido da via e facilidade de retorno

---

# PARTE 7 — ORDEM DE EXECUÇÃO

| Etapa | O que fazer | Tempo |
|---|---|---|
| **1** | Baixar IBGE (Camadas 1-5) e montar a base geográfica de Uberlândia | 1 dia |
| **2** | Levantar concorrentes (Camadas 6, 8, 9, 10) | 1 dia |
| **3** | Calibrar pesos por análise de análogos (Camada 7) | meio dia |
| **4** | Gerar isócronas para ~40 pontos candidatos (Camada 11) | meio dia |
| **5** | Aplicar filtros duros e calcular score → **Top 10** | meio dia |
| **6** | Meta Ads estimador + Biblioteca de Anúncios + Google Ads por raio (Camadas 16-18) | 1 dia |
| **7** | Garimpo de imóveis nos 10 bairros (Camada 15) | contínuo |
| **8** | Campo: fluxo, cliente oculto, pesquisa de origem (Camadas 20-22) nos **5 melhores** | 1 semana |
| **9** | Campanha geo-teste nas 4-5 finalistas (Camada 19) | 2 semanas, em paralelo |
| **10** | Decisão e negociação | — |

Etapas 1 a 5 são cerca de **3 dias de trabalho efetivo** e já entregam o Top 10. Da 6 em diante é refinamento — e a 9 é a que realmente decide.

**Em paralelo, desde já, sem depender de nada disto:** ligar para os representantes de ração premium e perguntar preço, pedido mínimo, bonificação de abertura e — principalmente — **se há carteira ou exclusividade por região em Uberlândia**. Marca bloqueada num bairro derruba o bairro, por melhor que ele pontue no mapa.
