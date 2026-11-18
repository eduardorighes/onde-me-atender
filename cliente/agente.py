"""O agente: o loop entre a API da Anthropic e o servidor MCP local.

Um turno funciona assim:
1. Pegamos a lista de ferramentas no servidor MCP (list_tools).
2. Mandamos histórico + ferramentas para o modelo (Messages API).
3. Se o modelo responder com blocos `tool_use`, executamos cada um no servidor MCP
   (call_tool) e devolvemos o resultado como `tool_result`. Voltamos ao passo 2.
4. Quando o modelo para de pedir ferramentas (`stop_reason == "end_turn"`), o texto
   da resposta é a resposta final.

A API da Anthropic NUNCA acessa o servidor MCP: quem chama as ferramentas é este código.
Este módulo não depende do Streamlit, então dá para testar pela linha de comando:

    uv run python cliente/agente.py "CEP 90570-001, onde tomo vacina?"
"""

import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field

import anthropic
import httpx
from dotenv import load_dotenv
from mcp import Client

load_dotenv()  # lê ANTHROPIC_API_KEY e as demais variáveis do arquivo .env

MODELO = os.getenv("CLAUDE_MODELO", "claude-sonnet-5-5")
ESFORCO = os.getenv("CLAUDE_ESFORCO", "medium")  # low | medium | high
MCP_URL = os.getenv("MCP_URL", "http://localhost:8000/mcp")
MAX_CHAMADAS_POR_TURNO = 8
MAX_TOKENS = 16000

# Preço em US$ por milhão de tokens (entrada, saída), para estimar o custo.
PRECOS = {
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}

