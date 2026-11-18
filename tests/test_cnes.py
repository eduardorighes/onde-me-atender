"""Testes do cliente CNES e da consulta de CEP, usando respostas reais salvas em fixtures."""

import httpx
import pytest

from conftest import carregar_fixture, cliente_falso
from servidor_mcp import cep, cnes

# --- Conversão do código do município -------------------------------------


def test_converte_7_digitos_para_6():
    assert cnes.converter_codigo_municipio("4314902") == "431490"
    assert cnes.converter_codigo_municipio(4314902) == "431490"


def test_6_digitos_passa_intacto():
    assert cnes.converter_codigo_municipio("431490") == "431490"


@pytest.mark.parametrize("invalido", ["", "43149", "43149021", "abc123", "4314-02"])
def test_codigo_invalido_gera_erro_claro(invalido):
    with pytest.raises(ValueError, match="6 ou 7 dígitos"):
        cnes.converter_codigo_municipio(invalido)


# --- Paginação ---------------------------------------------------------------


def test_paginacao_usa_offset_como_pagina_e_para_na_pagina_vazia():
    pagina_cheia = carregar_fixture("estabelecimentos_poa_tipo2_pagina0.json")
    assert len(pagina_cheia["estabelecimentos"]) == 20

    def responder(request):
        pagina = int(request.url.params["offset"])
        if pagina < 2:
            return httpx.Response(200, json=pagina_cheia)
        return httpx.Response(200, json={"estabelecimentos": []})

    cliente = cliente_falso(responder)
    registros, completo = cnes.paginar_estabelecimentos({"codigo_municipio": "431490"}, cliente)

    assert completo is True
    assert len(registros) == 40
    offsets = [int(p.url.params["offset"]) for p in cliente.pedidos]
    assert offsets == [0, 1, 2]  # página 0, 1 e a 2 vazia (fim)
    assert all(int(p.url.params["limit"]) <= 20 for p in cliente.pedidos)


def test_paginacao_para_em_pagina_incompleta():
    pagina_curta = carregar_fixture("estabelecimentos_poa_tipo73.json")  # 7 registros reais
    cliente = cliente_falso(lambda request: httpx.Response(200, json=pagina_curta))

    registros, completo = cnes.paginar_estabelecimentos({"codigo_municipio": "431490"}, cliente)

    assert completo is True
    assert len(registros) == 7
    assert len(cliente.pedidos) == 1  # não precisou pedir a página seguinte


def test_paginacao_respeita_teto_de_paginas():
    pagina_cheia = carregar_fixture("estabelecimentos_poa_tipo2_pagina0.json")
    cliente = cliente_falso(lambda request: httpx.Response(200, json=pagina_cheia))

    registros, completo = cnes.paginar_estabelecimentos({}, cliente, max_paginas=3)

    assert completo is False
    assert len(registros) == 60
    assert len(cliente.pedidos) == 3


def test_listar_estabelecimentos_converte_codigo_e_usa_cache():
    pagina = carregar_fixture("estabelecimentos_poa_tipo73.json")
    cliente = cliente_falso(lambda request: httpx.Response(200, json=pagina))

    registros, _ = cnes.listar_estabelecimentos("4314902", 73, cliente)
    pedido = cliente.pedidos[0]
    assert pedido.url.params["codigo_municipio"] == "431490"  # 7 -> 6 dígitos
    assert pedido.url.params["codigo_tipo_unidade"] == "73"
    assert pedido.url.params["status"] == "1"
    assert set(registros[0]) == set(cnes.CAMPOS_UTEIS)  # só os campos úteis

    # Segunda chamada (agora com 6 dígitos) vem do cache: nenhuma requisição nova.
    cnes.listar_estabelecimentos("431490", 73, cliente)
    assert len(cliente.pedidos) == 1


# --- Erros e retry -------------------------------------------------------------


def test_erro_5xx_faz_retry_e_depois_mensagem_amigavel():
    cliente = cliente_falso(lambda request: httpx.Response(503, text="Service Unavailable"))

    with pytest.raises(cnes.ErroCNES, match="não respondeu após 3 tentativas"):
        cnes.listar_tipos_unidade(cliente)
    assert len(cliente.pedidos) == 3


def test_erro_5xx_seguido_de_sucesso():
    tipos = carregar_fixture("tipounidades.json")
    respostas = iter([httpx.Response(502), httpx.Response(200, json=tipos)])
    cliente = cliente_falso(lambda request: next(respostas))

    resultado = cnes.listar_tipos_unidade(cliente)

    assert len(cliente.pedidos) == 2
    assert {"codigo": 85, "descricao": "CENTRO DE IMUNIZACAO"} in resultado


