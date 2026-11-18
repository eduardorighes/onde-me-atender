"""Testes das 6 ferramentas MCP (como funções Python comuns), sem rede."""

import httpx
import pytest

from conftest import LOCAL_POA, carregar_fixture
from servidor_mcp import cnes, ferramentas
from servidor_mcp.ferramentas import (
    FONTE_CNES,
    buscar_estabelecimentos,
    buscar_por_servico,
    detalhar_estabelecimento,
    distancia_km,
    estabelecimentos_do_municipio,
    gerar_link_mapa,
    listar_tipos_unidade,
    resumir_estabelecimento,
    servicos_do_estabelecimento,
    telefone_fora_do_padrao,
    termo_cnes,
)

CAMPOS_RESUMO = {
    "codigo_cnes",
    "nome",
    "endereco",
    "bairro",
    "telefone",
    "turno_atendimento",
    "atende_sus",
    "distancia_km",
    "lat",
    "lon",
    "localizacao_corrigida",
    "link_google_maps",
}
CENTRO_HISTORICO = (-30.0297, -51.2240)  # Rua dos Andradas, Porto Alegre


@pytest.fixture
def api_falsa(monkeypatch, cep_falso):
    """Substitui as chamadas HTTP do CNES por respostas reais salvas em fixtures."""
    pagina_tipo2 = carregar_fixture("estabelecimentos_poa_tipo2_pagina0.json")
    tipo73 = carregar_fixture("estabelecimentos_poa_tipo73.json")

    def get_json_falso(caminho, params=None, cliente=None):
        if caminho == "/cnes/tipounidades":
            return carregar_fixture("tipounidades.json")
        if caminho == "/cnes/estabelecimentos/2237571":
            return carregar_fixture("estabelecimento_2237571.json")
        if caminho.startswith("/cnes/estabelecimentos/"):
            return None  # 404
        # Lista: só Porto Alegre (6 dígitos!) e só a primeira página tem dados.
        if params["codigo_municipio"] != "431490" or params["offset"] > 0:
            return {"estabelecimentos": []}
        return {2: pagina_tipo2, 73: tipo73}.get(params.get("codigo_tipo_unidade"), {"estabelecimentos": []})

    monkeypatch.setattr(cnes, "_get_json", get_json_falso)


# --- Haversine -------------------------------------------------------------------


def test_haversine_um_grau_no_equador():
    assert distancia_km(0, 0, 0, 1) == pytest.approx(111.19, rel=0.01)


def test_haversine_londres_paris():
    assert distancia_km(51.5074, -0.1278, 48.8566, 2.3522) == pytest.approx(343.5, rel=0.01)


def test_haversine_mesmo_ponto():
    assert distancia_km(-30.03, -51.23, -30.03, -51.23) == 0


# --- buscar_estabelecimentos (pelo CEP) -----------------------------------------------


def test_busca_pelo_cep_ordena_por_distancia_e_devolve_o_local(api_falsa):
    resultado = buscar_estabelecimentos("90570-001", [2])

    assert resultado["local"]["codigo_municipio"] == "431490"
    assert resultado["local"]["bairro"] == "Moinhos de Vento"
    assert (resultado["local"]["lat"], resultado["local"]["lon"]) == (LOCAL_POA["lat"], LOCAL_POA["lon"])
    assert resultado["ordenacao"] == "distância até o CEP"
    assert resultado == {
        **estabelecimentos_do_municipio("431490", [2], "Moinhos de Vento", lat=LOCAL_POA["lat"], lon=LOCAL_POA["lon"]),
        "local": resultado["local"],
    }


def test_cep_so_com_centro_do_municipio_usa_o_bairro(api_falsa, cep_falso):
    todos = estabelecimentos_do_municipio("431490", [2], max_resultados=10)["estabelecimentos"]
    cep_falso.update(bairro=todos[-1]["bairro"].title(), fonte_coordenadas="BrasilAPI (centro do município)")

    resultado = buscar_estabelecimentos("90570-001", [2])

    assert resultado["local"]["lat"] is None and resultado["local"]["lon"] is None
    assert resultado["estabelecimentos"][0]["bairro"] == todos[-1]["bairro"]
    assert all(e["distancia_km"] is None for e in resultado["estabelecimentos"])
    assert "aviso_distancia" not in resultado


