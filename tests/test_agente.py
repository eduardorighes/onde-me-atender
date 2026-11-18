"""Testes do loop do agente, sem rede: um "Claude falso" com respostas roteirizadas
conversa com o servidor MCP de verdade, rodando no mesmo processo."""

import asyncio
from types import SimpleNamespace

from cliente.agente import AVISO_192, AVISO_CNES, MAX_CHAMADAS_POR_TURNO, completar_avisos, responder
from servidor_mcp.ferramentas import mcp
from test_ferramentas import api_falsa  # noqa: F401  (fixture com o CNES simulado)


def texto(conteudo):
    return SimpleNamespace(type="text", text=conteudo)


def uso_de_ferramenta(id_, nome, argumentos):
    return SimpleNamespace(type="tool_use", id=id_, name=nome, input=argumentos)


def resposta(blocos, stop_reason):
    return SimpleNamespace(
        content=blocos, stop_reason=stop_reason, usage=SimpleNamespace(input_tokens=100, output_tokens=10)
    )


class ClaudeFalso:
    """Imita `AsyncAnthropic().messages.create`, devolvendo respostas pré-definidas."""

    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.pedidos = []
        self.messages = self

    async def create(self, **kwargs):
        self.pedidos.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.respostas.pop(0)


def test_loop_tool_use_tool_result_end_turn(api_falsa):  # noqa: F811
    claude = ClaudeFalso([
        resposta([uso_de_ferramenta("t1", "listar_tipos_unidade", {"busca": "pronto"})], "tool_use"),
        resposta([uso_de_ferramenta("t2", "buscar_estabelecimentos", {"cep": "90570-001", "codigos_tipo_unidade": [73]})], "tool_use"),
        resposta([texto("1. **PRONTO ATENDIMENTO CRUZEIRO DO SUL** ... confirme por telefone.")], "end_turn"),
    ])
    historico = []

    turno = asyncio.run(responder(historico, "Pronto atendimento em Porto Alegre", servidor=mcp, claude=claude))

    assert [c.nome for c in turno.chamadas] == ["listar_tipos_unidade", "buscar_estabelecimentos"]
    assert "PRONTO ATENDIMENTO" in turno.chamadas[1].resultado  # resultado real da ferramenta
    assert turno.resposta.startswith("1. **PRONTO ATENDIMENTO")
    assert turno.tokens_entrada == 300 and turno.chamadas_modelo == 3

    # As ferramentas foram oferecidas ao modelo no formato da Messages API.
    nomes = {f["name"] for f in claude.pedidos[0]["tools"]}
    assert nomes == {
        "listar_tipos_unidade",
        "buscar_estabelecimentos",
        "detalhar_estabelecimento",
        "servicos_do_estabelecimento",
        "buscar_por_servico",
        "gerar_link_mapa",
    }

    # Histórico: user, assistant(tool_use), user(tool_result), assistant(tool_use), user(tool_result), assistant(texto)
    assert [m["role"] for m in historico] == ["user", "assistant", "user", "assistant", "user", "assistant"]
    resultado = historico[2]["content"][0]
    assert resultado["type"] == "tool_result" and resultado["tool_use_id"] == "t1"


def test_teto_de_chamadas_por_turno(api_falsa):  # noqa: F811
    # O modelo falso pede 10 ferramentas de uma vez; só 8 podem rodar.
    pedidos = [uso_de_ferramenta(f"t{i}", "listar_tipos_unidade", {"busca": "posto"}) for i in range(10)]
    claude = ClaudeFalso([resposta(pedidos, "tool_use"), resposta([texto("Pronto.")], "end_turn")])

    turno = asyncio.run(responder([], "teste", servidor=mcp, claude=claude))

    assert len(turno.chamadas) == MAX_CHAMADAS_POR_TURNO
    resultados = claude.pedidos[1]["messages"][-1]["content"]
    assert len(resultados) == 10  # todo tool_use precisa de um tool_result
    assert sum(r["is_error"] for r in resultados) == 2
    assert claude.pedidos[1]["tool_choice"] == {"type": "none"}  # força a resposta em texto


def test_recusa_vira_mensagem_amigavel(api_falsa):  # noqa: F811
    claude = ClaudeFalso([resposta([], "refusal")])
    historico = []

    turno = asyncio.run(responder(historico, "...", servidor=mcp, claude=claude))

    assert "não posso ajudar" in turno.resposta
    assert historico[-1] == {"role": "assistant", "content": turno.resposta}


# --- Avisos de segurança garantidos pelo aplicativo ------------------------------------

LISTA = "1. **PRONTO ATENDIMENTO BOM JESUS** (CNES 6537650): Rua Bom Jesus, 410. Telefone: 51 33681626."


def test_avisos_sao_acrescentados_quando_a_lista_vem_sem_eles():
    resposta = completar_avisos("Ligue 192 agora.\n\n" + LISTA)
    assert resposta.endswith(AVISO_CNES)  # o 192 já estava na resposta
    assert completar_avisos(LISTA).endswith(f"{AVISO_CNES} {AVISO_192}")


def test_avisos_nao_sao_repetidos():
    completa = LISTA + "\n\nConfirme por telefone antes de ir. Em emergência, ligue 192."
    assert completar_avisos(completa) == completa
    outra_forma = LISTA + "\n\nLigue antes de ir. Em emergência, ligue 192."
    assert completar_avisos(outra_forma) == outra_forma


def test_resposta_sem_unidades_fica_como_esta():
    pedido = "Qual é o seu CEP? Preciso dos 8 dígitos para buscar."
    assert completar_avisos(pedido) == pedido