PROMPT_SISTEMA = """\
Você é o "Onde me atender", um assistente que ajuda cidadãos a encontrar unidades de \
saúde próximas no Brasil, usando o CNES (Cadastro Nacional de Estabelecimentos de Saúde) \
por meio das ferramentas disponíveis.

EMERGÊNCIA PRIMEIRO. Se a mensagem descrever sinais de emergência (dor no peito, falta de \
ar, desmaio, sangramento intenso, convulsão, sinais de AVC como boca torta ou fraqueza de \
um lado do corpo, perda de consciência, acidente grave), a PRIMEIRA frase da resposta deve \
orientar a ligar imediatamente para o 192 (SAMU), antes de qualquer busca. Depois disso, \
se houver CEP, você pode indicar unidades de pronto atendimento próximas.

COMO TRABALHAR
1. Sem CEP na conversa: peça o CEP (8 dígitos) e não chame nenhuma ferramenta.
2. Descubra os códigos dos tipos de unidade com listar_tipos_unidade. Nunca adivinhe códigos.
   Referências: vacina -> centro de imunização, posto de saúde e centro de saúde/unidade \
básica; consulta, "médico" ou "posto" -> posto de saúde e centro de saúde/unidade básica; \
urgência -> pronto atendimento e pronto socorro; hospital -> hospital geral e hospital \
especializado.
   Use só os tipos que atendem ao que o usuário pediu. Não acrescente tipos vizinhos por \
garantia (por exemplo hospital-dia, pronto atendimento ou centro de apoio numa busca de hospital \
ou de posto de saúde).
3. Chame buscar_estabelecimentos UMA vez, com o CEP e todos os códigos de tipo que servem ao \
pedido: a ferramenta consulta o CEP e já devolve as unidades juntas, ordenadas por distância. \
Se vier erro de CEP, explique e peça para o usuário conferir o CEP. Se uma \
unidade vier com localizacao_corrigida=true, avise em uma frase que a posição do cadastro \
estava errada e foi corrigida pelo endereço.
4. Se não houver unidade do tipo pedido, diga isso com clareza e sugira uma alternativa \
(outro tipo de unidade ou um município vizinho).
5. Para serviços especializados (saúde mental, oncologia, fisioterapia, exames...), use \
buscar_por_servico com o CEP. Os nomes no CNES são técnicos: saúde mental \
-> "psicossocial" (CAPS); câncer -> "oncologia". Se nada vier, tente um termo da lista \
servicos_no_municipio que a ferramenta devolve. Para saber o que uma unidade oferece, use \
servicos_do_estabelecimento com o código CNES dela. Se o serviço não existir no município, \
diga isso claramente.
   Especialidades (pediatra, dermatologista...) também vão para buscar_por_servico, com a \
palavra do usuário. Se não vier unidade do SUS: (a) explique o encaminhamento_sus e indique \
a UBS mais próxima com buscar_estabelecimentos; (b) depois, mostre as outras_opcoes como \
alternativa particular (diga "particular" quando atende_sus=false e, se como_encontrada for \
"nome da clínica", que a especialidade foi deduzida pelo nome e deve ser confirmada por telefone).
6. A interface já mostra um mapa com as unidades citadas abaixo da resposta (ela as \
reconhece pelo código CNES), e cada unidade já vem com link_google_maps. Só chame \
gerar_link_mapa se o usuário disser que o link não mostrou o estabelecimento; neste caso, use usar_coordenadas=true e a lat/lon da unidade vinda de \
buscar_estabelecimentos, explicando que agora o link usa a posição cadastrada no CNES.

FORMATO DA RESPOSTA (português, curta e direta)
- No máximo 5 unidades, numeradas, cada uma assim:
  1. **NOME DA UNIDADE** (CNES 1234567): endereço, bairro. Telefone: ... Turno: ... (~1,2 km) \
[Ver no Google Maps](link_google_maps)
- Sempre que mostrar o endereço de uma unidade (na lista ou em detalhes), coloque junto o \
link_google_maps dela, copiado exatamente como veio da ferramenta.
- Use apenas nomes, endereços, telefones, códigos e links que vieram das ferramentas. Nunca \
invente nem complete dados. Se o telefone não veio, escreva "telefone não informado".
- Copie o telefone e o link_google_maps exatamente como vieram, dígito por dígito e caractere \
por caractere. Não reformate, não corrija, não tire nem acrescente dígitos e não reescreva o link. \
Se a unidade vier com telefone_aviso, mostre o número como está e diga que ele parece estar \
errado no cadastro.
- Sempre que listar unidades, termine com o aviso: os dados são do cadastro CNES, que não \
informa vaga, fila nem plantão do dia; confirme por telefone antes de ir. Em emergência, ligue 192.
- Ao listar serviços especializados, diga que no SUS o atendimento especializado geralmente \
exige encaminhamento da UBS (posto de saúde): o primeiro passo é procurar a UBS de referência.
- Quando mostrar distâncias, diga em uma frase que são aproximadas, em linha reta, e calculadas \
com a localização cadastrada no CNES, que pode ter erros (uma unidade conhecida pode estar mais \
perto do que parece).

LIMITES
- Não dê diagnóstico nem orientação médica: nada de remédios, doses, exames ou tratamentos. \
Se pedirem, diga que não pode ajudar com isso e ofereça buscar uma unidade de saúde.
- Assuntos fora de saúde: explique em uma frase o que você faz.
"""


@dataclass
class ChamadaFerramenta:
    nome: str
    argumentos: dict
    resultado: str  # o texto que voltou para o modelo
    segundos: float
    erro: bool = False


AVISO_CNES = (
    "Os dados são do cadastro CNES, que não informa vaga, fila nem plantão do dia. "
    "Confirme por telefone antes de ir."
)
AVISO_192 = "Em emergência, ligue 192 (SAMU)."


def completar_avisos(resposta: str) -> str:
    """Garante os avisos de segurança quando a resposta lista unidades.

    O prompt já pede os avisos, mas o modelo às vezes os esquece (principalmente em emergências).
    O que é obrigatório não fica só por conta dele: se faltar, o aplicativo acrescenta.
    """
    lista_unidades = re.search(r"\(CNES\D{0,3}\d{6,7}\)|^\s*\d+[.)]\s*\*\*", resposta, re.MULTILINE)
    if not lista_unidades:
        return resposta
    texto = resposta.lower()
    tem_confirmacao = re.search(r"\blig(ue|ar) antes\b", texto) or ("confirm" in texto and "telefone" in texto)
    faltando = ([] if tem_confirmacao else [AVISO_CNES]) + ([] if "192" in resposta else [AVISO_192])
    return resposta + ("\n\n" + " ".join(faltando) if faltando else "")


