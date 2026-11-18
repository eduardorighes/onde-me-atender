"""Cliente da API de dados abertos do CNES (Cadastro Nacional de Estabelecimentos de Saúde).

Comportamentos da API que este módulo trata (confirmados com chamadas reais):
- `offset` é o NÚMERO DA PÁGINA, não o deslocamento de linhas.
- `limit` máximo é 20; valores maiores são cortados para 20 sem aviso.
- `codigo_municipio` usa 6 dígitos (o código IBGE de 7 dígitos devolve lista vazia).
- A API às vezes responde com erro 5xx: usamos timeout e novas tentativas.

Rota de serviços especializados (/assistencia-a-saude/cnes-servicos-especializados),
também confirmada com chamadas reais (14/11/2026):
- Cada resposta traz SÓ O PRIMEIRO registro da página, qualquer que seja o `limit`.
  `limit=20&offset=1` devolve apenas o 21º registro. Para ler tudo, usamos `limit=1` e
  `offset` = posição do registro: uma requisição por registro.
- Sem `nu_comp` (competência AAAAMM) não há erro, mas vêm dados de 2008. Sempre enviamos.
- A competência mais recente com dados fica alguns meses atrás (em 11/2026, era 07/2026).
- `co_ibge` usa 6 dígitos; `co_cnes` vai como texto de 7 dígitos COM zeros à esquerda
  ("0049603"); como número (49603) volta vazio.
"""

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import httpx

from servidor_mcp.cache import cache

URL_BASE = "https://apidadosabertos.saude.gov.br"
FONTE = "CNES/DEMAS"
TIMEOUT = 30.0  # segundos
TENTATIVAS = 3
ESPERA_INICIAL = 1.0  # segundos; dobra a cada nova tentativa (1 s, 2 s)
LIMITE_MAXIMO = 20  # maior `limit` aceito pela API
MAX_PAGINAS = 50  # teto de segurança: 50 páginas x 20 = 1000 registros

URL_SERVICOS = "/assistencia-a-saude/cnes-servicos-especializados"
MAX_REGISTROS_SERVICOS = 5000  # acima disso o download (1 requisição por registro) demora demais
REQUISICOES_SIMULTANEAS = 8  # para baixar os serviços sem sobrecarregar a API
CAMPOS_SERVICO = [
    "co_cnes",
    "no_fantasia",
    "tp_unidade",
    "ds_servico",
    "ds_classificacao",
    "ds_serv_amb_sus",
    "ds_serv_amb_nao_sus",
    "ds_serv_hosp_sus",
    "ds_serv_hosp_nao_sus",
]

# Campos que guardamos de cada estabelecimento (a API devolve mais de 40).
CAMPOS_UTEIS = [
    "codigo_cnes",
    "nome_fantasia",
    "codigo_tipo_unidade",
    "endereco_estabelecimento",
    "numero_estabelecimento",
    "bairro_estabelecimento",
    "codigo_cep_estabelecimento",
    "numero_telefone_estabelecimento",
    "latitude_estabelecimento_decimo_grau",
    "longitude_estabelecimento_decimo_grau",
    "descricao_turno_atendimento",
    "estabelecimento_faz_atendimento_ambulatorial_sus",
    "data_atualizacao",
]


class ErroCNES(Exception):
    """Erro com mensagem amigável, pronta para mostrar ao usuário."""


def converter_codigo_municipio(codigo) -> str:
    """Converte o código IBGE do município para os 6 dígitos que o CNES usa.

    O código IBGE tem 7 dígitos; o último é um dígito verificador.
    Exemplo: 4314902 (Porto Alegre) -> 431490. Seis dígitos passam intactos.
    """
    texto = str(codigo).strip()
    if not texto.isdigit() or len(texto) not in (6, 7):
        raise ValueError(
            f"Código de município inválido: '{codigo}'. "
            "Use o código IBGE com 6 ou 7 dígitos (ex.: 431490 ou 4314902)."
        )
    return texto[:6]