def test_varios_tipos_numa_chamada_vem_juntos_por_distancia(api_falsa):
    resultado = buscar_estabelecimentos("90570-001", [2, 73], max_resultados=10)

    so_2 = estabelecimentos_do_municipio("431490", [2])["total_encontrado"]
    so_73 = estabelecimentos_do_municipio("431490", [73])["total_encontrado"]
    assert resultado["total_encontrado"] == so_2 + so_73
    assert resultado["codigos_tipo_unidade"] == [2, 73]
    distancias = [e["distancia_km"] for e in resultado["estabelecimentos"]]
    assert distancias == sorted(distancias)


def test_tipo_repetido_nao_repete_unidades(api_falsa):
    assert (
        estabelecimentos_do_municipio("431490", [73, 73])["total_encontrado"]
        == estabelecimentos_do_municipio("431490", [73])["total_encontrado"]
    )


def test_cep_invalido_vira_mensagem():
    for resultado in (buscar_estabelecimentos("123", [2]), buscar_por_servico("123", "psicossocial")):
        assert "8 dígitos" in resultado["erro"]
        assert "fonte" in resultado


# --- estabelecimentos_do_municipio (a busca, com o local já resolvido) ----------------


def test_saida_tem_so_campos_previstos_e_fonte(api_falsa):
    resultado = estabelecimentos_do_municipio("431490", [2], lat=CENTRO_HISTORICO[0], lon=CENTRO_HISTORICO[1])

    assert resultado["fonte"] == FONTE_CNES
    assert resultado["atualizado_em"]  # data mais recente dos registros
    assert len(resultado["estabelecimentos"]) == 5  # padrão
    for item in resultado["estabelecimentos"]:
        assert set(item) == CAMPOS_RESUMO


def test_no_maximo_10_itens(api_falsa):
    resultado = estabelecimentos_do_municipio("431490", [2], max_resultados=50)
    assert len(resultado["estabelecimentos"]) == 10
    assert resultado["total_encontrado"] > 10  # havia mais, mas só 10 voltam


def test_ordena_por_distancia(api_falsa):
    resultado = estabelecimentos_do_municipio("431490", [2], lat=CENTRO_HISTORICO[0], lon=CENTRO_HISTORICO[1], max_resultados=10)

    distancias = [e["distancia_km"] for e in resultado["estabelecimentos"]]
    assert resultado["ordenacao"] == "distância até o CEP"
    assert distancias == sorted(distancias)


def test_aviso_de_distancia_so_quando_ha_distancia(api_falsa):
    com_distancia = estabelecimentos_do_municipio("431490", [2], lat=CENTRO_HISTORICO[0], lon=CENTRO_HISTORICO[1])
    sem_distancia = estabelecimentos_do_municipio("431490", [2], bairro="Centro")

    assert "localização cadastrada no CNES" in com_distancia["aviso_distancia"]
    assert "aviso_distancia" not in sem_distancia


def unidade_e_usuario_longe():
    """Uma unidade real da fixture e um "usuário" ~3,3 km ao norte dela."""
    pagina = carregar_fixture("estabelecimentos_poa_tipo2_pagina0.json")["estabelecimentos"]
    unidade = next(r for r in pagina if r["estabelecimento_faz_atendimento_ambulatorial_sus"] == "SIM")
    usuario = (unidade["latitude_estabelecimento_decimo_grau"] + 0.03, unidade["longitude_estabelecimento_decimo_grau"])
    return unidade, usuario