def test_estabelecimento_inexistente_devolve_none():
    cliente = cliente_falso(lambda request: httpx.Response(404, json={"message": "Estabelecimento not found."}))
    assert cnes.obter_estabelecimento("9999999", cliente) is None


def test_detalhe_de_estabelecimento():
    detalhe = carregar_fixture("estabelecimento_2237571.json")
    cliente = cliente_falso(lambda request: httpx.Response(200, json=detalhe))

    dados = cnes.obter_estabelecimento(2237571, cliente)

    assert dados["nome_fantasia"] == "HOSPITAL NOSSA SENHORA DA CONCEICAO"
    assert cliente.pedidos[0].url.path == "/cnes/estabelecimentos/2237571"


# --- CEP ---------------------------------------------------------------------


def responder_cep(nominatim_ok=True, brasilapi_ok=True):
    """Simula ViaCEP, Nominatim e BrasilAPI com as respostas reais salvas."""

    def responder(request):
        if "viacep" in request.url.host:
            return httpx.Response(200, json=carregar_fixture("viacep_90570001.json"))
        if "nominatim" in request.url.host:
            corpo = carregar_fixture("nominatim_felix_da_cunha.json") if nominatim_ok else []
            return httpx.Response(200, json=corpo)
        if brasilapi_ok:
            return httpx.Response(200, json=carregar_fixture("brasilapi_90570001.json"))
        return httpx.Response(404, json={"message": "CEP não encontrado"})

    return responder


def test_consultar_cep_porto_alegre():
    cliente = cliente_falso(responder_cep())
    resultado = cep.consultar_cep("90570-001", cliente)

    assert resultado["cidade"] == "Porto Alegre"
    assert resultado["bairro"] == "Moinhos de Vento"
    assert resultado["codigo_ibge_7"] == "4314902"
    assert resultado["codigo_municipio"] == "431490"
    # Coordenadas da rua (OpenStreetMap), em Moinhos de Vento.
    assert resultado["lat"] == pytest.approx(-30.0209, abs=0.001)
    assert resultado["lon"] == pytest.approx(-51.2021, abs=0.001)
    assert resultado["fonte_coordenadas"] == "OpenStreetMap (rua)"
    assert not any("brasilapi" in p.url.host for p in cliente.pedidos)  # não precisou


def test_cep_sem_rua_no_osm_usa_centro_do_municipio_da_brasilapi():
    resultado = cep.consultar_cep("90570001", cliente_falso(responder_cep(nominatim_ok=False)))

    assert resultado["lat"] == pytest.approx(-30.03283)
    assert resultado["lon"] == pytest.approx(-51.23019)
    assert resultado["fonte_coordenadas"] == "BrasilAPI (centro do município)"


def test_cep_sem_coordenadas_devolve_none():
    responder = responder_cep(nominatim_ok=False, brasilapi_ok=False)
    resultado = cep.consultar_cep("90570001", cliente_falso(responder))
    assert resultado["lat"] is None and resultado["lon"] is None
    assert resultado["fonte_coordenadas"] is None


def test_cep_inexistente():
    cliente = cliente_falso(lambda request: httpx.Response(200, json={"erro": "true"}))
    with pytest.raises(cep.ErroCEP, match="não encontrado"):
        cep.consultar_cep("00000-000", cliente)


@pytest.mark.parametrize("invalido", ["123", "9057000", "abcdefgh", ""])
def test_cep_mal_formado(invalido):
    with pytest.raises(cep.ErroCEP, match="8 dígitos"):
        cep.normalizar_cep(invalido)


def test_rua_longa_usa_o_trecho_do_mesmo_cep():
    # Rua Doutor Barcelos (Porto Alegre) tem trechos em Cavalhada, Camaquã e Tristeza.
    # Colocamos de propósito o trecho errado (Cavalhada) em primeiro lugar.
    trechos = carregar_fixture("nominatim_doutor_barcelos.json")
    trechos.sort(key=lambda t: t["address"]["postcode"] != "91740-001")

    def responder(request):
        if "viacep" in request.url.host:
            return httpx.Response(200, json=carregar_fixture("viacep_91910251.json"))
        return httpx.Response(200, json=trechos)

    resultado = cep.consultar_cep("91910-251", cliente_falso(responder))

    assert resultado["bairro"] == "Tristeza"
    assert resultado["lat"] == pytest.approx(-30.1067, abs=0.001)
    assert resultado["lon"] == pytest.approx(-51.2522, abs=0.001)


def test_sem_trecho_com_o_cep_usa_o_mesmo_bairro():
    trechos = [t for t in carregar_fixture("nominatim_doutor_barcelos.json") if t["address"]["postcode"] != "91910-251"]
    trechos.sort(key=lambda t: "Tristeza" in t["display_name"])  # trechos da Tristeza por último

    escolhido = cep.escolher_trecho(trechos, "91910251", "Tristeza")

    assert "Tristeza" in escolhido["display_name"]


