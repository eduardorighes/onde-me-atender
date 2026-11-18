"""As 6 ferramentas do servidor MCP.

Cada ferramenta é uma função Python comum (pura: recebe argumentos, devolve um dict).
O decorador `@mcp.tool()` só REGISTRA a função no servidor MCP e a devolve
inalterada, por isso as rotas REST de depuração (app.py) chamam exatamente as
mesmas funções.

O modelo escolhe a ferramenta lendo a docstring (vira a "description") e as
descrições dos parâmetros. Por isso elas são detalhadas e em português.

Regras das respostas:
- pequenas (no máximo 10 estabelecimentos, só campos úteis, nada de JSON bruto);
- sempre com `fonte` e, quando há registros do CNES, `atualizado_em`;
- erros viram um campo `erro` com mensagem amigável (nunca uma exceção).
"""

import math
import re
import unicodedata
from typing import Annotated
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from servidor_mcp import cnes
from servidor_mcp.cep import ErroCEP, consultar_cep, coordenadas_do_endereco

FONTE_CNES = cnes.FONTE  # "CNES/DEMAS"
FONTE_MAPA = "Google Maps (link gerado a partir do endereço)"
FONTE_CEP = "ViaCEP (endereço) / OpenStreetMap ou BrasilAPI (coordenadas)"
MAX_RESULTADOS = 10
# Localização suspeita: a unidade está no MESMO bairro do usuário segundo o CNES, mas as
# coordenadas cadastradas a põem a mais de 1,5 km dele (ex.: Hospital Moinhos de Vento).
LIMITE_SUSPEITO_KM = 1.5
DIFERENCA_MINIMA_KM = 0.3  # só corrige se o endereço achado estiver longe do ponto do CNES
AVISO_DISTANCIA = (
    "Distâncias em linha reta, calculadas com a localização cadastrada no CNES. "
    "O cadastro pode ter erros de localização: uma unidade pode estar mais perto ou mais longe do que parece."
)

mcp = MCPServer(
    name="onde-me-atender",
    instructions=(
        "Ferramentas para encontrar unidades de saúde no Brasil usando o CNES. "
        "Fluxo típico: listar_tipos_unidade -> buscar_estabelecimentos (que recebe o CEP). "
        "Para mostrar uma unidade no mapa: gerar_link_mapa. "
        "Serviços especializados: buscar_por_servico e servicos_do_estabelecimento."
    ),
)


# --- Funções auxiliares --------------------------------------------------------


def normalizar_texto(texto: str | None) -> str:
    """Maiúsculas e sem acentos: 'Saúde' -> 'SAUDE'. Facilita comparar textos."""
    sem_acento = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return sem_acento.upper().strip()


