# CORREÇÕES — auditoria da primeira execução

Aplicar na ordem. Cada bloco tem o defeito observado, a causa, a correção e como validar.

---

# BLOQUEADORES

## B1 — A área de captação está medindo metade da cidade

**Observado:** isócronas de 10 min com mediana de 90 km² (máx. 112,5). Candidato líder alcança 176.664 domicílios, sendo que Uberlândia inteira tem 267.966. Cobertura mediana dos 300 candidatos: **48% da cidade**.

**Consequência:** todas as variáveis discriminantes foram achatadas na média municipal. `pct_apartamento_10min` varia apenas de 0,293 a 0,364 quando por setor varia de 0 a 1. O ranking está ordenando ruído.

**Causa:** isócrona binária (dentro/fora) com tempo alto demais para varejo de conveniência. Um cliente não atravessa a cidade para comprar ração.

**Correção — substituir captação binária por decaimento por distância.**

As isócronas de 5, 10 e 15 min já foram baixadas. Usar os três anéis como tempo discretizado e aplicar peso decrescente:

```python
PESO_ANEL = {5: 1.00, 10: 0.35, 15: 0.10}   # decaimento aproximado exp(-t/4)

# anel_5  = isócrona 5
# anel_10 = isócrona 10 menos isócrona 5
# anel_15 = isócrona 15 menos isócrona 10

domicilios_efetivos_i = Σ_anéis (domicílios do anel × PESO_ANEL[anel])
```

Rateio por área continua valendo para setor parcialmente coberto.

Todas as variáveis de entorno (`pct_apartamento`, `renda_media`, densidade) passam a ser **médias ponderadas pelos mesmos pesos**, não médias simples da isócrona de 10.

**Renomear as colunas** de `*_10min` para `*_efetivo`, para não restar ambiguidade sobre o que foi calculado.

**Validação:** `domicilios_efetivos` mediano deve ficar abaixo de 15% do total municipal (< ~40.000). `pct_apartamento_efetivo` precisa ter desvio padrão pelo menos 3× maior que o atual. Se continuar plano, o problema é outro — investigar antes de seguir.

---

## B2 — Captura fixa de 5-15% substituída por participação esperada

**Observado:** `potencial_mensal` de R$ 8,3 a 13,2 milhões, com 230 a 300 concorrentes dentro do alcance. Aplicar "captura de 5%" sobre isso não tem sentido: a captura depende de quantos concorrentes disputam cada domicílio.

**Correção — modelo gravitacional (Huff), por anel:**

```python
# atratividade
A_nova   = 1.0                                  # loja nova, sem histórico
A_conc_k = 0.5 + 1.5 * (avaliacoes_k / max_avaliacoes_cidade)
A_rede   = A_conc * 3.0

# para cada setor j, com t = tempo do anel em que j cai:
P(j -> i) = (A_i / t_i²) / Σ_k (A_k / t_k²)

demanda_capturada_i = Σ_j domicilios_j × taxa_posse × gasto_medio × P(j→i)
```

Concorrente sem avaliação recebe `A = 0.5` e é contado — hoje há 29 a 36 deles por candidato e ignorá-los superestima a captura.

`demanda_capturada` substitui `potencial_mensal × captura_min` no teste absoluto.

**Validação:** somar `demanda_capturada` de todos os candidatos de um mesmo bairro não pode ultrapassar o potencial daquele bairro.

---

## B3 — Ponto de equilíbrio calculado sem aluguel

**Observado:** `custo_fixo_mensal = 1500`, `sacos_breakeven = 42,86`, `aluguel_estimado_regiao` vazio em todos. O teste absoluto comparou R$ 11 milhões contra R$ 3.750 e aprovou **100% dos candidatos** — a salvaguarda foi neutralizada.

**Correção:**

```python
aluguel = mediana_imoveis_do_bairro  or  config.negocio.teto_aluguel   # fallback 4000
custo_fixo = aluguel + custo_fixo_extra_mensal
```

Quando o fallback for usado, marcar `aluguel_e_estimado = True` e exibir a marcação na tabela do relatório e na ficha do candidato.

**Validação:** com fallback de R$ 4.000, `sacos_breakeven` deve dar ≈ 157. Se o teste absoluto continuar aprovando 100% dos candidatos **depois** de B1 e B2, registrar isso explicitamente no relatório — pode ser real, mas precisa ser dito, não escondido.

---

# GRAVES

## G1 — Viés no percentual de apartamento

**Observado:** 220 setores com `domicilios_apartamento` nulo tendo `domicilios_casa` e `domicilios_ocupados` preenchidos. Em **202 deles, casa = ocupados** — ou seja, o branco significa zero, não supressão por sigilo. Tratados como nulo, saíram da média e enviesaram o indicador para cima.

