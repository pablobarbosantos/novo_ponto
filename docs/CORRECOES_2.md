# CORREÇÕES 2 — vitalidade comercial, conhecimento local e realismo de entrega

Aplicar **depois** de `CORRECOES.md`. Origem: revisão do Top 10 pelo dono do negócio, que conhece a cidade.

Três defeitos estruturais foram apontados, e nenhum deles é bug de código — são variáveis que faltam no modelo.

---

# C1 — O modelo não enxerga bairro em decadência comercial

**Observado:** o Top 10 trouxe Centro e Presidente Roosevelt. Segundo o conhecimento local, são regiões onde o comércio fecha, e os poucos que resistem estão mal.

**Causa:** o modelo mede quem **mora** ali (IBGE) e quem **concorre** ali (Places/OSM). Não mede se o comércio ali está vivo. Bairro pode ter domicílio e renda razoáveis e ainda assim ser cemitério de loja — por perfil de fluxo, por segurança, por estacionamento, por deslocamento do eixo comercial da cidade.

**Correção — criar o eixo "vitalidade comercial", com três indicadores mensuráveis:**

### C1.1 Taxa de mortalidade empresarial (Receita Federal)

Base de CNPJs em `https://dadosabertos.rfb.gov.br`, campos `situacao_cadastral` e `data_situacao_cadastral`.

```python
# por bairro (agregar por CEP → bairro)
baixadas_36m = CNPJs com situacao_cadastral == 08 (baixada)
               e data_situacao_cadastral nos últimos 36 meses
ativas       = CNPJs com situacao_cadastral == 02

taxa_mortalidade = baixadas_36m / (ativas + baixadas_36m)
idade_media_ativas = média de (hoje - data_inicio_atividade) das ativas
```

Filtrar por CNAEs de **comércio varejista em geral** (divisão 47), não só pet — o que interessa é a saúde do comércio de rua do bairro.

Leitura: taxa de mortalidade alta = bairro que mata loja. Idade média das ativas muito alta com poucas aberturas recentes = bairro estagnado, sem renovação.

### C1.2 Estabelecimentos permanentemente fechados (Google Places)

A Places API retorna `business_status`. Contar `CLOSED_PERMANENTLY` por região e calcular:

```python
taxa_fechamento_maps = fechados_permanentemente / (operacionais + fechados_permanentemente)
```

Este é o indicador mais direto de "vitrine vazia" que existe em dado aberto.

### C1.3 Recência das avaliações

Para os estabelecimentos ativos no raio, pegar a data da avaliação mais recente. Se a mediana das últimas avaliações do entorno for antiga (mais de 6 meses), o comércio local tem pouco movimento.

### C1.4 Uso no score

Novo eixo `vitalidade_comercial`, peso **15%**, retirado proporcionalmente de `demanda_estimada` e `saturacao`.

E um **filtro duro**: candidato cujo bairro esteja no quartil superior de taxa de mortalidade empresarial é eliminado, não apenas penalizado.

**Validação:** rodar os indicadores contra Centro e Presidente Roosevelt. Se eles **não** ficarem entre os piores da cidade, o indicador não está capturando o fenômeno — investigar antes de confiar nele.

---

# C2 — Conhecimento local como camada explícita

Conhecimento de quem opera na cidade há anos é dado, não palpite. Mas precisa ficar **declarado e auditável**, nunca embutido no código.

Acrescentar em `config.yaml`:

```yaml
conhecimento_local:
  bairros_excluidos:
    - nome: "Centro"
      motivo: "comércio de rua em decadência; fluxo diurno de passagem, não residencial; estacionamento difícil"
    - nome: "Presidente Roosevelt"
      motivo: "região onde comércio fecha; remanescentes com desempenho fraco"

  bairros_penalizados:
    - nome: "Santa Mônica"
      fator: 0.5
      motivo: "uma das maiores concentrações de concorrência do setor na cidade; proximidade de Petz e Cobasi; alta presença de moradia estudantil (UFU) infla % apartamento sem demanda pet correspondente"
```

Aplicar após o score, antes do corte do Top 10. **Toda exclusão e penalização precisa aparecer numa seção própria do relatório**, com o motivo declarado — para que a decisão seja rastreável e possa ser revista depois.

---

# C3 — Moradia estudantil infla o percentual de apartamento

**Observado:** Santa Mônica pontuou alto por renda e verticalização. É o bairro do campus da UFU. República e apartamento de estudante contam como domicílio em apartamento, mas não são domicílio com pet nem comprador de ração premium.

**Correção — detectar e descontar:**