def test_endereco_do_cnes_procurado_so_perto_do_usuario():
    corpo = [{"lat": "-30.0256264", "lon": "-51.2079724", "display_name": "Hospital Moinhos de Vento, 910, Rua Ramiro Barcelos"}]
    cliente = cliente_falso(lambda request: httpx.Response(200, json=corpo))

    lat, lon = cep.coordenadas_do_endereco("RUA RAMIRO BARCELOS 910", -30.0217, -51.2061, cliente)

    assert (lat, lon) == (pytest.approx(-30.0256, abs=0.001), pytest.approx(-51.2080, abs=0.001))
    parametros = cliente.pedidos[0].url.params
    assert parametros["q"] == "RUA RAMIRO BARCELOS 910"
    assert parametros["bounded"] == "1"  # descarta resultados fora da caixa
    oeste, norte, leste, sul = map(float, parametros["viewbox"].split(","))
    assert oeste < -51.2061 < leste and sul < -30.0217 < norte

    # Segunda consulta igual vem do cache.
    cep.coordenadas_do_endereco("RUA RAMIRO BARCELOS 910", -30.0217, -51.2061, cliente)
    assert len(cliente.pedidos) == 1


def test_endereco_nao_encontrado():
    cliente = cliente_falso(lambda request: httpx.Response(200, json=[]))
    assert cep.coordenadas_do_endereco("RUA QUE NAO EXISTE 1", -30.0, -51.2, cliente) == (None, None)


# --- Serviços especializados ----------------------------------------------------------------

from conftest import DataFixa, responder_servicos  # noqa: E402


def test_codigo_cnes_vai_com_zeros_a_esquerda():
    assert cnes.normalizar_codigo_cnes(622133) == "0622133"
    assert cnes.normalizar_codigo_cnes("2264382") == "2264382"
    with pytest.raises(ValueError, match="Código CNES inválido"):
        cnes.normalizar_codigo_cnes("12345678")


def test_competencia_mais_recente_volta_ate_achar_dados(monkeypatch):
    monkeypatch.setattr(cnes, "date", DataFixa)  # "hoje" = 11/2026
    cliente = cliente_falso(responder_servicos)

    assert cnes.competencia_mais_recente(cliente) == 202607
    competencias = [p.url.params["nu_comp"] for p in cliente.pedidos]
    assert competencias == ["202611", "202610", "202609", "202608", "202607"]

    cnes.competencia_mais_recente(cliente)  # segunda vez: cache, sem requisição nova
    assert len(cliente.pedidos) == 5


def test_contar_servicos_por_busca_binaria():
    cliente = cliente_falso(responder_servicos)
    # AESC Hospital Santa Ana: 47 registros na fixture.
    assert cnes.contar_servicos({"nu_comp": 202607, "co_cnes": "6295320"}, cliente) == 47
    assert len(cliente.pedidos) < 15  # bem menos que 47 requisições
    assert cnes.contar_servicos({"nu_comp": 202607, "co_cnes": "9999999"}, cliente) == 0


def test_listar_servicos_do_estabelecimento(monkeypatch):
    monkeypatch.setattr(cnes, "date", DataFixa)
    cliente = cliente_falso(responder_servicos)

    registros, competencia = cnes.listar_servicos(codigo_cnes=622133, cliente=cliente)  # sem o zero

    assert competencia == 202607
    assert [r["co_cnes"] for r in registros] == ["0622133"]
    assert set(registros[0]) == set(cnes.CAMPOS_SERVICO)
    assert all(int(p.url.params["limit"]) == 1 for p in cliente.pedidos if "nu_comp" in p.url.params)


def test_listar_servicos_do_municipio_le_um_registro_por_requisicao(monkeypatch):
    monkeypatch.setattr(cnes, "date", DataFixa)
    cliente = cliente_falso(responder_servicos)

    registros, _ = cnes.listar_servicos(codigo_municipio="4314902", cliente=cliente)  # 7 -> 6 dígitos

    assert len(registros) == 56  # todos os registros da fixture
    assert {p.url.params.get("co_ibge") for p in cliente.pedidos} == {None, "431490"}
    antes = len(cliente.pedidos)
    cnes.listar_servicos(codigo_municipio="431490", cliente=cliente)  # cache
    assert len(cliente.pedidos) == antes


def test_municipio_grande_demais_vira_mensagem(monkeypatch):
    monkeypatch.setattr(cnes, "date", DataFixa)
    monkeypatch.setattr(cnes, "MAX_REGISTROS_SERVICOS", 10)
    with pytest.raises(cnes.ErroCNES, match="56 registros"):
        cnes.listar_servicos(codigo_municipio="431490", cliente=cliente_falso(responder_servicos))