def distancia_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distância em linha reta entre dois pontos (fórmula de Haversine)."""
    raio_terra_km = 6371.0
    fi1, fi2 = math.radians(lat1), math.radians(lat2)
    delta_fi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_fi / 2) ** 2 + math.cos(fi1) * math.cos(fi2) * math.sin(delta_lambda / 2) ** 2
    return 2 * raio_terra_km * math.asin(math.sqrt(a))


def data_mais_recente(registros: list[dict]) -> str | None:
    """Maior `data_atualizacao` (formato AAAA-MM-DD, então dá para comparar como texto)."""
    datas = [r["data_atualizacao"] for r in registros if r.get("data_atualizacao")]
    return max(datas) if datas else None


def link_google_maps(texto: str) -> str:
    """Link de busca do Google Maps. `quote` troca espaços, acentos, vírgulas e barras por
    códigos que podem ir numa URL (ex.: espaço -> %20, "S/N" -> S%2FN). Codificar também a
    barra evita que o modelo "conserte" o link ao copiá-lo."""
    return f"https://www.google.com/maps/search/?api=1&query={quote(texto, safe='')}"


AVISO_TELEFONE = (
    "O telefone está fora do padrão no cadastro do CNES (dígitos a mais ou a menos). "
    "Repasse exatamente como veio, sem corrigir, e avise que pode estar errado."
)


def telefone_fora_do_padrao(telefone: str | None) -> bool:
    """Número que não é fixo (8 dígitos), celular (9) nem 0800, com ou sem DDD.

    Só olha o número principal: '5131354444 RAMAL3413' e '3320-3000 / 3320-3001' são válidos.
    """
    if not telefone:
        return False
    principal = re.split(r"[A-Za-z/;,]", telefone.strip(), maxsplit=1)[0]
    digitos = re.sub(r"\D", "", principal)
    if digitos.startswith("0800"):
        return len(digitos) != 11
    digitos = digitos.lstrip("0")
    if len(digitos) in (8, 10):
        return False
    if len(digitos) == 9:
        return digitos[0] != "9"  # celular sem DDD
    if len(digitos) == 11:
        return digitos[2] != "9"  # celular com DDD
    return True


def resumir_estabelecimento(registro: dict, distancia: float | None = None) -> dict:
    """Só os campos que interessam ao cidadão."""
    endereco = registro.get("endereco_estabelecimento") or ""
    if registro.get("numero_estabelecimento"):
        endereco += f", {registro['numero_estabelecimento']}"

    # Link pelo nome + endereço (não pelas coordenadas, que podem estar erradas no CNES).
    # O endereço do CNES não traz a cidade; o CEP da unidade evita cair em outra cidade.
    partes = [registro.get("nome_fantasia"), endereco, registro.get("bairro_estabelecimento")]
    cep = registro.get("codigo_cep_estabelecimento") or ""
    if len(cep) == 8:
        partes.append(f"{cep[:5]}-{cep[5:]}")
    busca = ", ".join(p for p in partes if p)
    telefone = registro.get("numero_telefone_estabelecimento")

    return {
        "codigo_cnes": registro.get("codigo_cnes"),
        "nome": registro.get("nome_fantasia"),
        "endereco": endereco or None,
        "bairro": registro.get("bairro_estabelecimento"),
        "telefone": telefone,
        # Só aparece quando há problema, para o modelo não "consertar" o número por conta própria.
        **({"telefone_aviso": AVISO_TELEFONE} if telefone_fora_do_padrao(telefone) else {}),
        "turno_atendimento": registro.get("descricao_turno_atendimento"),
        "atende_sus": registro.get("estabelecimento_faz_atendimento_ambulatorial_sus") == "SIM",
        "distancia_km": round(distancia, 1) if distancia is not None else None,
        # Coordenadas da unidade: o Streamlit usa para desenhar o mapa no chat.
        "lat": registro.get("latitude_estabelecimento_decimo_grau"),
        "lon": registro.get("longitude_estabelecimento_decimo_grau"),
        "localizacao_corrigida": bool(registro.get("localizacao_corrigida")),
        "link_google_maps": link_google_maps(busca) if busca else None,
    }


def corrigir_localizacao(registro: dict, lat_usuario: float, lon_usuario: float) -> dict:
    """Procura o endereço da unidade no OpenStreetMap, perto do usuário.

    Se achar um ponto diferente do cadastrado no CNES, devolve uma CÓPIA do registro com
    as coordenadas novas e `localizacao_corrigida=True`. Senão, devolve o registro original.
    """
    endereco = f"{registro.get('endereco_estabelecimento') or ''} {registro.get('numero_estabelecimento') or ''}".strip()
    nova_lat, nova_lon = coordenadas_do_endereco(endereco, lat_usuario, lon_usuario)
    if nova_lat is None:
        return registro  # o OpenStreetMap não achou: fica o que está no CNES
    lat_cnes = registro["latitude_estabelecimento_decimo_grau"]
    lon_cnes = registro["longitude_estabelecimento_decimo_grau"]
    if distancia_km(nova_lat, nova_lon, lat_cnes, lon_cnes) < DIFERENCA_MINIMA_KM:
        return registro  # o CNES estava certo
    return {
        **registro,
        "latitude_estabelecimento_decimo_grau": nova_lat,
        "longitude_estabelecimento_decimo_grau": nova_lon,
        "localizacao_corrigida": True,
    }


def local_do_cep(cep) -> dict:
    """Onde o usuário está, a partir do CEP (pode levantar ErroCEP).

    `lat`/`lon` só vêm quando são da rua (OpenStreetMap). O centro do município (BrasilAPI)
    não serve para ordenar por distância nem para o mapa: nesse caso ficam null e vale o bairro.
    """
    local = consultar_cep(cep)
    da_rua = (local.get("fonte_coordenadas") or "").startswith("OpenStreetMap")
    return {
        "cep": local["cep"],
        "logradouro": local["logradouro"],
        "bairro": local["bairro"],
        "cidade": local["cidade"],
        "uf": local["uf"],
        "codigo_municipio": local["codigo_municipio"],
        "lat": local["lat"] if da_rua else None,
        "lon": local["lon"] if da_rua else None,
        "fonte": FONTE_CEP,
    }


# --- Ferramentas ----------------------------------------------------------------


@mcp.tool()
def listar_tipos_unidade(
    busca: Annotated[
        str | None,
        Field(description="Palavras para filtrar a descrição oficial do tipo. Ex.: 'pronto', 'posto', 'imunizacao'."),
    ] = None,
) -> dict:
    """Lista os tipos de unidade de saúde do CNES ({codigo, descricao}), filtrando pela
    descrição oficial (sem diferenciar acentos/maiúsculas; basta uma palavra coincidir).
    Use para descobrir os códigos de tipo que correspondem ao pedido do usuário antes
    de chamar buscar_estabelecimentos. As descrições são termos técnicos (ex.: 'POSTO DE SAUDE',
    'CENTRO DE SAUDE/UNIDADE BASICA', 'PRONTO ATENDIMENTO', 'CENTRO DE IMUNIZACAO'), então
    palavras do dia a dia como 'vacina' ou 'médico' podem não aparecer: nesse caso chame sem
    `busca` para ver a tabela completa (cerca de 50 tipos) e escolha os mais adequados.
    """
    try:
        tipos = cnes.listar_tipos_unidade()
    except cnes.ErroCNES as erro:
        return {"erro": str(erro), "fonte": FONTE_CNES}

    if busca:
        palavras = [p for p in normalizar_texto(busca).split() if len(p) >= 3]
        tipos = [t for t in tipos if any(p in normalizar_texto(t["descricao"]) for p in palavras)]

    resposta = {"fonte": FONTE_CNES, "total": len(tipos), "tipos": tipos}
    if busca and not tipos:
        resposta["mensagem"] = (
            f"Nenhum tipo contém '{busca}'. Chame listar_tipos_unidade sem `busca` para ver a tabela completa."
        )
    return resposta


@mcp.tool()
def buscar_estabelecimentos(
    cep: Annotated[str, Field(description="CEP do usuário, com 8 dígitos, com ou sem traço. Ex.: '90570-001'.")],
    codigos_tipo_unidade: Annotated[
        list[int] | None,
        Field(description="Códigos dos tipos de unidade (obtidos em listar_tipos_unidade). Ex.: [1, 2]. Recomendado: sem eles a busca é lenta e parcial."),
    ] = None,
    apenas_sus: Annotated[bool, Field(description="Se true (padrão), só unidades com atendimento ambulatorial SUS.")] = True,
    max_resultados: Annotated[int, Field(description="Quantas unidades devolver (padrão 5, máximo 10).")] = 5,
) -> dict:
    """Busca estabelecimentos de saúde ATIVOS perto de um CEP no CNES. A própria ferramenta
    consulta o CEP (rua, bairro, cidade e posição aproximada, devolvidos em `local`) e devolve
    uma lista resumida (nome, endereço, bairro, telefone, turno de atendimento, se atende SUS,
    a distância em km, as coordenadas e um link do Google Maps), ordenada pela distância até
    o CEP. Quando só se conhece o centro do município (`local.lat` null), vêm primeiro as
    unidades do bairro do CEP. Passe em `codigos_tipo_unidade` TODOS os tipos que servem ao
    pedido (ex.: posto de saúde e centro de saúde) numa única chamada: o resultado já vem
    junto e ordenado. Unidades que o CNES põe no mesmo bairro do usuário, mas longe dele, têm
    a posição conferida pelo endereço no OpenStreetMap (`localizacao_corrigida`). CEP inválido
    ou inexistente volta em `erro`.
    """
    try:
        local = local_do_cep(cep)
    except ErroCEP as erro:
        return {"erro": str(erro), "fonte": FONTE_CEP}
    resposta = estabelecimentos_do_municipio(
        local["codigo_municipio"], codigos_tipo_unidade, local["bairro"], apenas_sus, local["lat"], local["lon"], max_resultados
    )
    return resposta if "erro" in resposta else {**resposta, "local": local}


def estabelecimentos_do_municipio(
    codigo_municipio: str | int,
    codigos_tipo_unidade: list[int] | None = None,
    bairro: str | None = None,
    apenas_sus: bool = True,
    lat: float | None = None,
    lon: float | None = None,
    max_resultados: int = 5,
) -> dict:
    """A busca de buscar_estabelecimentos, com o local já resolvido (município, bairro e,
    se houver, coordenadas). Junta os tipos pedidos numa lista só, sem repetir unidades."""
    max_resultados = max(1, min(int(max_resultados), MAX_RESULTADOS))
    por_codigo: dict = {}  # codigo_cnes -> registro
    completo = True
    try:
        for tipo in codigos_tipo_unidade or [None]:
            do_tipo, tipo_completo = cnes.listar_estabelecimentos(codigo_municipio, tipo)
            completo = completo and tipo_completo
            for registro in do_tipo:
                por_codigo.setdefault(registro.get("codigo_cnes"), registro)
    except (ValueError, cnes.ErroCNES) as erro:
        return {"erro": str(erro), "fonte": FONTE_CNES}
    registros = list(por_codigo.values())

    if apenas_sus:
        registros = [r for r in registros if r.get("estabelecimento_faz_atendimento_ambulatorial_sus") == "SIM"]

    if lat is not None and lon is not None:
        ordenacao = "distância até o CEP"

        def distancia(r):
            if r.get("latitude_estabelecimento_decimo_grau") is None or r.get("longitude_estabelecimento_decimo_grau") is None:
                return None  # sem coordenadas no CNES
            return distancia_km(lat, lon, r["latitude_estabelecimento_decimo_grau"], r["longitude_estabelecimento_decimo_grau"])

        bairro_normalizado = normalizar_texto(bairro)
        com_distancia = []
        for r in registros:
            d = distancia(r)
            suspeita = (
                d is not None
                and d > LIMITE_SUSPEITO_KM
                and bairro_normalizado
                and normalizar_texto(r.get("bairro_estabelecimento")) == bairro_normalizado
            )
            if suspeita:
                r = corrigir_localizacao(r, lat, lon)
                d = distancia(r)
            com_distancia.append((r, d))
        # Quem não tem coordenadas vai para o fim da lista.
        com_distancia.sort(key=lambda par: (par[1] is None, par[1] or 0))
    else:
        bairro_normalizado = normalizar_texto(bairro)
        ordenacao = f"bairro '{bairro}' primeiro" if bairro else "ordem alfabética"
        com_distancia = sorted(
            ((r, None) for r in registros),
            key=lambda par: (
                not bairro_normalizado or normalizar_texto(par[0].get("bairro_estabelecimento")) != bairro_normalizado,
                par[0].get("nome_fantasia") or "",
            ),
        )

    resposta = {
        "fonte": FONTE_CNES,
        "atualizado_em": data_mais_recente(registros),
        "codigo_municipio": cnes.converter_codigo_municipio(codigo_municipio),
        "codigos_tipo_unidade": list(codigos_tipo_unidade or []),
        "total_encontrado": len(registros),
        "ordenacao": ordenacao,
        "estabelecimentos": [resumir_estabelecimento(r, d) for r, d in com_distancia[:max_resultados]],
    }
    if not registros:
        resposta["mensagem"] = (
            "Nenhum estabelecimento ativo encontrado com esses filtros. "
            "Tente outro tipo de unidade ou apenas_sus=false."
        )
    if lat is not None and lon is not None:
        resposta["aviso_distancia"] = AVISO_DISTANCIA
        corrigidas = [r.get("nome_fantasia") for r, _ in com_distancia if r.get("localizacao_corrigida")]
        if corrigidas:
            resposta["aviso_distancia"] += (
                f" Posição corrigida pelo endereço (OpenStreetMap), pois o CNES a cadastrou longe "
                f"do endereço: {', '.join(corrigidas)}."
            )
    if not completo:
        resposta["aviso"] = "Lista parcial (o município tem muitos registros). Informe codigos_tipo_unidade."
    return resposta


@mcp.tool()
def detalhar_estabelecimento(
    codigo_cnes: Annotated[str | int, Field(description="Código CNES do estabelecimento. Ex.: '2237571'.")],
) -> dict:
    """Ficha completa (resumida) de um estabelecimento pelo código CNES: tipo de unidade,
    endereço, telefone, e-mail, turno de atendimento, se atende SUS e se tem atendimento
    hospitalar, ambulatorial, centro cirúrgico ou obstétrico. Use quando o usuário pedir
    mais detalhes de uma unidade já encontrada.
    """
    try:
        dados = cnes.obter_estabelecimento(codigo_cnes)
    except (ValueError, cnes.ErroCNES) as erro:
        return {"erro": str(erro), "fonte": FONTE_CNES}
    if dados is None:
        return {"erro": f"Nenhum estabelecimento com o código CNES {codigo_cnes}.", "fonte": FONTE_CNES}

    try:
        tipos = {t["codigo"]: t["descricao"] for t in cnes.listar_tipos_unidade()}
    except cnes.ErroCNES:
        tipos = {}  # sem a tabela, mostramos só o código

    def sim(campo):
        return dados.get(campo) == 1

    return {
        **resumir_estabelecimento(dados),
        "razao_social": dados.get("nome_razao_social"),
        "tipo_unidade": tipos.get(dados.get("codigo_tipo_unidade"), dados.get("codigo_tipo_unidade")),
        "cep": dados.get("codigo_cep_estabelecimento"),
        "codigo_municipio": dados.get("codigo_municipio"),
        "email": dados.get("endereco_email_estabelecimento"),
        "esfera_administrativa": dados.get("descricao_esfera_administrativa"),
        "atendimento_hospitalar": sim("estabelecimento_possui_atendimento_hospitalar"),
        "atendimento_ambulatorial": sim("estabelecimento_possui_atendimento_ambulatorial"),
        "centro_cirurgico": sim("estabelecimento_possui_centro_cirurgico"),
        "centro_obstetrico": sim("estabelecimento_possui_centro_obstetrico"),
        "fonte": FONTE_CNES,
        "atualizado_em": dados.get("data_atualizacao"),
    }


def formatar_competencia(competencia: int) -> str:
    """202607 -> '07/2026'."""
    return f"{competencia % 100:02d}/{competencia // 100}"


# Especialidades médicas não são "serviços" no CNES. Palavras que não dá para deduzir viram o
# termo técnico; as outras viram radical (PEDIATRA -> PEDIATR, que casa com PRONTO SOCORRO
# PEDIATRICO; CARDIOLOGISTA -> CARDIOLOG, que casa com CARDIOLOGIA).
TERMOS_CNES = {
    "PSICOLOGO": "PSICOSSOCIAL",
    "PSICOLOGA": "PSICOSSOCIAL",
    "PSICOLOGIA": "PSICOSSOCIAL",
    "PSIQUIATRA": "PSICOSSOCIAL",
    "PSIQUIATRIA": "PSICOSSOCIAL",
    "DENTISTA": "BUCAL",
    "ODONTOLOGISTA": "BUCAL",
    "ODONTOLOGIA": "BUCAL",
    "FISIOTERAPEUTA": "FISIOTERAP",
    "CANCER": "ONCOLOG",
}
SUFIXOS_ESPECIALIDADE = ("OLOGISTA", "OLOGIA", "IATRA", "IATRIA")  # viram OLOG / IATR
TIPO_CLINICA_ESPECIALIDADE = 36  # CLINICA/CENTRO DE ESPECIALIDADE
ENCAMINHAMENTO_SUS = (
    "No SUS, consulta com especialista (ex.: pediatra, dermatologista) costuma ser marcada por "
    "encaminhamento da UBS (posto de saúde) de referência: o primeiro passo é procurar a UBS mais próxima."
)


def termo_cnes(palavra: str) -> str:
    """Traduz uma palavra (já normalizada) para o vocabulário do CNES: 'PEDIATRA' -> 'PEDIATR'."""
    if palavra in TERMOS_CNES:
        return TERMOS_CNES[palavra]
    for sufixo in SUFIXOS_ESPECIALIDADE:
        if palavra.endswith(sufixo):
            return palavra[: -len(sufixo)] + sufixo[:4]
    return palavra


def resumir_servico(registro: dict) -> dict:
    """Um serviço especializado: o que é, se é pelo SUS e se é ambulatorial e/ou hospitalar."""
    return {
        "servico": registro.get("ds_servico"),
        "classificacao": registro.get("ds_classificacao"),
        "sus": "SIM" in (registro.get("ds_serv_amb_sus"), registro.get("ds_serv_hosp_sus")),
        "ambulatorial": "SIM" in (registro.get("ds_serv_amb_sus"), registro.get("ds_serv_amb_nao_sus")),
        "hospitalar": "SIM" in (registro.get("ds_serv_hosp_sus"), registro.get("ds_serv_hosp_nao_sus")),
    }


@mcp.tool()
def servicos_do_estabelecimento(
    codigo_cnes: Annotated[str | int, Field(description="Código CNES do estabelecimento. Ex.: '2264382'.")],
) -> dict:
    """Lista os serviços especializados cadastrados de uma unidade (ex.: atenção primária,
    imunização, atenção psicossocial, oncologia), com a indicação de SUS e de atendimento
    ambulatorial e/ou hospitalar. Use quando o usuário perguntar o que uma unidade oferece.
    """
    try:
        registros, competencia = cnes.listar_servicos(codigo_cnes=codigo_cnes)
    except (ValueError, cnes.ErroCNES) as erro:
        return {"erro": str(erro), "fonte": FONTE_CNES}

    resposta = {"fonte": FONTE_CNES, "competencia": formatar_competencia(competencia), "codigo_cnes": int(codigo_cnes)}
    if not registros:
        resposta["mensagem"] = (
            f"Nenhum serviço especializado cadastrado para o CNES {codigo_cnes} em "
            f"{formatar_competencia(competencia)}. Confira o código ou consulte detalhar_estabelecimento."
        )
        return resposta

    # Um item por serviço, juntando as classificações (sem repetir).
    por_servico: dict[str, dict] = {}
    for registro in registros:
        resumo = resumir_servico(registro)
        item = por_servico.setdefault(
            resumo["servico"],
            {"servico": resumo["servico"], "classificacoes": [], "sus": False, "ambulatorial": False, "hospitalar": False},
        )
        if resumo["classificacao"] not in item["classificacoes"]:
            item["classificacoes"].append(resumo["classificacao"])
        for campo in ("sus", "ambulatorial", "hospitalar"):
            item[campo] = item[campo] or resumo[campo]

    return {
        **resposta,
        "nome": registros[0].get("no_fantasia"),
        "tipo_unidade": registros[0].get("tp_unidade"),
        "total_servicos": len(por_servico),
        "servicos": sorted(por_servico.values(), key=lambda item: item["servico"]),
    }


@mcp.tool()
def buscar_por_servico(
    cep: Annotated[str, Field(description="CEP do usuário, com 8 dígitos, com ou sem traço. Ex.: '90570-001'.")],
    servico: Annotated[
        str, Field(description="Serviço procurado, em termos do CNES. Ex.: 'psicossocial', 'oncologia', 'fisioterapia'.")
    ],
    apenas_sus: Annotated[bool, Field(description="Se true (padrão), só serviços oferecidos pelo SUS.")] = True,
    max_resultados: Annotated[int, Field(description="Quantas unidades devolver (padrão 5, máximo 10).")] = 5,
) -> dict:
    """Busca as unidades do município do CEP que oferecem um serviço especializado, comparando o
    texto (sem acento e sem diferenciar maiúsculas; todas as palavras precisam aparecer) com o
    nome do serviço e a classificação no CNES. Cada unidade aparece uma vez, com os serviços
    que casaram, endereço, telefone e link do Google Maps. Os nomes são técnicos: saúde mental
    é 'psicossocial' (os CAPS), câncer é 'oncologia'. Se nada casar, a resposta traz os
    serviços existentes no município para você tentar de novo. A primeira busca num município
    pode levar alguns minutos (a API entrega um registro por vez); depois fica em cache.
    Aceita especialidades ('pediatra', 'cardiologista'): viram o termo do CNES, que fica em
    `termos_cnes`. Sem unidade do SUS, a resposta traz `encaminhamento_sus` (como chegar ao
    especialista pela UBS) e `outras_opcoes` (particulares ou clínicas cujo nome cita o termo).
    """
    max_resultados = max(1, min(int(max_resultados), MAX_RESULTADOS))
    try:
        local = local_do_cep(cep)
    except ErroCEP as erro:
        return {"erro": str(erro), "fonte": FONTE_CEP}
    codigo_municipio = local["codigo_municipio"]
    try:
        registros, competencia = cnes.listar_servicos(codigo_municipio=codigo_municipio)
    except (ValueError, cnes.ErroCNES) as erro:
        return {"erro": str(erro), "fonte": FONTE_CNES}

    palavras = [termo_cnes(p) for p in normalizar_texto(servico).split() if len(p) >= 3]
    encontrados = [
        r for r in registros
        if palavras and all(p in normalizar_texto(f"{r.get('ds_servico')} {r.get('ds_classificacao')}") for p in palavras)
    ]
    do_sus = [r for r in encontrados if resumir_servico(r)["sus"]]
    unidades, total = agrupar_por_unidade(do_sus if apenas_sus else encontrados, max_resultados)

    resposta = {
        "fonte": FONTE_CNES,
        "competencia": formatar_competencia(competencia),
        "local": local,
        "servico_buscado": servico,
        "termos_cnes": palavras,
        "total_unidades": total,
        "estabelecimentos": unidades,
    }
    if total:
        return resposta

    resposta["mensagem"] = f"Nenhuma unidade {'do SUS ' if apenas_sus else ''}com o serviço '{servico}' neste município."
    resposta["servicos_no_municipio"] = sorted({r.get("ds_servico") for r in registros if r.get("ds_servico")})
    if apenas_sus:
        resposta["encaminhamento_sus"] = ENCAMINHAMENTO_SUS
        resposta["outras_opcoes"] = outras_opcoes(codigo_municipio, encontrados, palavras, max_resultados)
    return resposta


def agrupar_por_unidade(registros: list[dict], max_resultados: int) -> tuple[list[dict], int]:
    """Cada unidade uma vez, com os serviços que casaram. Devolve (unidades, total de unidades)."""
    por_unidade: dict[str, list[dict]] = {}
    primeiro_registro: dict[str, dict] = {}  # nome e tipo da unidade
    for registro in registros:
        primeiro_registro.setdefault(registro["co_cnes"], registro)
        servicos = por_unidade.setdefault(registro["co_cnes"], [])
        resumo = resumir_servico(registro)
        if resumo not in servicos:
            servicos.append(resumo)

    unidades = []
    for codigo in sorted(por_unidade, key=lambda c: primeiro_registro[c].get("no_fantasia") or "")[:max_resultados]:
        # Endereço e telefone vêm do cadastro do estabelecimento (a rota de serviços não traz).
        try:
            detalhe = cnes.obter_estabelecimento(codigo)
        except cnes.ErroCNES:
            detalhe = None
        detalhe = detalhe or {"codigo_cnes": int(codigo), "nome_fantasia": primeiro_registro[codigo].get("no_fantasia")}
        unidades.append({
            **resumir_estabelecimento(detalhe),
            "tipo_unidade": primeiro_registro[codigo].get("tp_unidade"),
            "servicos": por_unidade[codigo],
        })
    return unidades, len(por_unidade)


def outras_opcoes(codigo_municipio, encontrados: list[dict], palavras: list[str], max_resultados: int) -> list[dict]:
    """Alternativas para quando o SUS não tem o serviço no município (cada uma com `atende_sus`).

    Primeiro, as unidades com o serviço cadastrado só como particular. Se não houver (caso das
    especialidades que não são "serviço" no CNES, como dermatologia), as clínicas de
    especialidade cujo nome contém o termo (ex.: 'ISBS SERVICOS DE DERMATOLOGIA').
    """
    unidades, _ = agrupar_por_unidade(encontrados, max_resultados)
    if unidades:
        return [{**u, "como_encontrada": "serviço cadastrado sem SUS"} for u in unidades]
    if not palavras:
        return []
    try:
        clinicas, _ = cnes.listar_estabelecimentos(codigo_municipio, TIPO_CLINICA_ESPECIALIDADE)
    except (ValueError, cnes.ErroCNES):
        return []
    pelo_nome = [c for c in clinicas if all(p in normalizar_texto(c.get("nome_fantasia")) for p in palavras)]
    pelo_nome.sort(key=lambda c: c.get("nome_fantasia") or "")
    return [{**resumir_estabelecimento(c), "como_encontrada": "nome da clínica"} for c in pelo_nome[:max_resultados]]


@mcp.tool()
def gerar_link_mapa(
    endereco: Annotated[
        str,
        Field(description="Endereço completo, com bairro, cidade e UF. Ex.: 'Rua Ramiro Barcelos, 910, Moinhos de Vento, Porto Alegre - RS'."),
    ],
    nome: Annotated[
        str | None,
        Field(description="Nome do estabelecimento, como veio do CNES. Ex.: 'HOSPITAL MOINHOS DE VENTO'."),
    ] = None,
    usar_coordenadas: Annotated[
        bool,
        Field(description="Deixe false. Use true SÓ se o usuário disser que o link anterior não mostrou o estabelecimento."),
    ] = False,
    lat: Annotated[float | None, Field(description="Latitude da unidade; só é usada com usar_coordenadas=true.")] = None,
    lon: Annotated[float | None, Field(description="Longitude da unidade; só é usada com usar_coordenadas=true.")] = None,
) -> dict:
    """Gera o link do Google Maps de um estabelecimento. Não consulta nenhum serviço: só
    monta a URL. As unidades de buscar_estabelecimentos já trazem `link_google_maps`; use
    esta ferramenta quando o usuário disser que o link não mostrou o estabelecimento ou
    quando faltar o link. Passe o nome e o endereço completo da unidade: o Google Maps costuma
    achar o próprio estabelecimento pelo nome. Se o usuário disser que o link não mostrou o
    estabelecimento, chame de novo com usar_coordenadas=true e a lat/lon da unidade vindas
    de buscar_estabelecimentos (que já traz a posição corrigida, quando foi preciso).
    """
    endereco = (endereco or "").strip()
    nome = (nome or "").strip()

    if usar_coordenadas:
        # Plano B: o ponto cadastrado no CNES (que também pode ter erros).
        if lat is None or lon is None:
            return {"erro": "Para usar coordenadas, informe lat e lon da unidade.", "fonte": FONTE_MAPA}
        return {
            "busca": f"{lat},{lon}",
            "modo": "coordenadas cadastradas no CNES",
            "google_maps": f"https://www.google.com/maps/search/?api=1&query={lat},{lon}",
            "fonte": FONTE_MAPA,
        }

    if not endereco:
        return {"erro": "Informe o endereço do estabelecimento.", "fonte": FONTE_MAPA}

    # Padrão: o site procura pelo texto. O Google Maps entende "nome, endereço" e costuma
    # abrir a ficha do estabelecimento.
    busca = f"{nome}, {endereco}" if nome else endereco
    return {
        "busca": busca,
        "modo": "nome e endereço",
        "google_maps": link_google_maps(busca),
        "fonte": FONTE_MAPA,
    }