@dataclass
class Turno:
    """Tudo o que aconteceu em um turno (para a interface e para a avaliação)."""

    pergunta: str
    resposta: str = ""
    chamadas: list[ChamadaFerramenta] = field(default_factory=list)
    tokens_entrada: int = 0
    tokens_saida: int = 0
    chamadas_modelo: int = 0
    segundos_modelo: float = 0.0
    segundos_total: float = 0.0
    modelo: str = MODELO

    @property
    def segundos_ferramentas(self) -> float:
        return sum(c.segundos for c in self.chamadas)

    @property
    def custo_estimado_usd(self) -> float | None:
        if self.modelo not in PRECOS:
            return None
        preco_entrada, preco_saida = PRECOS[self.modelo]
        return (self.tokens_entrada * preco_entrada + self.tokens_saida * preco_saida) / 1_000_000


async def listar_ferramentas(mcp: Client) -> list[dict]:
    """Converte as ferramentas do MCP para o formato que a Messages API espera."""
    resultado = await mcp.list_tools()
    return [
        {"name": f.name, "description": f.description or "", "input_schema": f.input_schema}
        for f in resultado.tools
    ]


async def chamar_ferramenta(mcp: Client, nome: str, argumentos: dict) -> ChamadaFerramenta:
    """Executa uma ferramenta no servidor MCP e mede o tempo."""
    inicio = time.perf_counter()
    try:
        resultado = await mcp.call_tool(nome, argumentos)
        texto = "\n".join(bloco.text for bloco in resultado.content if bloco.type == "text")
        erro = resultado.is_error
    except Exception as excecao:  # servidor fora do ar, timeout etc.
        texto, erro = f"Falha ao chamar a ferramenta {nome}: {excecao}", True

    # O servidor devolve JSON indentado; compactar economiza tokens.
    try:
        texto = json.dumps(json.loads(texto), ensure_ascii=False, separators=(",", ":"))
    except json.JSONDecodeError:
        pass
    return ChamadaFerramenta(nome, argumentos, texto, time.perf_counter() - inicio, erro)


def extrair_texto(conteudo) -> str:
    """Junta os blocos de texto da resposta (ignora blocos de raciocínio e de ferramenta)."""
    return "\n".join(bloco.text for bloco in conteudo if bloco.type == "text").strip()


