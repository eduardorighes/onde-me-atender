"""Servidor FastAPI com duas "portas" para as mesmas ferramentas:

1. MCP (streamable HTTP) em http://localhost:8000/mcp  -> usado pelo agente (Streamlit).
2. Rotas REST de depuração em /api/...                  -> para testar pelo /docs.

Rodar:  uv run uvicorn servidor_mcp.app:app --reload
"""

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Query

from servidor_mcp.cep import ErroCEP, consultar_cep
from servidor_mcp.ferramentas import (
    FONTE_CEP,
    buscar_estabelecimentos,
    buscar_por_servico,
    detalhar_estabelecimento,
    gerar_link_mapa,
    listar_tipos_unidade,
    mcp,
    servicos_do_estabelecimento,
)

# O SDK do MCP cria uma mini-aplicação ASGI (Starlette) que já atende no caminho "/mcp".
# Vamos montá-la na RAIZ do FastAPI: se a montássemos em "/mcp", o endpoint
# ficaria em "/mcp/mcp" (o caminho duplicado).
app_mcp = mcp.streamable_http_app()


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI):
    # O "session manager" do MCP precisa estar rodando enquanto o servidor estiver no ar.
    # Quando o app MCP é montado dentro do FastAPI, o lifespan dele não roda sozinho,
    # então o iniciamos aqui.
    async with mcp.session_manager.run():
        yield


app = FastAPI(
    title="Onde me atender: servidor MCP",
    description="Ferramentas do CNES expostas via MCP (/mcp) e via REST (/api) para depuração.",
    lifespan=ciclo_de_vida,
)


@app.get("/health", tags=["depuração"])
def health():
    return {"status": "ok", "mcp": "/mcp", "ferramentas": 6}


@app.get("/api/cep/{cep}", tags=["depuração"])
def rota_consultar_cep(cep: str):
    # Não é ferramenta do agente: mostra o que buscar_estabelecimentos descobre sobre o CEP.
    try:
        return {**consultar_cep(cep), "fonte": FONTE_CEP}
    except ErroCEP as erro:
        return {"erro": str(erro), "fonte": FONTE_CEP}


@app.get("/api/tipos-unidade", tags=["depuração"])
def rota_listar_tipos_unidade(busca: str | None = None):
    return listar_tipos_unidade(busca)


@app.get("/api/estabelecimentos", tags=["depuração"])
def rota_buscar_estabelecimentos(
    cep: str,
    codigos_tipo_unidade: Annotated[list[int] | None, Query()] = None,
    apenas_sus: bool = True,
    max_resultados: int = 5,
):
    return buscar_estabelecimentos(cep, codigos_tipo_unidade, apenas_sus, max_resultados)


@app.get("/api/estabelecimentos/{codigo_cnes}", tags=["depuração"])
def rota_detalhar_estabelecimento(codigo_cnes: str):
    return detalhar_estabelecimento(codigo_cnes)


@app.get("/api/estabelecimentos/{codigo_cnes}/servicos", tags=["depuração"])
def rota_servicos_do_estabelecimento(codigo_cnes: str):
    return servicos_do_estabelecimento(codigo_cnes)


@app.get("/api/servicos", tags=["depuração"])
def rota_buscar_por_servico(cep: str, servico: str, apenas_sus: bool = True, max_resultados: int = 5):
    return buscar_por_servico(cep, servico, apenas_sus, max_resultados)


@app.get("/api/mapa", tags=["depuração"])
def rota_gerar_link_mapa(
    endereco: str = "",
    nome: str | None = None,
    usar_coordenadas: bool = False,
    lat: float | None = None,
    lon: float | None = None,
):
    return gerar_link_mapa(endereco, nome, usar_coordenadas, lat, lon)


# Por último: tudo que não casou com as rotas acima (ou seja, /mcp) vai para o app MCP.
app.mount("/", app_mcp)