def test_localizacao_suspeita_e_corrigida_pelo_endereco(api_falsa, monkeypatch):
    unidade, (lat, lon) = unidade_e_usuario_longe()
    consultas = []

    def osm_falso(endereco, lat_ref, lon_ref):
        consultas.append(endereco)
        return lat_ref - 0.002, lon_ref  # o endereço "verdadeiro" fica a ~200 m do usuário

    monkeypatch.setattr(ferramentas, "coordenadas_do_endereco", osm_falso)

    # O usuário está no mesmo bairro que o CNES atribui à unidade, mas a ~3,3 km dela.
    resultado = estabelecimentos_do_municipio("431490", [2], bairro=unidade["bairro_estabelecimento"].title(), lat=lat, lon=lon)

    primeira = resultado["estabelecimentos"][0]
    assert primeira["codigo_cnes"] == unidade["codigo_cnes"]
    assert primeira["localizacao_corrigida"] is True
    assert primeira["distancia_km"] == pytest.approx(0.2, abs=0.05)
    assert primeira["lat"] == pytest.approx(lat - 0.002)  # mapa e link usam a posição nova
    assert unidade["nome_fantasia"] in resultado["aviso_distancia"]
    # Só unidades do mesmo bairro e longe são conferidas (não todas as 19).
    assert unidade["endereco_estabelecimento"] in consultas[0] and len(consultas) < 5


def test_localizacao_nao_muda_se_o_cnes_estava_certo(api_falsa, monkeypatch):
    unidade, (lat, lon) = unidade_e_usuario_longe()
    lat_cnes, lon_cnes = unidade["latitude_estabelecimento_decimo_grau"], unidade["longitude_estabelecimento_decimo_grau"]
    monkeypatch.setattr(ferramentas, "coordenadas_do_endereco", lambda *args: (lat_cnes + 0.0005, lon_cnes))

    resultado = estabelecimentos_do_municipio("431490", [2], bairro=unidade["bairro_estabelecimento"], lat=lat, lon=lon, max_resultados=10)

    assert not any(e["localizacao_corrigida"] for e in resultado["estabelecimentos"])
    assert "corrigida" not in resultado["aviso_distancia"]


def test_sem_bairro_nao_confere_localizacao(api_falsa, monkeypatch):
    _, (lat, lon) = unidade_e_usuario_longe()
    monkeypatch.setattr(ferramentas, "coordenadas_do_endereco", lambda *args: pytest.fail("não devia consultar"))
    estabelecimentos_do_municipio("431490", [2], lat=lat, lon=lon)


def test_sem_coordenadas_prioriza_o_bairro(api_falsa):
    todos = estabelecimentos_do_municipio("431490", [2], max_resultados=10)["estabelecimentos"]
    bairro_alvo = todos[-1]["bairro"]  # um bairro que não viria primeiro em ordem alfabética

    resultado = estabelecimentos_do_municipio("431490", [2], bairro=bairro_alvo.lower())

    assert resultado["estabelecimentos"][0]["bairro"] == bairro_alvo
    assert all(e["distancia_km"] is None for e in resultado["estabelecimentos"])


def test_apenas_sus(api_falsa):
    so_sus = estabelecimentos_do_municipio("431490", [73], max_resultados=10)
    todos = estabelecimentos_do_municipio("431490", [73], apenas_sus=False, max_resultados=10)

    assert all(e["atende_sus"] for e in so_sus["estabelecimentos"])
    assert todos["total_encontrado"] == 7
    assert so_sus["total_encontrado"] < todos["total_encontrado"]


def test_codigo_com_6_ou_7_digitos_da_o_mesmo_resultado(api_falsa):
    assert estabelecimentos_do_municipio("4314902", [2]) == estabelecimentos_do_municipio("431490", [2])


def test_municipio_sem_a_unidade_pedida(api_falsa):
    resultado = estabelecimentos_do_municipio("431490", [85])
    assert resultado["estabelecimentos"] == []
    assert "Nenhum estabelecimento" in resultado["mensagem"]


def test_codigo_de_municipio_invalido_vira_mensagem(api_falsa):
    resultado = estabelecimentos_do_municipio("12345", [2])
    assert "6 ou 7 dígitos" in resultado["erro"]
    assert resultado["fonte"] == FONTE_CNES


def test_erro_5xx_vira_mensagem_amigavel_sem_excecao(monkeypatch):
    # Aqui a falha é simulada no nível HTTP, para exercitar o retry de verdade.
    cliente_original = httpx.Client
    pedidos = []

    def cliente_com_erro(**kwargs):
        def responder(request):
            pedidos.append(request)
            return httpx.Response(503)

        return cliente_original(transport=httpx.MockTransport(responder))

    monkeypatch.setattr(httpx, "Client", cliente_com_erro)

    resultado = estabelecimentos_do_municipio("431490", [2])

    assert len(pedidos) == 3  # 3 tentativas
    assert "não respondeu após 3 tentativas" in resultado["erro"]
    assert resultado["fonte"] == FONTE_CNES