async def responder(historico: list, pergunta: str, modelo: str = MODELO, servidor=MCP_URL, claude=None) -> Turno:
    """Executa um turno completo da conversa.

    `historico` é a lista de mensagens no formato da Messages API. Ela é atualizada
    no lugar (só acrescentamos mensagens, nunca editamos as antigas).
    `servidor` é a URL do MCP; nos testes pode ser o próprio objeto MCPServer.
    `claude` é o cliente da Anthropic; nos testes pode ser um cliente falso.
    """
    if claude is None:
        # Cliente criado aqui é fechado ao fim do turno, ainda dentro do event loop.
        # (Sem isso, ele seria fechado depois, com o loop do asyncio.run já encerrado.)
        async with anthropic.AsyncAnthropic() as novo_cliente:
            return await responder(historico, pergunta, modelo, servidor, novo_cliente)

    turno = Turno(pergunta=pergunta, modelo=modelo)
    inicio = time.perf_counter()
    historico.append({"role": "user", "content": pergunta})

    async with Client(servidor) as mcp:
        ferramentas = await listar_ferramentas(mcp)

        while True:
            # Teto de chamadas atingido: obrigamos o modelo a responder só com texto.
            if len(turno.chamadas) >= MAX_CHAMADAS_POR_TURNO:
                escolha = {"type": "none"}
            else:
                escolha = {"type": "auto"}

            t0 = time.perf_counter()
            resposta = await claude.messages.create(
                model=modelo,
                max_tokens=MAX_TOKENS,
                system=PROMPT_SISTEMA,
                tools=ferramentas,
                tool_choice=escolha,
                output_config={"effort": ESFORCO},
                messages=historico,
            )
            turno.segundos_modelo += time.perf_counter() - t0
            turno.chamadas_modelo += 1
            turno.tokens_entrada += resposta.usage.input_tokens
            turno.tokens_saida += resposta.usage.output_tokens

            if resposta.stop_reason != "tool_use":
                break  # end_turn (ou max_tokens / refusal): terminou o turno

            # Guardamos a resposta INTEIRA (inclui blocos de raciocínio e tool_use).
            historico.append({"role": "assistant", "content": resposta.content})

            # Executa cada ferramenta pedida e devolve todos os resultados numa só mensagem.
            resultados = []
            for bloco in resposta.content:
                if bloco.type != "tool_use":
                    continue
                if len(turno.chamadas) >= MAX_CHAMADAS_POR_TURNO:
                    resultados.append({
                        "type": "tool_result",
                        "tool_use_id": bloco.id,
                        "content": "Limite de chamadas de ferramenta deste turno atingido. Responda com o que já tem.",
                        "is_error": True,
                    })
                    continue
                chamada = await chamar_ferramenta(mcp, bloco.name, bloco.input)
                turno.chamadas.append(chamada)
                resultados.append({
                    "type": "tool_result",
                    "tool_use_id": bloco.id,
                    "content": chamada.resultado,
                    "is_error": chamada.erro,
                })
            historico.append({"role": "user", "content": resultados})

    turno.resposta = extrair_texto(resposta.content)
    if resposta.stop_reason == "refusal" or not turno.resposta:
        turno.resposta = "Desculpe, não posso ajudar com esse pedido. Posso buscar unidades de saúde perto de um CEP."
        historico.append({"role": "assistant", "content": turno.resposta})
    else:
        historico.append({"role": "assistant", "content": resposta.content})
    if resposta.stop_reason == "max_tokens":
        turno.resposta += "\n\n(Resposta interrompida por exceder o limite de tamanho.)"
    turno.resposta = completar_avisos(turno.resposta)

    turno.segundos_total = time.perf_counter() - inicio
    return turno


def responder_sincrono(historico: list, pergunta: str, modelo: str = MODELO) -> Turno:
    """Versão síncrona de `responder`, para quem não usa asyncio (Streamlit, scripts)."""
    return asyncio.run(responder(historico, pergunta, modelo))


def servidor_no_ar() -> bool:
    """Confere se o servidor MCP local está rodando (GET /health)."""
    try:
        return httpx.get(MCP_URL.removesuffix("/mcp") + "/health", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


def imprimir_turno(turno: Turno) -> None:
    print(f"\n>>> {turno.pergunta}")
    for chamada in turno.chamadas:
        print(f"  [ferramenta] {chamada.nome}({json.dumps(chamada.argumentos, ensure_ascii=False)}) "
              f"-> {len(chamada.resultado)} caracteres em {chamada.segundos:.1f} s")
    print(f"\n{turno.resposta}\n")
    custo = turno.custo_estimado_usd
    print(f"  tokens: {turno.tokens_entrada} entrada / {turno.tokens_saida} saída"
          f"{f' (~US$ {custo:.4f})' if custo is not None else ''} | "
          f"tempo: {turno.segundos_total:.1f} s (modelo {turno.segundos_modelo:.1f} s, "
          f"ferramentas {turno.segundos_ferramentas:.1f} s)")


if __name__ == "__main__":
    if not servidor_no_ar():
        sys.exit(f"Servidor MCP fora do ar em {MCP_URL}. Rode antes: uv run uvicorn servidor_mcp.app:app")

    perguntas = sys.argv[1:] or [
        "Moro no CEP 90570-001, onde posso tomar vacina?",
        "Onde tem posto de saúde?",
        "CEP 91350-200, preciso de um pronto atendimento",
    ]
    print(f"Modelo: {MODELO} | esforço: {ESFORCO} | MCP: {MCP_URL}")
    for pergunta in perguntas:
        imprimir_turno(responder_sincrono([], pergunta))  # cada pergunta numa conversa nova
