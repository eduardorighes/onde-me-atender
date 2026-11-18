"""A mesma avaliação de avaliar.py, chamando o modelo pelo Claude Code em vez da API da Anthropic.

Usa o login do Claude Code (`claude -p`, modo headless), sem a ANTHROPIC_API_KEY. O prompt de
sistema, o modelo, o esforço, as ferramentas do servidor MCP e as métricas são os mesmos. Muda:

- o laço de chamadas é o do Claude Code, não o de cliente/agente.py (o teto de chamadas por turno
  não é aplicado; o script conta quantos turnos passariam dele);
- a latência inclui a partida do `claude -p`;
- o custo é uma estimativa pelo preço de tabela sobre todos os tokens de entrada (inclusive os
  lidos do cache), não um valor cobrado.

    uv run python avaliacao/avaliar_cli.py --modelo claude-haiku-5-5 --sufixo claude-haiku-5-5_cli

Aceita as mesmas opções de avaliar.py, menos --juiz (o juiz usa a API).
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

import avaliar
from avaliar import MCP_URL, ler_argumentos, principal

from cliente.agente import (
    ESFORCO,
    MAX_CHAMADAS_POR_TURNO,
    PROMPT_SISTEMA,
    ChamadaFerramenta,
    Turno,
    completar_avisos,
)

SERVIDOR = "onde"  # nome do servidor MCP na configuração; as ferramentas viram mcp__onde__<nome>
RECUSA = "Desculpe, não posso ajudar com esse pedido. Posso buscar unidades de saúde perto de um CEP."

# Sem a chave da API e sem as variáveis da sessão do Claude Code que estiver rodando este script.
AMBIENTE = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY" and not k.startswith("CLAUDE")}

turnos_acima_do_teto = 0


def preparar_pasta() -> Path:
    """Pasta temporária com o prompt e a configuração do MCP (longe do CLAUDE.md do projeto)."""
    pasta = Path(tempfile.mkdtemp(prefix="onde-me-atender-"))
    (pasta / "prompt.txt").write_text(PROMPT_SISTEMA, encoding="utf-8")
    (pasta / "mcp.json").write_text(json.dumps({"mcpServers": {SERVIDOR: {"type": "http", "url": MCP_URL}}}))
    return pasta


PASTA_CLI = preparar_pasta()


async def rodar_claude(pergunta: str, modelo: str, sessao: str, continuar: bool) -> tuple[list[dict], dict]:
    """Um turno no `claude -p`. Devolve os eventos (stream-json) e o evento final."""
    comando = [
        "claude", "-p", pergunta, "--model", modelo, "--effort", ESFORCO,
        "--system-prompt-file", "prompt.txt",
        "--mcp-config", "mcp.json", "--strict-mcp-config",
        "--tools", "", "--allowedTools", f"mcp__{SERVIDOR}",  # só as ferramentas do nosso servidor
        "--setting-sources", "",
        "--output-format", "stream-json", "--verbose",
        *(["--resume", sessao] if continuar else ["--session-id", sessao]),
    ]
    processo = await asyncio.create_subprocess_exec(
        *comando, cwd=PASTA_CLI, env=AMBIENTE, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    saida, erro = await processo.communicate()
    eventos = [json.loads(linha) for linha in saida.decode().splitlines() if linha.startswith("{")]
    final = next((e for e in eventos if e.get("type") == "result"), None)
    if final is None or final.get("is_error"):
        raise RuntimeError(f"claude -p falhou: {(final or {}).get('result') or erro.decode()[:300]}")
    return eventos, final


def chamadas_do_turno(eventos: list[dict]) -> list[ChamadaFerramenta]:
    """Casa cada pedido de ferramenta (tool_use) com o seu resultado (tool_result)."""
    pedidos, chamadas = {}, []
    for evento in eventos:
        if evento.get("type") not in ("assistant", "user"):
            continue
        for bloco in evento["message"]["content"]:
            if not isinstance(bloco, dict):
                continue
            if bloco["type"] == "tool_use":
                pedidos[bloco["id"]] = bloco
            elif bloco["type"] == "tool_result":
                pedido = pedidos[bloco["tool_use_id"]]
                conteudo = bloco.get("content") or ""
                texto = conteudo if isinstance(conteudo, str) else "\n".join(c.get("text", "") for c in conteudo)
                try:  # compacto, como em cliente/agente.py
                    texto = json.dumps(json.loads(texto), ensure_ascii=False, separators=(",", ":"))
                except json.JSONDecodeError:
                    pass
                nome = pedido["name"].removeprefix(f"mcp__{SERVIDOR}__")
                chamadas.append(ChamadaFerramenta(nome, pedido["input"], texto, 0.0, bool(bloco.get("is_error"))))
    return chamadas


async def responder(historico: list, pergunta: str, modelo: str) -> Turno:
    """Mesmo contrato de cliente.agente.responder; `historico` guarda só o id da sessão do Claude Code."""
    global turnos_acima_do_teto
    continuar = bool(historico)
    if not continuar:
        historico.append(str(uuid.uuid4()))

    inicio = time.perf_counter()
    eventos, final = await rodar_claude(pergunta, modelo, historico[0], continuar)
    turno = Turno(pergunta=pergunta, modelo=modelo, segundos_total=time.perf_counter() - inicio)

    partida = next(e for e in eventos if e.get("subtype") == "init")
    if partida["apiKeySource"] != "none" or partida["model"] != modelo:
        raise RuntimeError(f"sessão inesperada: chave {partida['apiKeySource']}, modelo {partida['model']}")

    turno.chamadas = chamadas_do_turno(eventos)
    turnos_acima_do_teto += len(turno.chamadas) > MAX_CHAMADAS_POR_TURNO
    uso = final["usage"]
    turno.tokens_entrada = (
        uso["input_tokens"] + uso.get("cache_creation_input_tokens", 0) + uso.get("cache_read_input_tokens", 0)
    )
    turno.tokens_saida = uso["output_tokens"]
    turno.chamadas_modelo = final.get("num_turns", 0)
    turno.segundos_modelo = final.get("duration_api_ms", 0) / 1000
    # Mesmo pós-processamento do aplicativo: os avisos de segurança não dependem só do modelo.
    turno.resposta = completar_avisos((final.get("result") or "").strip() or RECUSA)
    return turno


if __name__ == "__main__":
    args = ler_argumentos("Avaliação do agente pelo Claude Code headless (sem a ANTHROPIC_API_KEY).")
    if args.juiz:
        sys.exit("--juiz usa a API da Anthropic; rode sem essa opção.")
    avaliar.responder = responder  # troca só quem chama o modelo
    asyncio.run(principal(args))
    print(f"  turnos acima do teto de {MAX_CHAMADAS_POR_TURNO} chamadas do aplicativo: {turnos_acima_do_teto}")