# --- listar_tipos_unidade -------------------------------------------------------------


def test_tipos_filtrados_por_texto(api_falsa):
    resultado = listar_tipos_unidade("Pronto")
    assert {t["codigo"] for t in resultado["tipos"]} == {20, 21, 73}
    assert resultado["fonte"] == FONTE_CNES


def test_tipos_busca_sem_acento(api_falsa):
    resultado = listar_tipos_unidade("imunização")
    assert resultado["tipos"] == [{"codigo": 85, "descricao": "CENTRO DE IMUNIZACAO"}]


def test_tipos_sem_resultado_orienta_o_modelo(api_falsa):
    resultado = listar_tipos_unidade("vacina")
    assert resultado["tipos"] == []
    assert "sem `busca`" in resultado["mensagem"]


def test_tipos_sem_busca_devolve_tabela_toda(api_falsa):
    resultado = listar_tipos_unidade()
    assert resultado["total"] == len(carregar_fixture("tipounidades.json")["tipos_unidade"])
    assert set(resultado["tipos"][0]) == {"codigo", "descricao"}


# --- detalhar_estabelecimento ---------------------------------------------------------------


def test_detalhar_estabelecimento(api_falsa):
    ficha = detalhar_estabelecimento("2237571")
    assert ficha["nome"] == "HOSPITAL NOSSA SENHORA DA CONCEICAO"
    assert ficha["tipo_unidade"] == "HOSPITAL GERAL"  # código traduzido pela tabela de tipos
    assert ficha["atendimento_hospitalar"] is True
    assert ficha["fonte"] == FONTE_CNES
    assert ficha["atualizado_em"] == "2025-11-13"


def test_cada_unidade_tem_link_do_google_maps_por_nome_endereco_e_cep(api_falsa):
    ficha = detalhar_estabelecimento("2237571")
    assert ficha["link_google_maps"] == (
        "https://www.google.com/maps/search/?api=1&query="
        "HOSPITAL%20NOSSA%20SENHORA%20DA%20CONCEICAO%2C%20AV%20FRANCISCO%20TREIN%2C%20596%2C"
        "%20CRISTO%20REDENTOR%2C%2091350-200"
    )
    lista = estabelecimentos_do_municipio("431490", [73], max_resultados=10)["estabelecimentos"]
    assert all(e["link_google_maps"].startswith("https://www.google.com/maps/search/") for e in lista)


def test_detalhar_codigo_inexistente(api_falsa):
    assert "Nenhum estabelecimento" in detalhar_estabelecimento("9999999")["erro"]


# --- gerar_link_mapa ------------------------------------------------------------------------


def test_link_mapa_usa_nome_e_endereco():
    resultado = gerar_link_mapa("Rua Ramiro Barcelos, 910, Porto Alegre - RS", nome="HOSPITAL MOINHOS DE VENTO")

    assert resultado["modo"] == "nome e endereço"
    assert resultado["google_maps"] == (
        "https://www.google.com/maps/search/?api=1&query="
        "HOSPITAL%20MOINHOS%20DE%20VENTO%2C%20Rua%20Ramiro%20Barcelos%2C%20910%2C%20Porto%20Alegre%20-%20RS"
    )
    assert "openstreetmap" not in resultado  # só Google Maps, para economizar tokens
    assert resultado["fonte"]


def test_link_mapa_codifica_a_barra_de_sem_numero():
    assert "S%2FN" in gerar_link_mapa("Rua Santa Rita de Cassia, S/N, Porto Alegre - RS")["google_maps"]


def test_link_mapa_codifica_acentos():
    resultado = gerar_link_mapa("Rua São Manoel, 543, Porto Alegre - RS")
    assert "Rua%20S%C3%A3o%20Manoel" in resultado["google_maps"]
    assert " " not in resultado["google_maps"]  # nada de espaços ou acentos crus na URL


