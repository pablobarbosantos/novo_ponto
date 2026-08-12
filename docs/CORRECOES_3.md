# CORREÇÕES 3 — a seleção de candidatos está eliminando bairros antes de avaliá-los

Aplicar **depois** de `CORRECOES.md` e `CORRECOES_2.md`.

Origem: bairros conhecidos da cidade (Alto Umuarama, Umuarama, Granja Marileusa) não apareceram no resultado. A investigação mostrou que **não foram mal pontuados — não chegaram a ser avaliados.**

---

# DIAGNÓSTICO

A Fase 4 gera pontos a cada 400 m, aplica filtros duros e corta para os 300 melhores por um score preliminar que é apenas **soma de domicílios num buffer euclidiano de 1,5 km**. Densidade populacional pura, sem renda, sem verticalização, sem concorrência.

Consequências medidas na última execução:

| Fato | Número |
|---|---|
| Candidatos avaliados | 300 |
| Bairros representados | **13 de 75** |
| Candidatos em Presidente Roosevelt (bairro vetado) | **100 (33%)** |
| Candidatos em Alto Umuarama, Umuarama e Granja Marileusa | **0** |

Perfil dos bairros ignorados:

| Bairro | Domicílios | Renda média do responsável | % apartamento |
|---|---:|---:|---:|
| Granja Marileusa | 829 | **R$ 12.829** | 0,216 |
| Umuarama | 1.570 | R$ 5.539 | **0,418** |
| Alto Umuarama | 2.738 | R$ 5.249 | 0,222 |

Granja Marileusa tem **a maior renda do estudo** — cerca de 2,5× a dos bairros que entraram no Top 10 — e foi descartada por ter poucos domicílios, sem que renda fosse sequer consultada.

Dois defeitos independentes, portanto:

1. **D1 — o corte preliminar é unidimensional.** Ordena por população e nada mais, então elimina qualquer região de renda alta e densidade média antes da avaliação real.
2. **D2 — o corte acontece antes das exclusões.** Bairros vetados por conhecimento local consomem o orçamento de candidatos e só são removidos na Fase 7, quando já custaram isócronas e cálculo.

---

# A1 — Aplicar exclusões ANTES do corte preliminar

Hoje `bairros_excluidos` só é aplicado na Fase 7. Mover para a Fase 4, logo após o join com os setores:

```python
excluidos = {normalizar(b["nome"]) for b in cfg["conhecimento_local"]["bairros_excluidos"]}
antes = len(pontos)
pontos = pontos[~pontos["NM_BAIRRO"].map(normalizar).isin(excluidos)]
LOGGER.info("conhecimento local — removidos %d pontos em %d bairros vetados (%d -> %d)",
            antes - len(pontos), len(excluidos), antes, len(pontos))
```

Manter a exclusão também na Fase 7 (defesa em profundidade), mas ali ela deve passar a ser no-op. Se ainda remover alguém, é sinal de bug — registrar como aviso.

**Ganho:** libera cerca de um terço do orçamento de candidatos.

---

# A2 — Cota por bairro no corte preliminar

Mesmo depois de A1, nada impede que um único bairro denso ocupe metade dos 300. Substituir o `head(300)` global por seleção estratificada:

```python
CANDIDATOS_POR_BAIRRO_MAX = 12

sobrou = sobrou.sort_values("score_preliminar", ascending=False)

# 1ª passada: até 12 por bairro, os melhores de cada
top = (sobrou.groupby("NM_BAIRRO", group_keys=False)
             .head(CANDIDATOS_POR_BAIRRO_MAX))

# 2ª passada: se sobrar vaga para chegar a top_n, completar com os melhores globais ainda não escolhidos
if len(top) < top_n:
    resto = sobrou.drop(index=top.index)
    top = pd.concat([top, resto.head(top_n - len(top))])
else:
    top = top.sort_values("score_preliminar", ascending=False).head(top_n)

LOGGER.info("A2 — cota por bairro: %d candidatos em %d bairros distintos (máx %d por bairro)",
            len(top), top["NM_BAIRRO"].nunique(), CANDIDATOS_POR_BAIRRO_MAX)
```

**Aceite:** os 300 candidatos precisam cobrir **no mínimo 25 bairros distintos**. Nenhum bairro com mais de 12.

---

# A3 — Score preliminar multidimensional

Densidade sozinha é o que apagou Granja Marileusa. Trocar por combinação normalizada por percentil, ainda barata (só buffer euclidiano, sem isócrona):

```python
# tudo em buffer euclidiano de 1,5 km, normalizado por percentil na cidade
score_preliminar = (
    0.35 * pct(domicilios_1500m)
  + 0.30 * pct(renda_media_ponderada_1500m)
  + 0.20 * pct(pct_apartamento_ponderado_1500m)
  + 0.15 * pct(1 / (1 + n_concorrentes_1500m))
)
```