Indicadores disponíveis no Censo por setor:
- alta proporção de moradores de 18 a 24 anos
- alta proporção de domicílios de 1 ou 2 moradores **combinada** com renda do responsável baixa em bairro de renda alta
- proximidade de campus universitário (adicionar polígonos da UFU — Santa Mônica, Umuarama, Educação Física — via OSM `amenity=university`)

```python
suspeita_estudantil = (pct_18a24 > percentil_75_cidade) & (dist_campus_km < 2)
fator_verticalizacao = fator_verticalizacao × 0.6  onde suspeita_estudantil
```

Registrar no log quantos setores foram descontados, para poder conferir se o critério pegou o alvo certo.

---

# C4 — Realismo de entrega: tempo de mapa ≠ tempo de porta

**Observado:** isócrona de 10 min gerando áreas de 90 a 112 km². Além do problema já tratado em `CORRECOES.md` (B1), o tempo de deslocamento puro ignora a operação real de entrega:

- portaria de prédio, interfone, espera e elevador
- cliente que quer conversar
- trânsito de pico, que não está no perfil médio do roteador
- blitz, obra, via interditada
- tempo de retorno do entregador (a entrega ocupa ida **e** volta)

**Correção — separar dois conceitos que hoje estão misturados:**

### C4.1 Captação de balcão

É a que decide o ponto. Alcance curto, comportamento de conveniência.

```yaml
captacao_balcao:
  anel_primario_min: 5      # peso 1,00
  anel_secundario_min: 10   # peso 0,25
  descartar_acima_de_min: 10
```
**O anel de 15 minutos sai do cálculo de captação.** Ninguém atravessa a cidade para comprar ração.

### C4.2 Viabilidade de entrega

É outra conta, e serve para dimensionar o raio operacional, não para escolher o ponto.

```python
tempo_porta_a_porta = tempo_deslocamento × 2.5 + 6   # min; ida, volta e fricção de prédio
raio_entrega_viavel = maior distância onde tempo_porta_a_porta ≤ 25 min
```

Com esse ajuste, um deslocamento de 6 minutos no mapa vira 21 minutos reais de operação. **O raio de entrega útil cai para algo em torno de 3 a 4 km**, não os 8 km da premissa original.

Adicionar à ficha de cada candidato: quantos domicílios ficam dentro do raio de entrega viável, e o custo estimado por entrega (`tempo_porta_a_porta × custo/hora do entregador`) — para comparar com a margem do pedido médio e definir o ticket mínimo de frete grátis.

---

# C5 — Endurecer o filtro de rede

**Observado:** o candidato de Centro passou com 1.709 m de distância de rede. O filtro de 1.500 m é permissivo demais para concorrente que trabalha a mesma faixa de produto com escala de compra muito maior.

```yaml
concorrencia:
  raio_bloqueio_rede_m: 3000        # era 1500
  percentil_maximo_forca: 0.70      # elimina candidatos no quartil superior de força de concorrência
```

O segundo parâmetro é o que teria barrado Santa Mônica automaticamente, sem precisar de conhecimento local.

---

# C6 — Novo teste de sanidade obrigatório

Antes de gerar o relatório, o pipeline deve rodar e registrar no log:

1. **Diversidade:** o Top 10 contém pelo menos 6 bairros distintos.
2. **Exclusões:** nenhum candidato pertence a `bairros_excluidos`.
3. **Vitalidade:** nenhum candidato está no quartil superior de mortalidade empresarial.
4. **Rede:** nenhum candidato a menos de 3 km de Petz ou Cobasi.
5. **Captação plausível:** `domicilios_efetivos` mediano abaixo de 10% do total municipal.
6. **Teste absoluto discrimina:** a taxa de aprovação está entre 20% e 80% dos candidatos. Fora dessa faixa, o teste está mal parametrizado — 100% de aprovação foi exatamente o defeito da primeira execução.

Falhando qualquer um, **abortar a geração do relatório** e escrever o motivo no log. Relatório bonito com resultado errado é pior que ausência de relatório.

---

# ORDEM DE APLICAÇÃO

1. C4.1 (captação curta — muda tudo o mais)
2. C5 (filtros de rede e força)
3. C1 (vitalidade comercial — é a coleta mais trabalhosa, mas é o que faltava de verdade)
4. C3 (desconto estudantil)
5. C2 (conhecimento local declarado)
6. C4.2 (raio de entrega, para a ficha)
7. C6 (testes de sanidade)
8. Regerar e comparar com o Top 10 anterior

**Expectativa:** o novo Top 10 não deve ter Centro nem Presidente Roosevelt, deve ter no máximo um candidato em Santa Mônica, e deve trazer bairros que ainda não apareceram. Se vier parecido com o anterior, alguma correção não pegou.
