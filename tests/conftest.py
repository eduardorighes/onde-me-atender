"""Configuração comum dos testes: nada de rede, cache isolado e sem esperas."""

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from servidor_mcp import cache as modulo_cache
from servidor_mcp import cep, cnes, ferramentas

PASTA_FIXTURES = Path(__file__).parent / "fixtures"


def carregar_fixture(nome: str):
    return json.loads((PASTA_FIXTURES / nome).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def ambiente_isolado(tmp_path, monkeypatch):
    # Cada teste usa um cache novo numa pasta temporária.
    novo_cache = modulo_cache.Cache(pasta=tmp_path / "cache")
    monkeypatch.setattr(cnes, "cache", novo_cache)
    monkeypatch.setattr(cep, "cache", novo_cache)
    # Retry sem esperar 1 s, 2 s... para os testes serem rápidos.
    monkeypatch.setattr(cnes, "ESPERA_INICIAL", 0)
    # Nominatim pede 1 consulta por segundo; nos testes não há por que esperar.
    monkeypatch.setattr(cep, "_ultima_consulta_nominatim", float("-inf"))


LOCAL_POA = {  # o que consultar_cep devolve para o CEP 90570-001
    "cep": "90570-001",
    "logradouro": "Rua Félix da Cunha",
    "bairro": "Moinhos de Vento",
    "cidade": "Porto Alegre",
    "uf": "RS",
    "codigo_ibge_7": "4314902",
    "codigo_municipio": "431490",
    "lat": -30.0209,
    "lon": -51.2021,
    "fonte_coordenadas": "OpenStreetMap (rua)",
}


@pytest.fixture
def cep_falso(monkeypatch):
    """As ferramentas consultam o CEP sem rede: todo CEP válido é o LOCAL_POA.

    Devolve o dict usado: o teste pode alterá-lo (ex.: trocar o bairro ou as coordenadas).
    """
    local = dict(LOCAL_POA)

    def consultar(numero):
        cep.normalizar_cep(numero)  # CEP mal formado continua dando ErroCEP
        return dict(local)

    monkeypatch.setattr(ferramentas, "consultar_cep", consultar)
    return local


def cliente_falso(responder) -> httpx.Client:
    """Cria um httpx.Client cujas respostas vêm da função `responder(request)`.

    Também registra todas as requisições em `cliente.pedidos` para conferência.
    """
    pedidos = []

    def manipulador(request: httpx.Request) -> httpx.Response:
        pedidos.append(request)
        return responder(request)

    cliente = httpx.Client(transport=httpx.MockTransport(manipulador))
    cliente.pedidos = pedidos
    return cliente


class DataFixa(date):
    """'Hoje' fixo em 14/11/2026, para a busca da competência não depender do dia do teste."""

    @classmethod
    def today(cls):
        return cls(2026, 11, 14)


def responder_servicos(request: httpx.Request) -> httpx.Response:
    """Imita a rota de serviços especializados com os registros reais da fixture.

    Reproduz o comportamento real: só há dados na competência 202607, e cada resposta
    traz SÓ o primeiro registro da página (o de posição offset x limit).
    """
    if "/cnes/estabelecimentos/" in request.url.path:
        return httpx.Response(404, json={"message": "Estabelecimento not found."})
    params = request.url.params
    registros = [
        r for r in carregar_fixture("servicos_poa_amostra.json")["registros"]
        if str(r["nu_comp"]) == params.get("nu_comp")
        and params.get("co_cnes", r["co_cnes"]) == r["co_cnes"]
        and params.get("co_ibge", str(r["co_ibge"])) == str(r["co_ibge"])
    ]
    posicao = int(params.get("offset", 0)) * int(params.get("limit", 1))
    lote = registros[posicao:posicao + 1]
    return httpx.Response(200, json={"cnes_servicos_especializados": lote})


@pytest.fixture
def api_servicos(monkeypatch, cep_falso):
    """Faz todo httpx.Client criado pelos módulos falar com `responder_servicos`."""
    monkeypatch.setattr(cnes, "date", DataFixa)
    cliente_original = httpx.Client
    pedidos = []

    def cliente_simulado(**kwargs):
        def manipulador(request):
            pedidos.append(request)
            return responder_servicos(request)

        return cliente_original(transport=httpx.MockTransport(manipulador))

    monkeypatch.setattr(httpx, "Client", cliente_simulado)
    return pedidos