Concorrentes já estão disponíveis desde a Fase 2 — a contagem por buffer é barata e evita gastar isócrona em ponto cercado de concorrência.

**Validação obrigatória:** após a mudança, Granja Marileusa, Umuarama e Alto Umuarama **precisam** ter pelo menos 1 candidato cada no pool de 300. Se não tiverem, registrar no log qual filtro os removeu e por quê. Não seguir sem essa resposta.

---

# A4 — Piso de renda está cortando pelo lado errado

`renda_minima_responsavel: 2000` elimina o extremo pobre, mas **nada** elimina o extremo caro-e-vazio. Acrescentar teto de sanidade de densidade, não de renda:

```yaml
candidatos:
  renda_minima_responsavel: 2000
  domicilios_minimos_buffer_1500m: 2500   # abaixo disso não sustenta loja de rua
```

Isso barra corretamente um ponto isolado em área empresarial **sem** barrar Granja Marileusa pelo motivo errado — lá o buffer de 1,5 km alcança vizinhança além dos 829 domicílios do próprio setor.

Registrar no log quantos pontos caíram por esse critério, por bairro.

---

# A5 — Relatório precisa mostrar o que foi descartado

Adicionar seção **"Cobertura da análise"** ao HTML:

- quantos bairros de Uberlândia existem, quantos entraram no pool de candidatos, quantos chegaram ao ranking final
- tabela dos bairros **não avaliados**, com o motivo: vetado por conhecimento local / abaixo do piso de renda / abaixo do piso de domicílios / não entrou no corte preliminar
- destaque para bairros com renda no quartil superior da cidade que não foram avaliados — são os falsos negativos mais caros

**Motivo:** um Top 10 sem essa seção parece dizer "estes são os 10 melhores da cidade", quando na execução anterior significava "os 10 melhores de 13 bairros, um terço deles vetado". A diferença entre as duas frases é a decisão inteira.

---

# A6 — Indicador de mortalidade empresarial: não usar por enquanto

O log registrou que a validação falhou: Centro ficou em **66º de 75** em mortalidade (quase o melhor da cidade) e Presidente Roosevelt em 24º. Nenhum dos dois no quartil superior, ao contrário do esperado.

A decisão automática de rebaixar o filtro para aviso foi correta. Manter assim, **e adicionar no relatório** que o eixo `vitalidade_comercial` está com 15% do peso apoiado num indicador que não validou.

Investigar em rodada futura, nesta ordem:

1. **Casamento de bairro.** 11.773 de 73.116 estabelecimentos não casaram bairro por texto livre. Se o erro for enviesado por região, a taxa está errada onde mais importa.
2. **Recorte de CNAE.** A divisão 47 inteira inclui muito e-commerce e MEI sem loja física, que morrem em massa e não dizem nada sobre comércio de rua. Testar com um subconjunto de CNAEs de loja física.
3. **Denominador.** Bairro com muita abertura recente pode ter mortalidade alta e ser vibrante; bairro estagnado sem abertura nem baixa aparece saudável. Testar `baixadas_36m / estoque médio do período` e, separadamente, um indicador de **taxa de abertura** — que talvez seja o que de fato distingue bairro vivo de bairro parado.
4. **Alternativa direta:** `taxa_fechamento_maps` está zerada porque o levantamento não trouxe `CLOSED_PERMANENTLY`. Com a chave do Places e o campo `business_status`, esse é o indicador mais direto de vitrine vazia — vale mais que a base da Receita.

---

# A7 — Pendências herdadas ainda abertas

Registrar no relatório, sem tentar consertar agora:

| Item | Situação |
|---|---|
| Teste absoluto | Aprovação de **100%** dos candidatos. Continua não discriminando — o parâmetro precisa ser recalibrado quando `imoveis.csv` trouxer aluguel real |
| `domicilios_efetivo` | Mediana de 28,3% do município, acima do alvo. O log documenta que a malha viária de Uberlândia é geometricamente generosa mesmo em 5 min — aceitável como característica local, mas o número precisa aparecer no relatório |
| Recência de avaliações (C1.3) | Interrompido por cota do Places (123 de 273). Retomar quando renovar |
| Oferta de imóvel | Eixo de 20% ausente; pesos renormalizados. Some assim que `imoveis.csv` for preenchido |

---

# ORDEM DE APLICAÇÃO

1. A1 — exclusões antes do corte
2. A4 — piso de domicílios no buffer
3. A3 — score preliminar multidimensional
4. A2 — cota de 12 por bairro
5. A5 — seção de cobertura no relatório
6. A6 — nota metodológica sobre o indicador não validado
7. Regerar e comparar

**Aceite final:** pool de 300 candidatos cobrindo ≥ 25 bairros, nenhum bairro com mais de 12, zero candidatos em bairros vetados, e Granja Marileusa, Umuarama e Alto Umuarama presentes no pool — avaliados e aprovados ou avaliados e reprovados, mas com motivo registrado.