def _get_json(caminho: str, params: dict | None = None, cliente: httpx.Client | None = None):
    """Faz um GET na API do CNES com timeout e até 3 tentativas (backoff exponencial).

    Devolve o JSON da resposta, ou None se o recurso não existir (HTTP 404).
    Depois de esgotar as tentativas, levanta ErroCNES com uma mensagem amigável.
    """
    if cliente is None:
        with httpx.Client(timeout=TIMEOUT) as novo_cliente:
            return _get_json(caminho, params, novo_cliente)

    motivo = ""
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            resposta = cliente.get(URL_BASE + caminho, params=params)
            if resposta.status_code == 404:
                return None
            if resposta.status_code < 500:
                resposta.raise_for_status()  # outros 4xx: erro do nosso pedido
                return resposta.json()
            motivo = f"HTTP {resposta.status_code}"
        except httpx.TransportError as erro:  # timeout, conexão recusada etc.
            motivo = type(erro).__name__
        except httpx.HTTPStatusError as erro:
            raise ErroCNES(f"A API do CNES recusou o pedido (HTTP {erro.response.status_code}).") from erro

        if tentativa < TENTATIVAS:
            time.sleep(ESPERA_INICIAL * 2 ** (tentativa - 1))

    raise ErroCNES(
        f"A API do CNES não respondeu após {TENTATIVAS} tentativas ({motivo}). "
        "Tente novamente em alguns minutos."
    )


def paginar_estabelecimentos(
    params: dict, cliente: httpx.Client | None = None, max_paginas: int | None = MAX_PAGINAS
) -> tuple[list[dict], bool]:
    """Percorre as páginas de /cnes/estabelecimentos até acabar.

    Devolve (registros, completo). `completo` é False quando paramos por causa
    do teto `max_paginas` (use None para não ter teto).
    """
    if cliente is None:
        with httpx.Client(timeout=TIMEOUT) as novo_cliente:
            return paginar_estabelecimentos(params, novo_cliente, max_paginas)

    registros = []
    pagina = 0
    while max_paginas is None or pagina < max_paginas:
        # Atenção: `offset` é o número da página (0, 1, 2...), não da linha.
        dados = _get_json(
            "/cnes/estabelecimentos",
            {**params, "limit": LIMITE_MAXIMO, "offset": pagina},
            cliente,
        )
        lote = (dados or {}).get("estabelecimentos", [])
        registros.extend(lote)
        if len(lote) < LIMITE_MAXIMO:  # página vazia ou incompleta: era a última
            return registros, True
        pagina += 1
    return registros, False


def listar_estabelecimentos(
    codigo_municipio, codigo_tipo_unidade: int | None = None, cliente: httpx.Client | None = None
) -> tuple[list[dict], bool]:
    """Lista os estabelecimentos ATIVOS de um município (opcionalmente de um tipo).

    Guarda o resultado em cache por (município, tipo) durante 24 h.
    Devolve (registros resumidos aos CAMPOS_UTEIS, completo).
    """
    municipio = converter_codigo_municipio(codigo_municipio)
    chave = f"cnes:estabelecimentos:{municipio}:{codigo_tipo_unidade or 'todos'}"
    guardado = cache.obter(chave)
    if guardado is not None:
        return guardado["registros"], guardado["completo"]

    params = {"codigo_municipio": municipio, "status": 1}
    if codigo_tipo_unidade is not None:
        params["codigo_tipo_unidade"] = codigo_tipo_unidade
    brutos, completo = paginar_estabelecimentos(params, cliente)

    registros = [{campo: r.get(campo) for campo in CAMPOS_UTEIS} for r in brutos]
    cache.guardar(chave, {"registros": registros, "completo": completo})
    return registros, completo


def obter_estabelecimento(codigo_cnes, cliente: httpx.Client | None = None) -> dict | None:
    """Detalhe completo de um estabelecimento, ou None se o código não existir."""
    codigo = str(codigo_cnes).strip()
    if not codigo.isdigit():
        raise ValueError(f"Código CNES inválido: '{codigo_cnes}'. Use apenas números (ex.: 2237571).")
    chave = f"cnes:estabelecimento:{int(codigo)}"
    guardado = cache.obter(chave)
    if guardado is not None:
        return guardado

    dados = _get_json(f"/cnes/estabelecimentos/{int(codigo)}", cliente=cliente)
    if dados is not None:
        cache.guardar(chave, dados)
    return dados


def listar_tipos_unidade(cliente: httpx.Client | None = None) -> list[dict]:
    """Tabela de tipos de unidade: [{"codigo": 1, "descricao": "POSTO DE SAUDE"}, ...]."""
    guardado = cache.obter("cnes:tipos_unidade")
    if guardado is not None:
        return guardado

    dados = _get_json("/cnes/tipounidades", cliente=cliente) or {}
    tipos = sorted(
        (
            {"codigo": t["codigo_tipo_unidade"], "descricao": t["descricao_tipo_unidade"]}
            for t in dados.get("tipos_unidade", [])
        ),
        key=lambda t: t["codigo"],
    )
    cache.guardar("cnes:tipos_unidade", tipos)
    return tipos