Efeito medido: média de `pct_apartamento` cai de **0,267 para 0,238** após correção. Referência municipal: 25,3% dos domicílios são apartamento.

**Correção:**

```python
falta_apto = apartamento.isna() & casa.notna() & ocupados.notna()
apartamento = apartamento.mask(falta_apto, (ocupados - casa).clip(lower=0))
# idem para o caso inverso (casa nula, apartamento preenchido)
```

Registrar no log quantas células foram preenchidas por diferença. Se `casa + apartamento` divergir de `ocupados` em mais de 5%, **não** preencher — aí é supressão de verdade. Manter nulo e contar.

**Validação:** `Σ apartamento / Σ ocupados` deve permanecer ≈ 25,3%.

---

## G2 — Top 10 concentrado em 5 bairros

**Observado:** Santa Mônica aparece 4 vezes, Presidente Roosevelt 3, Centro 1, Chácaras Tubalina 1, Luizote 1. A deduplicação de 800 m não resolve, porque bairro grande comporta vários pontos distantes entre si.

**Correção:** após a dedup geográfica, aplicar **máximo de 2 candidatos por bairro** no Top 10. Os excedentes vão para uma seção "outros pontos do mesmo bairro" na ficha daquele bairro.

**Motivo:** o entregável serve para visitar 10 lugares diferentes. Dez esquinas de cinco bairros não é isso.

---

## G3 — Verificar o sinal da concorrência no score

**Observado:** a calibração por quartis atribuiu peso **positivo** a `n_concorrentes` (0,179) e a `n_clinicas_sem_loja` (0,280).

Para clínicas isso é correto e desejado — clínica sem loja é demanda qualificada e parceiro em potencial. Para concorrentes, é o efeito "concorrente revela demanda", que é real, **mas não pode se somar duas vezes**: se a densidade de concorrentes eleva o eixo de demanda e ao mesmo tempo o eixo de saturação usa a força da concorrência como divisor, o sinal pode se anular ou até inverter.

**Correção:** escrever um teste unitário que fixe todas as variáveis e varie apenas o número de concorrentes. **O score final tem que cair monotonicamente.** Se não cair, corrigir a composição dos eixos.

---

# MENORES

## M1 — Renormalizar o score quando um eixo está ausente
`oferta_imovel_score = 0` para todos os 300 candidatos (falta `imoveis.csv`). Hoje isso zera 20% do peso de forma uniforme, o que distorce a escala do score final sem aviso. Renormalizar os pesos sobre os eixos efetivamente disponíveis e **declarar no relatório** quais eixos entraram e com que peso.

## M2 — Auditar os 90 setores sem domicílio
`domicilios_ocupados` nulo em 90 setores. Verificar se são setores rurais, industriais ou de fato suprimidos, e registrar a contagem por motivo no log.

## M3 — Confirmar a unidade da renda
`renda_media_responsavel` tem média 3.872 e máximo 8.050. Plausível como reais, mas conferir contra o dicionário de dados se a variável é valor em R$ ou em salários mínimos, e se é média do responsável (não soma domiciliar). Gravar a unidade no metadado da coluna e exibi-la no cabeçalho da tabela do relatório.

## M4 — Zoneamento
Confirmado como limitação legítima: o texto da lei não foi acessível automaticamente. Manter como está, mas **adicionar linha no checklist de visita de cada ficha**: "confirmar uso permitido na Prefeitura antes de assinar — inclui depósito e possibilidade futura de banho e tosa".

---

# O QUE JÁ ESTÁ CERTO — NÃO MEXER

- Ingestão do IBGE: 1.988 setores, 267.966 domicílios, 2,66 moradores por domicílio. Bate com Uberlândia.
- Encoding, `dtype=str` nos códigos de setor e reprojeção métrica: funcionaram.
- Arquivos de limitações das fases 3 e 8: exatamente o comportamento pedido.
- `pesos.json` com método, confiança e nota metodológica explicando que só 65% do peso foi calibrado localmente: é o padrão de honestidade que o projeto deve manter.
- Idempotência, log e commit por fase.

---

# ORDEM DE APLICAÇÃO

1. G1 (mais barato, corrige a base de tudo)
2. B1 (é o que destrava a discriminação entre candidatos)
3. B2
4. B3
5. G3 (teste de monotonicidade)
6. G2, M1
7. Regerar o relatório e **comparar o novo Top 10 com o atual**. Se os 10 forem os mesmos, algo não foi aplicado — o resultado tem que mudar.
