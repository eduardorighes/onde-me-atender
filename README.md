# Onde me atender

Aplicação didática que ajuda um cidadão a encontrar **unidades de saúde próximas** no Brasil.
O usuário informa um CEP e o que precisa ("vacina", "pronto atendimento", "posto de saúde"); um
agente baseado em Claude consulta o **CNES** (Cadastro Nacional de Estabelecimentos de Saúde) por
meio de um **servidor MCP local** e responde com as unidades mais próximas: nome, endereço,
telefone e turno de atendimento.

Prioridades do código: **clareza > simplicidade > funcionalidades**. Município de referência nos
exemplos e testes: **Porto Alegre/RS** (IBGE `4314902`, ou `431490` com 6 dígitos).

> Os dados vêm de um cadastro: não informam vaga, fila nem plantão do dia. **Confirme por telefone
> antes de ir.** Em emergência, ligue **192 (SAMU)**.

## Arquitetura

```
┌──────────────────────┐   1. pergunta + histórico + lista de ferramentas   ┌─────────────────┐
│  Streamlit           │ ─────────────────────────────────────────────────► │  API Anthropic  │
│  cliente/app.py      │ ◄───────────────────────────────────────────────── │  (Claude)       │
│  cliente/agente.py   │   2. "tool_use": quero chamar listar_tipos...(...) └─────────────────┘
│  (faz o papel de     │
│   cliente MCP)       │   3. call_tool  ┌──────────────────────────────┐    ┌──────────────────┐
│                      │ ──────────────► │ Servidor MCP local           │ ─► │ CNES (Min. Saúde)│
│                      │ ◄────────────── │ FastAPI + MCPServer          │    └──────────────────┘
└──────────────────────┘   4. resultado  │ http://localhost:8000/mcp    │ ─► ViaCEP / OpenStreetMap
                                         │ /api/... (REST p/ depuração) │    / BrasilAPI
                                         └──────────────────────────────┘
```

**A API da Anthropic nunca acessa o servidor local.** O modelo só *pede* uma ferramenta; quem a
executa é o nosso código (`cliente/agente.py`), que devolve o resultado ao modelo.

### O caminho de uma pergunta

Pergunta: *"CEP 90570-001, onde tomo vacina?"*

1. O Streamlit chama `responder()` em `cliente/agente.py`, que conecta ao MCP e pede a lista de
   ferramentas (`list_tools`). As 6 ferramentas são convertidas para o formato da Messages API.
2. O agente envia ao Claude: prompt de sistema + histórico + ferramentas.
3. O Claude responde com `stop_reason == "tool_use"` e um bloco `listar_tipos_unidade({})`, para
   descobrir que "vacina" corresponde a "CENTRO DE IMUNIZACAO", "CENTRO DE SAUDE/UNIDADE BASICA" etc.
4. O agente executa `call_tool("listar_tipos_unidade", ...)` no servidor MCP e devolve o
   resultado ao Claude como `tool_result`.
5. O Claude então chama `buscar_estabelecimentos({"cep": "90570-001", "codigos_tipo_unidade": [...]})`.
   A própria ferramenta consulta o CEP (ViaCEP e OpenStreetMap), pagina o CNES para cada tipo,
   filtra e ordena tudo por distância.
6. Quando o Claude tem o que precisa, responde com `stop_reason == "end_turn"` e o texto final.
7. O Streamlit mostra a resposta e, na barra lateral, cada chamada de ferramenta, tokens e tempos.

Teto de segurança: no máximo **8 chamadas de ferramenta por turno**; ao atingir, o agente manda
`tool_choice: none` e o modelo é obrigado a responder com o que já tem.

## Estrutura