# --- Serviços especializados -------------------------------------------------------------


def normalizar_codigo_cnes(codigo_cnes) -> str:
    """O filtro `co_cnes` da rota de serviços exige 7 dígitos com zeros: 49603 -> '0049603'."""
    texto = str(codigo_cnes).strip()
    if not texto.isdigit() or len(texto) > 7:
        raise ValueError(f"Código CNES inválido: '{codigo_cnes}'. Use até 7 números (ex.: 2237571).")
    return texto.zfill(7)


def _servico_na_posicao(params: dict, posicao: int, cliente: httpx.Client) -> dict | None:
    """O registro número `posicao` (0, 1, 2...), ou None se não existir.

    A API só devolve o primeiro registro de cada página; com limit=1, a página `posicao`
    começa exatamente no registro que queremos.
    """
    dados = _get_json(URL_SERVICOS, {**params, "limit": 1, "offset": posicao}, cliente)
    lista = (dados or {}).get("cnes_servicos_especializados", [])
    return lista[0] if lista else None


def competencia_mais_recente(cliente: httpx.Client | None = None) -> int:
    """A competência (AAAAMM) mais recente com dados, testando do mês atual para trás."""
    if cliente is None:
        with httpx.Client(timeout=TIMEOUT) as novo_cliente:
            return competencia_mais_recente(novo_cliente)

    guardado = cache.obter("cnes:servicos:competencia")
    if guardado is not None:
        return guardado

    hoje = date.today()
    ano, mes = hoje.year, hoje.month
    for _ in range(12):
        competencia = ano * 100 + mes
        if _servico_na_posicao({"nu_comp": competencia}, 0, cliente) is not None:
            cache.guardar("cnes:servicos:competencia", competencia)
            return competencia
        ano, mes = (ano, mes - 1) if mes > 1 else (ano - 1, 12)
    raise ErroCNES("A API do CNES não tem dados de serviços especializados nos últimos 12 meses.")


def contar_servicos(params: dict, cliente: httpx.Client) -> int:
    """Quantos registros existem para os filtros, sem baixar todos (busca binária na posição).

    Dobramos a posição até não haver registro (1, 2, 4, 8...) e depois dividimos o intervalo
    ao meio. Para 3.868 registros (Porto Alegre) são ~25 requisições em vez de 3.868.
    """
    if _servico_na_posicao(params, 0, cliente) is None:
        return 0
    existe, nao_existe = 0, 1
    while _servico_na_posicao(params, nao_existe, cliente) is not None:
        existe, nao_existe = nao_existe, nao_existe * 2
    while nao_existe - existe > 1:
        meio = (existe + nao_existe) // 2
        if _servico_na_posicao(params, meio, cliente) is not None:
            existe = meio
        else:
            nao_existe = meio
    return nao_existe


def listar_servicos(
    codigo_municipio=None, codigo_cnes=None, cliente: httpx.Client | None = None
) -> tuple[list[dict], int]:
    """Serviços especializados de um município OU de um estabelecimento, na competência mais recente.

    Devolve (registros resumidos aos CAMPOS_SERVICO, competência). Cache de 24 h por
    (competência, município ou estabelecimento).
    """
    if cliente is None:
        with httpx.Client(timeout=TIMEOUT) as novo_cliente:
            return listar_servicos(codigo_municipio, codigo_cnes, novo_cliente)

    competencia = competencia_mais_recente(cliente)
    params = {"nu_comp": competencia}
    if codigo_cnes is not None:
        params["co_cnes"] = normalizar_codigo_cnes(codigo_cnes)
    else:
        params["co_ibge"] = converter_codigo_municipio(codigo_municipio)

    chave = f"cnes:servicos:{competencia}:{params.get('co_cnes') or params['co_ibge']}"
    guardado = cache.obter(chave)
    if guardado is not None:
        return guardado, competencia

    total = contar_servicos(params, cliente)
    if total > MAX_REGISTROS_SERVICOS:
        raise ErroCNES(
            f"Este município tem {total} registros de serviços especializados; baixar todos "
            "demoraria demais (a API devolve um por requisição). Consulte por estabelecimento."
        )

    # Uma requisição por registro, várias ao mesmo tempo.
    with ThreadPoolExecutor(REQUISICOES_SIMULTANEAS) as executor:
        brutos = executor.map(lambda posicao: _servico_na_posicao(params, posicao, cliente), range(total))
        registros = [{campo: r.get(campo) for campo in CAMPOS_SERVICO} for r in brutos if r]

    cache.guardar(chave, registros)
    return registros, competencia