def test_link_mapa_ignora_coordenadas_por_padrao():
    # Mesmo recebendo lat/lon, o padrão é buscar pelo nome e endereço.
    resultado = gerar_link_mapa("Rua Ramiro Barcelos, 910, Porto Alegre - RS", nome="HOSPITAL", lat=-30.03, lon=-51.22)
    assert resultado["modo"] == "nome e endereço"
    assert "-30.03" not in resultado["google_maps"]


def test_link_mapa_com_coordenadas_quando_pedido():
    resultado = gerar_link_mapa("Rua X", usar_coordenadas=True, lat=-30.0161, lon=-51.1586)

    assert resultado["modo"] == "coordenadas cadastradas no CNES"
    assert resultado["google_maps"] == "https://www.google.com/maps/search/?api=1&query=-30.0161,-51.1586"
    assert "openstreetmap" not in resultado


def test_link_mapa_erros_viram_mensagem():
    assert "Informe o endereço" in gerar_link_mapa("   ")["erro"]
    assert "informe lat e lon" in gerar_link_mapa("Rua X", usar_coordenadas=True)["erro"]


# --- Rotas REST de depuração -------------------------------------------------------------


def test_rotas_rest_chamam_as_mesmas_funcoes(api_falsa):
    from fastapi.testclient import TestClient

    from servidor_mcp.app import app

    with TestClient(app) as cliente:
        assert cliente.get("/health").json()["status"] == "ok"
        assert cliente.get("/api/tipos-unidade", params={"busca": "pronto"}).json() == listar_tipos_unidade("pronto")
        resposta = cliente.get("/api/estabelecimentos", params={"cep": "90570001", "codigos_tipo_unidade": [2, 73]})
        assert resposta.json() == buscar_estabelecimentos("90570-001", [2, 73])
        assert "8 dígitos" in cliente.get("/api/cep/123").json()["erro"]
        parametros = {"endereco": "Rua X, Canoas", "nome": "UBS X"}
        assert cliente.get("/api/mapa", params=parametros).json() == gerar_link_mapa("Rua X, Canoas", "UBS X")


# --- Serviços especializados -------------------------------------------------------------------


def test_busca_por_servico_sem_acento_e_sem_maiusculas(api_servicos):
    com_acento = buscar_por_servico("90570-001", "Atenção Psicossocial")
    sem_acento = buscar_por_servico("90570001", "atencao psicossocial")

    assert com_acento["estabelecimentos"] == sem_acento["estabelecimentos"]
    assert com_acento["estabelecimentos"]  # e encontrou algo
    assert com_acento["competencia"] == "07/2026"
    assert com_acento["local"]["codigo_municipio"] == "431490"  # o município vem do CEP
    assert com_acento["fonte"] == FONTE_CNES
    nomes = {e["nome"] for e in com_acento["estabelecimentos"]}
    assert "CAPSI CASA HARMONIA" in nomes


def test_busca_por_servico_agrupa_por_estabelecimento(api_servicos):
    resultado = buscar_por_servico("90570-001", "atencao primaria", max_resultados=10)

    # A UBS Santa Cecília tem 3 registros de atenção primária, mas aparece uma vez só.
    assert resultado["total_unidades"] == 1
    unidade = resultado["estabelecimentos"][0]
    assert unidade["codigo_cnes"] == 2264382
    assert len(unidade["servicos"]) == 3
    assert unidade["link_google_maps"]  # mesmo sem o cadastro detalhado, há link pelo nome


def test_busca_por_servico_apenas_sus(api_servicos):
    so_sus = buscar_por_servico("90570-001", "psicossocial")
    todos = buscar_por_servico("90570-001", "psicossocial", apenas_sus=False)

    codigos_sus = {e["codigo_cnes"] for e in so_sus["estabelecimentos"]}
    codigos_todos = {e["codigo_cnes"] for e in todos["estabelecimentos"]}
    assert 3744841 not in codigos_sus  # INAPE: atendimento psicossocial só particular
    assert 3744841 in codigos_todos
    assert all(s["sus"] for e in so_sus["estabelecimentos"] for s in e["servicos"])


def test_servico_inexistente_devolve_lista_de_servicos(api_servicos):
    resultado = buscar_por_servico("90570-001", "transplante de coração")

    assert resultado["estabelecimentos"] == []
    assert "Nenhuma unidade" in resultado["mensagem"]
    assert "115 - ATENCAO PSICOSSOCIAL" in resultado["servicos_no_municipio"]


