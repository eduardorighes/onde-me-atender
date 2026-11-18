"""Testes do mapa embutido no chat (sem rede e sem Streamlit)."""

import json

from cliente.agente import ChamadaFerramenta, Turno
from cliente.mapa import CAMADA, COR_UNIDADE, COR_USUARIO, montar_mapa, ponto_clicado, pontos_do_mapa
from conftest import LOCAL_POA
from servidor_mcp.ferramentas import buscar_estabelecimentos
from test_ferramentas import api_falsa  # noqa: F401  (fixture com o CNES e o CEP simulados)

CEP = "90570-001"


def chamada(nome, resultado):
    return ChamadaFerramenta(nome, {}, json.dumps(resultado, ensure_ascii=False), 0.1)


def turno_com(resposta, busca):
    return Turno(pergunta="...", resposta=resposta, chamadas=[chamada("buscar_estabelecimentos", busca)])


def so_centro_do_municipio(busca):
    """A mesma busca, como viria de um CEP sem coordenadas da rua (`local.lat` null)."""
    return {**busca, "local": {**busca["local"], "lat": None, "lon": None}}


def test_mapa_tem_usuario_e_so_as_unidades_citadas(api_falsa):  # noqa: F811
    busca = buscar_estabelecimentos(CEP, [2])
    primeira, segunda = busca["estabelecimentos"][:2]
    resposta = f"1. **{primeira['nome']}** (CNES {primeira['codigo_cnes']}): ...\n2. **{segunda['nome']}** (CNES {segunda['codigo_cnes']})"

    pontos = pontos_do_mapa(turno_com(resposta, busca))

    assert [p["cor"] for p in pontos] == [COR_USUARIO, COR_UNIDADE, COR_UNIDADE]
    assert (pontos[0]["lat"], pontos[0]["lon"]) == (LOCAL_POA["lat"], LOCAL_POA["lon"])
    assert "Rua Félix da Cunha" in pontos[0]["detalhe"]
    assert [p["nome"] for p in pontos[1:]] == [primeira["nome"], segunda["nome"]]
    assert primeira["telefone"] in pontos[1]["detalhe"]  # telefone aparece no tooltip
    assert len(busca["estabelecimentos"]) > 2  # havia mais unidades, mas só 2 foram citadas


def test_centro_do_municipio_nao_vira_ponto_do_usuario(api_falsa):  # noqa: F811
    busca = so_centro_do_municipio(buscar_estabelecimentos(CEP, [2]))
    unidade = busca["estabelecimentos"][0]

    pontos = pontos_do_mapa(turno_com(f"(CNES {unidade['codigo_cnes']})", busca))

    assert [p["cor"] for p in pontos] == [COR_UNIDADE]


def test_posicao_do_usuario_vem_de_turno_anterior(api_falsa):  # noqa: F811
    busca = buscar_estabelecimentos(CEP, [2])
    unidade = busca["estabelecimentos"][0]
    primeiro = turno_com("Encontrei estas unidades...", busca)
    ficha = {"codigo_cnes": unidade["codigo_cnes"], "nome": unidade["nome"], "lat": unidade["lat"], "lon": unidade["lon"]}
    seguimento = Turno(pergunta="...", resposta=f"(CNES {unidade['codigo_cnes']})", chamadas=[chamada("detalhar_estabelecimento", ficha)])

    pontos = pontos_do_mapa(seguimento, [primeiro])

    assert [p["cor"] for p in pontos] == [COR_USUARIO, COR_UNIDADE]
    assert pontos[0]["lat"] == LOCAL_POA["lat"]


def test_cep_novo_so_com_centro_do_municipio_apaga_posicao_antiga(api_falsa):  # noqa: F811
    busca = buscar_estabelecimentos(CEP, [2])
    unidade = busca["estabelecimentos"][0]
    primeiro = turno_com("...", busca)

    pontos = pontos_do_mapa(turno_com(f"(CNES {unidade['codigo_cnes']})", so_centro_do_municipio(busca)), [primeiro])

    assert [p["cor"] for p in pontos] == [COR_UNIDADE]


def test_busca_com_cep_invalido_nao_apaga_posicao_antiga(api_falsa):  # noqa: F811
    busca = buscar_estabelecimentos(CEP, [2])
    unidade = busca["estabelecimentos"][0]
    com_erro = chamada("buscar_estabelecimentos", buscar_estabelecimentos("123", [2]))
    turno = Turno(pergunta="...", resposta=f"(CNES {unidade['codigo_cnes']})", chamadas=[chamada("buscar_estabelecimentos", busca), com_erro])

    assert pontos_do_mapa(turno)[0]["cor"] == COR_USUARIO


def test_posicao_do_usuario_vem_da_busca_por_servico():
    unidade = {"codigo_cnes": 2264382, "nome": "UNIDADE DE SAUDE SANTA CECILIA", "lat": -30.04, "lon": -51.21}
    busca = {"local": {**LOCAL_POA}, "estabelecimentos": [unidade]}
    turno = Turno(pergunta="...", resposta="(CNES 2264382)", chamadas=[chamada("buscar_por_servico", busca)])

    assert [p["cor"] for p in pontos_do_mapa(turno)] == [COR_USUARIO, COR_UNIDADE]


def test_sem_unidade_citada_nao_ha_mapa(api_falsa):  # noqa: F811
    busca = buscar_estabelecimentos(CEP, [2])
    assert pontos_do_mapa(turno_com("Qual é o seu CEP?", busca)) == []


def test_unidade_do_detalhamento_entra_no_mapa():
    ficha = {"codigo_cnes": 2237571, "nome": "HOSPITAL NOSSA SENHORA DA CONCEICAO", "endereco": "AV FRANCISCO TREIN, 596",
             "telefone": "51 33572000", "distancia_km": None, "lat": -30.0161, "lon": -51.1586}
    turno = Turno(pergunta="...", resposta="**HOSPITAL** (CNES 2237571)", chamadas=[chamada("detalhar_estabelecimento", ficha)])

    pontos = pontos_do_mapa(turno)

    assert len(pontos) == 1 and pontos[0]["lat"] == -30.0161


def test_montar_mapa_com_tooltip(api_falsa):  # noqa: F811
    busca = buscar_estabelecimentos(CEP, [2])
    unidade = busca["estabelecimentos"][0]
    pontos = pontos_do_mapa(turno_com(f"(CNES {unidade['codigo_cnes']})", busca))

    especificacao = json.loads(montar_mapa(pontos).to_json())

    camada = especificacao["layers"][0]
    assert camada["@@type"] == "ScatterplotLayer" and camada["pickable"] is True
    assert len(camada["data"]) == 2
    assert "{nome}" in montar_mapa(pontos)._tooltip["html"]  # nome da unidade ao passar o mouse


def test_clique_numa_unidade_da_o_link_da_ferramenta(api_falsa):  # noqa: F811
    busca = buscar_estabelecimentos(CEP, [2])
    unidade = busca["estabelecimentos"][0]
    usuario, ponto = pontos_do_mapa(turno_com(f"(CNES {unidade['codigo_cnes']})", busca))

    assert ponto["link"] == unidade["link_google_maps"]
    assert ponto_clicado({"objects": {CAMADA: [ponto]}}) == ponto
    assert ponto_clicado({"objects": {CAMADA: [usuario]}}) is None  # o ponto do usuário não tem link
    assert ponto_clicado({"objects": {}}) is None  # nada selecionado
    assert ponto_clicado(None) is None