```
onde-me-atender/
├── README.md
├── pyproject.toml / uv.lock      # projeto e dependências (gerenciados pelo uv)
├── .env.example                  # modelo do arquivo .env (sem valores reais)
├── servidor_mcp/
│   ├── app.py                    # FastAPI + montagem do MCP + rotas REST de depuração
│   ├── ferramentas.py            # as 6 ferramentas (funções puras + registro no MCP)
│   ├── cnes.py                   # cliente do CNES (paginação, retry, 6/7 dígitos, serviços)
│   ├── cep.py                    # ViaCEP + coordenadas (OpenStreetMap / BrasilAPI)
│   └── cache.py                  # cache em memória + JSON em .cache/ (TTL 24 h)
├── cliente/
│   ├── app.py                    # interface Streamlit
│   ├── mapa.py                   # mapa embutido no chat (pydeck)
│   └── agente.py                 # loop com a API Anthropic (roda sem Streamlit)
├── avaliacao/
│   ├── casos.yaml                # 32 casos de avaliação
│   ├── avaliar.py                # avaliação ponta a ponta (métricas + relatório)
│   ├── avaliar_cli.py            # a mesma avaliação pelo Claude Code headless (sem a chave da API)
│   ├── validar_dados.py          # validação dos dados do CNES
│   └── resultados/               # relatórios gerados
└── tests/                        # pytest, sem rede (fixtures com respostas reais)
```

## Etapa 0: o projeto com uv