def test_termo_cnes_traduz_especialidades():
    assert termo_cnes("PEDIATRA") == "PEDIATR"  # casa com PRONTO SOCORRO PEDIATRICO
    assert termo_cnes("CARDIOLOGISTA") == "CARDIOLOG"
    assert termo_cnes("PSICOLOGO") == "PSICOSSOCIAL"
    assert termo_cnes("FISIOTERAPIA") == "FISIOTERAPIA"


def test_especialidade_vira_termo_do_cnes(api_servicos):
    resultado = buscar_por_servico("90570-001", "psicólogo")

    assert resultado["termos_cnes"] == ["PSICOSSOCIAL"]
    assert "CAPSI CASA HARMONIA" in {e["nome"] for e in resultado["estabelecimentos"]}


def test_sem_sus_traz_encaminhamento_e_opcao_particular(api_servicos):
    resultado = buscar_por_servico("90570-001", "oncologista")  # na amostra, só um hospital particular

    assert resultado["estabelecimentos"] == []
    assert "UBS" in resultado["encaminhamento_sus"]
    assert [o["codigo_cnes"] for o in resultado["outras_opcoes"]] == [622133]
    assert resultado["outras_opcoes"][0]["como_encontrada"] == "serviço cadastrado sem SUS"


def test_sem_servico_procura_clinica_pelo_nome(api_servicos, monkeypatch):
    clinicas = [
        {"codigo_cnes": 30163, "nome_fantasia": "ISBS SERVICOS DE DERMATOLOGIA LTDA",
         "estabelecimento_faz_atendimento_ambulatorial_sus": "NAO"},
        {"codigo_cnes": 20273, "nome_fantasia": "CLINICA GHENO"},
    ]
    monkeypatch.setattr(cnes, "listar_estabelecimentos", lambda municipio, tipo: (clinicas, True))

    resultado = buscar_por_servico("90570-001", "dermatologista")

    assert [o["nome"] for o in resultado["outras_opcoes"]] == ["ISBS SERVICOS DE DERMATOLOGIA LTDA"]
    assert resultado["outras_opcoes"][0]["como_encontrada"] == "nome da clínica"
    assert resultado["outras_opcoes"][0]["atende_sus"] is False


def test_servicos_do_estabelecimento(api_servicos):
    resultado = servicos_do_estabelecimento("2264382")

    assert resultado["nome"] == "UNIDADE DE SAUDE SANTA CECILIA"
    assert resultado["competencia"] == "07/2026" and resultado["fonte"] == FONTE_CNES
    primaria = next(s for s in resultado["servicos"] if s["servico"] == "159 - ATENCAO PRIMARIA")
    assert len(primaria["classificacoes"]) == 3  # 3 registros viram 1 serviço
    assert primaria["sus"] and primaria["ambulatorial"] and not primaria["hospitalar"]
    assert resultado["total_servicos"] == 4


def test_servicos_de_estabelecimento_sem_cadastro(api_servicos):
    assert "Nenhum serviço especializado" in servicos_do_estabelecimento("9999999")["mensagem"]
    assert "Código CNES inválido" in servicos_do_estabelecimento("abc")["erro"]


# --- Telefone fora do padrão -----------------------------------------------------------


@pytest.mark.parametrize("telefone", [
    "51 33574100", "(51)32893456", "051 33203000", "23346348", "3365 5201", "5131354444 RAMAL3413",
    "51 99946-0593", "0800 6450 644", None, "",
])
def test_telefone_no_padrao(telefone):
    assert not telefone_fora_do_padrao(telefone)


@pytest.mark.parametrize("telefone", ["51-32889-5424", "5133", "513357410"])
def test_telefone_fora_do_padrao(telefone):
    assert telefone_fora_do_padrao(telefone)


def test_resumo_avisa_quando_o_telefone_esta_fora_do_padrao():
    assert "telefone_aviso" in resumir_estabelecimento({"numero_telefone_estabelecimento": "51-32889-5424"})
    assert "telefone_aviso" not in resumir_estabelecimento({"numero_telefone_estabelecimento": "51 33574100"})