Pré-requisitos: [uv](https://docs.astral.sh/uv/) instalado e uma chave da API da Anthropic.
Num clone do repositório basta:

```bash
uv sync                      # cria o .venv e instala exatamente as versões do uv.lock
cp .env.example .env         # depois edite o .env e coloque sua ANTHROPIC_API_KEY
```

Como o projeto foi criado (para referência; não precisa repetir):

| Comando | O que faz |
|---|---|
| `uv init onde-me-atender --app --no-package --python 3.12` | cria `pyproject.toml`, `.python-version` e um repositório git; `--no-package` porque é uma aplicação, não uma biblioteca a publicar |
| `uv add fastapi uvicorn mcp httpx anthropic streamlit python-dotenv pyyaml` | adiciona as dependências ao `pyproject.toml`, resolve as versões, grava o `uv.lock` e instala no `.venv` |
| `uv add --dev pytest` | dependência só de desenvolvimento (grupo `dev`) |
| `uv sync` | instala o que está no `uv.lock` (use depois de clonar ou de um `git pull`) |
| `uv run <comando>` | roda o comando dentro do ambiente do projeto, sem precisar "ativar" o venv |

Variáveis do `.env`:

| Variável | Padrão | Para quê |
|---|---|---|
| `ANTHROPIC_API_KEY` | (obrigatória) | chave da API |
| `CLAUDE_MODELO` | `claude-sonnet-5-5` | use `claude-haiku-5-5` para a opção econômica |
| `CLAUDE_ESFORCO` | `medium` | `low` é mais rápido e barato; `high` raciocina mais |
| `MCP_URL` | `http://localhost:8000/mcp` | onde o agente encontra o servidor MCP |

## Etapa 1: cliente do CNES

`servidor_mcp/cnes.py` trata as manhas da API (confirmadas com chamadas reais):

- `offset` é o **número da página**, não o deslocamento: `limit=5&offset=1` devolve os registros 6–10;
- `limit` máximo é **20** (valores maiores são cortados sem aviso);
- `codigo_municipio` usa **6 dígitos**: com 7 (`4314902`) a API devolve lista vazia, sem erro.
  A conversão é tirar o último dígito (verificador): `4314902 → 431490`;
- a API às vezes dá erro 5xx: timeout de 30 s e **3 tentativas** com espera de 1 s e 2 s;
- municípios grandes têm milhares de registros: sempre filtramos por tipo e guardamos em cache
  por (município, tipo). Há um teto de 50 páginas (1000 registros) para buscas sem tipo.

```bash
uv run pytest -v
```

Deve aparecer `78 passed`. Os testes **não usam a rede**: as respostas reais de Porto Alegre estão
salvas em `tests/fixtures/` e as chamadas HTTP são simuladas com `httpx.MockTransport`.

### Serviços especializados (rota `/assistencia-a-saude/cnes-servicos-especializados`)

Confirmado com chamadas reais em 14/11/2026, e diferente das outras rotas:

- **Cada resposta traz só o primeiro registro da página**, qualquer que seja o `limit` (testado de
  1 a 2000): `limit=20&offset=1` devolve só o 21º registro. Para ler tudo, usamos `limit=1` e
  `offset` = posição: **uma requisição por registro**. Para não baixar às cegas, o total é
  descoberto antes por busca binária na posição (~25 requisições para os 3.868 registros de
  Porto Alegre). O download usa 8 requisições simultâneas: Porto Alegre levou ~160 s na primeira
  vez, uma UBS ~4 s. Municípios com mais de 5.000 registros são recusados com mensagem.
- **Sem `nu_comp` não há erro**, mas vêm dados de 2008; por isso ele é sempre enviado.
- **Competência mais recente:** dados até 07/2026 (de 08/2026 em diante vem vazio).
  `competencia_mais_recente()` testa do mês atual para trás (até 12 meses) e guarda em cache.
- **`co_cnes` vai com 7 dígitos e zeros à esquerda** (`"0049603"`); como número, volta vazio.
- **Não há filtro por serviço** na API: a busca por texto é local (sem acento, sem diferenciar
  maiúsculas, todas as palavras precisam aparecer em `ds_servico` + `ds_classificacao`). Os nomes
  são técnicos: "saúde mental" literal só acha leitos hospitalares; os CAPS estão em
  "ATENCAO PSICOSSOCIAL". Por isso o prompt traz referências (saúde mental → "psicossocial") e,
  se nada casar, a ferramenta devolve os serviços existentes no município.
- A rota não traz endereço nem telefone; `buscar_por_servico` os completa com o cadastro do
  estabelecimento (até 10 consultas, em cache).

## Etapa 2: servidor MCP

```bash
uv run uvicorn servidor_mcp.app:app --reload
```

- http://localhost:8000/health → `{"status":"ok","mcp":"/mcp","ferramentas":6}`
- http://localhost:8000/docs → interface do FastAPI para testar as rotas REST, que chamam
  **as mesmas funções** das ferramentas MCP. Experimente:
  - `GET /api/cep/90570-001` (não é ferramenta: mostra o que as buscas descobrem sobre o CEP)
  - `GET /api/tipos-unidade?busca=pronto`
  - `GET /api/estabelecimentos?cep=90570-001&codigos_tipo_unidade=1&codigos_tipo_unidade=2`
  - `GET /api/estabelecimentos/2237571`
  - `GET /api/estabelecimentos/2264382/servicos`
  - `GET /api/servicos?cep=90570-001&servico=psicossocial`
  - `GET /api/mapa?nome=HOSPITAL MOINHOS DE VENTO&endereco=Rua Ramiro Barcelos, 910, Porto Alegre - RS`
- Endpoint MCP: `http://localhost:8000/mcp` (transporte *streamable HTTP*). Para inspecionar:

```bash
npx @modelcontextprotocol/inspector
# na interface: Transport Type = Streamable HTTP, URL = http://localhost:8000/mcp, Connect
# ou pela linha de comando:
npx @modelcontextprotocol/inspector --cli http://localhost:8000/mcp --transport http \
    --method tools/call --tool-name listar_tipos_unidade --tool-arg busca=pronto
```

### As 6 ferramentas

| Ferramenta | Entrada | Saída |
|---|---|---|
| `listar_tipos_unidade` | `busca` (opcional) | `{codigo, descricao}` filtrados por texto (sem diferenciar acentos) |
| `buscar_estabelecimentos` | `cep`, `codigos_tipo_unidade` (lista), `apenas_sus` (padrão `true`), `max_resultados` (padrão 5, máx. 10) | `local` (rua, bairro, cidade, município e `lat`/`lon` do CEP, ou `null`) e a lista resumida dos tipos pedidos, ordenada por distância (Haversine) ou com o bairro primeiro |
| `detalhar_estabelecimento` | `codigo_cnes` | ficha resumida do estabelecimento |
| `servicos_do_estabelecimento` | `codigo_cnes` | serviços especializados da unidade (um item por serviço, com as classificações), com SUS e ambulatorial/hospitalar |
| `buscar_por_servico` | `cep`, `servico` (texto livre), `apenas_sus` (padrão `true`), `max_resultados` (padrão 5, máx. 10) | `local` e as unidades do município do CEP que oferecem o serviço, cada uma uma vez, com endereço, telefone e link; se nada casar, a lista de serviços do município |
| `gerar_link_mapa` | `endereco`, `nome`; `usar_coordenadas` + `lat`/`lon` só como plano B | link do Google Maps (busca por nome + endereço); não consulta nenhum serviço |

Todas devolvem `fonte` e, quando há registros do CNES, `atualizado_em` (data mais recente). Erros
viram um campo `erro` com mensagem amigável, nunca uma exceção. Nenhum código de tipo de unidade
está fixo no código: o modelo usa `listar_tipos_unidade` para descobri-los.

### Integração MCP + FastAPI (detalhes da versão instalada, `mcp` 2.3)

- No `mcp` 2.x a classe `FastMCP` foi **renomeada para `MCPServer`**
  (`from mcp.server.mcpserver import MCPServer`). O uso é o mesmo: `@mcp.tool()` registra funções.
- `mcp.streamable_http_app()` cria um app Starlette que já atende em `/mcp`. Se ele fosse montado
  em `app.mount("/mcp", ...)`, o endpoint ficaria em `/mcp/mcp`. Por isso ele é montado na raiz
  (`app.mount("/", ...)`), **depois** das rotas do FastAPI.
- O app montado não roda o próprio *lifespan*; então o `lifespan` do FastAPI inicia o
  `mcp.session_manager.run()`.

## Etapa 3: agente na linha de comando

Com o servidor no ar e a chave no `.env`:

```bash
uv run python cliente/agente.py                       # 3 perguntas de exemplo
uv run python cliente/agente.py "CEP 91350-200, preciso de um pronto atendimento"
```

Para cada pergunta aparecem as ferramentas chamadas (com argumentos e tempo), a resposta, os
tokens, o custo estimado e a divisão do tempo entre modelo e ferramentas. Esperado:

- *"Moro no CEP 90570-001, onde posso tomar vacina?"* → `listar_tipos_unidade`,
  `buscar_estabelecimentos` e uma lista numerada de até 5 unidades, com o aviso de confirmar por telefone;
- *"Onde tem posto de saúde?"* → nenhuma ferramenta; o agente pede o CEP;
- *"CEP 91350-200, preciso de um pronto atendimento"* → pronto atendimentos perto do Cristo Redentor.

O loop de `agente.py` também é testado sem rede em `tests/test_agente.py`, com um "Claude falso"
roteirizado conversando com o servidor MCP de verdade no mesmo processo.

## Etapa 4: interface Streamlit

```bash
uv run streamlit run cliente/app.py
```

Abre em http://localhost:8501. Pergunte *"CEP 90570-001, onde tomo vacina?"*. Abaixo da resposta
aparece um **mapa** com a sua posição aproximada (azul) e as unidades citadas (vermelho); ao passar
o mouse sobre um ponto, aparecem o nome, o endereço, o telefone e a distância. Na barra lateral,
cada turno tem um painel com cada chamada de ferramenta (nome, argumentos, resultado resumido e
tempo), os tokens de entrada/saída, o custo estimado e a latência. O botão **Nova conversa** limpa
o histórico, e o rodapé mostra o aviso sobre os dados do CNES e o 192. Antes da primeira pergunta
aparece um **aviso de privacidade (LGPD)**: basta informar o CEP e o que precisa, sem dados
pessoais além do necessário.

## Etapa 5: avaliação

### 1. Testes determinísticos (sem rede)

`uv run pytest` cobre: conversão 6/7 dígitos, paginação (offset por página, parada na página
vazia/incompleta, `limit ≤ 20`), Haversine (tolerância de 1%), formato das ferramentas (só os
campos previstos, máx. 10 itens, `fonte`), retry em 5xx com mensagem amigável, CEP, rotas REST e o
loop do agente.

### 2. Validação dos dados (com rede, sem custo de API)

```bash
uv run python avaliacao/validar_dados.py
```

Para Porto Alegre, Canoas, Caxias do Sul e Pelotas (tipos 1, 2 e 73) confere se a contagem da
ferramenta bate com uma paginação direta da API, a busca com 6 e 7 dígitos, o percentual de
registros com coordenadas (e dentro do Brasil), com telefone e com SUS, e as datas de
atualização. Gera `avaliacao/resultados/validacao_AAAA-MM-DD.md`.

### 3. Avaliação do agente (ponta a ponta, usa a API)

```bash
uv run python avaliacao/avaliar.py                    # 32 casos x 3 repetições
uv run python avaliacao/avaliar.py --casos sem_cep,emergencia_dor_no_peito --repeticoes 1
uv run python avaliacao/avaliar.py --juiz             # + notas de um LLM como juiz
uv run python avaliacao/avaliar.py --sufixo rodada2   # resultados/AAAA-MM-DD_rodada2.md

# Sem a ANTHROPIC_API_KEY, com o login do Claude Code (claude -p):
uv run python avaliacao/avaliar_cli.py --modelo claude-haiku-5-5 --sufixo claude-haiku-5-5_cli
```

Gera `avaliacao/resultados/AAAA-MM-DD.md` e `.csv`. O relatório traz as métricas abaixo, um resumo
por caso, as **execuções que falharam** (pergunta, ferramentas chamadas com argumentos e resposta)
e 10 respostas sorteadas para revisão humana.

| Métrica | Como é medida | Meta |
|---|---|---|
| Acerto de ferramentas | ferramentas chamadas incluem as esperadas (`[]` = nenhuma) | ≥ 90% |
| Acerto de argumentos | códigos de tipo passados a `buscar_estabelecimentos` coerentes com o pedido, no município do CEP | ≥ 90% |
| Hit@5 | ≥ 1 unidade respondida no top-5 do gabarito (CNES direto + distância) | ≥ 80% |
| Precisão@5 | fração das unidades respondidas que estão no top-5 | ≥ 60% |
| Fidelidade | telefones, códigos CNES, nomes e links da resposta aparecem nos resultados das ferramentas | 100% |
| Conformidade de segurança | aviso de confirmação; 192 em emergência; sem termos proibidos | 100% |
| Tratamento de erro | CEP inválido ou sem unidade gera resposta útil, sem travar | 100% |
| Consistência | mesmo conjunto de ferramentas nas 3 execuções | ≥ 80% |
| Latência, custo, chamadas/turno | p50/p95, tempo no modelo x ferramentas, tokens, US$ | registrar |

Os casos cobrem bairros de Porto Alegre (Centro Histórico, Moinhos, zona norte e zona sul), Canoas,
Caxias do Sul, Pelotas, São Paulo e Rio de Janeiro, pedido ambíguo ("médico"), emergência, CEP
inválido, município sem a unidade pedida (André da Rocha, sem pronto atendimento), pedidos fora do
escopo e conversas de dois turnos (duas pedem o link do mapa, uma delas reclamando que o link não achou o lugar). Há também 3 casos de serviços especializados. Uma rodada completa tem 96 execuções (4 em paralelo) e faz
centenas de chamadas ao modelo; o tempo e o custo estimado aparecem no relatório. Para
experimentar barato, use `--casos` e `--repeticoes 1`.

## Decisões e desvios do enunciado

- **Coordenadas do CEP:** testando com CEPs reais, a BrasilAPI devolve o **centro do município**
  (todos os CEPs de Porto Alegre dão `-30.03283, -51.23019`), o que tornaria a ordenação por
  distância enganosa. Por isso `cep.py` geocodifica "logradouro + cidade" no **Nominatim
  (OpenStreetMap)**, gratuito e sem chave (no máximo 1 consulta/s, respeitado no código e pelo
  cache). A BrasilAPI fica como último recurso, marcada como `"BrasilAPI (centro do município)"`;
  nesse caso o agente ordena pelo bairro.
- **Ruas longas:** no OpenStreetMap uma rua longa vira vários trechos, cada um com o seu CEP
  (a Rua Doutor Barcelos passa por Cavalhada, Camaquã e Tristeza), e a ordem dos resultados muda
  entre consultas. `cep.py` pede até 10 trechos e escolhe o do **mesmo CEP**; sem ele, o do mesmo
  bairro. Pegar só o primeiro chegou a errar a posição do usuário em ~2 km.
- **`listar_tipos_unidade` sem `busca`** devolve a tabela inteira (~50 tipos curtos), acima do
  limite de 10 itens: é uma tabela de referência, e o modelo precisa dela quando o pedido usa
  palavras do dia a dia ("vacina", "médico") que não aparecem nas descrições oficiais.
- **Mapa no chat (`cliente/mapa.py`):** é desenhado pela interface com `pydeck` (já vem com o
  Streamlit), a partir dos resultados das ferramentas que o agente recebeu. O modelo não participa,
  então o mapa não custa tokens nem chamadas. Entram só as unidades citadas na resposta
  (reconhecidas pelo código CNES) e a posição do usuário apenas quando vem da rua (OpenStreetMap),
  nunca o centro do município. Para isso, `buscar_estabelecimentos` devolve o `lat`/`lon` de cada
  unidade e o `local` do CEP (também devolvido por `buscar_por_servico`).
- **Link do Google Maps em todo endereço:** cada unidade devolvida por `buscar_estabelecimentos`
  (e por `detalhar_estabelecimento`) já traz um `link_google_maps`, montado com nome + endereço +
  bairro + CEP da unidade, e o prompt manda colocá-lo junto de todo endereço listado. Gerar o link
  dentro da ferramenta evita uma chamada de `gerar_link_mapa` por unidade (5 unidades estourariam
  o teto de 8 chamadas por turno). A avaliação confere se cada link da resposta veio de uma ferramenta.
- **5ª ferramenta (`gerar_link_mapa`):** o enunciado previa 4; ela foi acrescentada depois.
  Não acessa a rede, só monta a URL do Google Maps (com `urllib.parse.quote` para espaços e
  acentos); o link do OpenStreetMap foi retirado para economizar tokens. Com o mapa embutido e o
  `link_google_maps` de cada unidade, o agente só a usa para o plano B abaixo.
  O link busca por **nome + endereço**, e não pelas coordenadas do CNES, que podem estar erradas
  (caso do Hospital Moinhos de Vento). Se o usuário disser que o link não mostrou o
  estabelecimento, o agente chama de novo com `usar_coordenadas=true` (plano B). Uma flag
  explícita é mais confiável que esperar o modelo deixar `lat`/`lon` em branco.
- **CEP dentro das buscas (sem `buscar_cep`):** havia uma ferramenta só para o CEP, e o modelo
  copiava município, bairro e coordenadas dela para `buscar_estabelecimentos`, uma chamada por
  tipo de unidade. Agora `buscar_estabelecimentos` e `buscar_por_servico` recebem o CEP, e a
  primeira aceita vários tipos de uma vez: uma busca comum cai de 3 a 5 chamadas para 2. A regra
  "centro do município não serve como posição do usuário" saiu do prompt e virou código
  (`local_do_cep`). O bloco `local` traz `fonte` (ViaCEP etc.), mas não `atualizado_em`: não há
  data nesses serviços.
- **Raciocínio do modelo:** o Claude Sonnet 5.5 usa raciocínio adaptativo por padrão; o histórico
  guarda a resposta inteira do modelo (inclusive esses blocos) e só cresce, nunca é editado.
- **Sem cache de prompt:** seria uma otimização de custo fácil (`cache_control`), deixada de fora
  para manter o código simples.

## Limitações

- O CNES é um cadastro: não informa vaga, fila, plantão do dia nem quais vacinas estão disponíveis.
- **As coordenadas do CNES podem estar erradas.** Exemplo real: o Hospital Moinhos de Vento (CNES
  3006522, Rua Ramiro Barcelos, 910) está cadastrado com um ponto no Centro Histórico, ~2 km do
  endereço verdadeiro; para o CEP 90570-110 ele aparecia a 2,3 km em vez de ~0,5 km. Duas medidas:
  - **Aviso:** `buscar_estabelecimentos` devolve um `aviso_distancia`, o agente avisa que as
    distâncias seguem o cadastro e a legenda do mapa diz o mesmo.
  - **Correção das localizações suspeitas:** se o CNES põe a unidade no **mesmo bairro** do usuário,
    mas a mais de 1,5 km dele, o endereço da unidade é procurado no OpenStreetMap, numa caixa de
    ~5 km ao redor do usuário (o endereço do CNES não traz a cidade). Se o ponto achado estiver a
    mais de 300 m do cadastrado, ele substitui o do CNES na distância, no mapa e no link, com
    `localizacao_corrigida: true`. Custa uma consulta por unidade suspeita (em geral, nenhuma).
    Limite: erros em unidades de **outros** bairros não são detectados.
  - O gabarito da avaliação (Hit@5) continua usando as coordenadas do CNES sem correção.
- Coordenadas do usuário são da rua (não do número); a distância é em linha reta.
- O filtro `apenas_sus` usa o campo de atendimento **ambulatorial** SUS do CNES.
- O cache (`.cache/`) dura 24 h; apague a pasta para forçar dados novos.
- `buscar_por_servico` lista as unidades em ordem alfabética, não por distância: a rota de serviços
  não tem coordenadas. A primeira busca de serviços num município grande leva alguns minutos.
