"""Testes do avaliador (sem rede): fidelidade e acerto de ferramentas, com textos de rodadas reais."""

import json

from avaliacao.avaliar import (
    avaliar_execucao,
    checar_fidelidade,
    dados_das_ferramentas,
    tem_aviso_de_confirmacao,
)
from cliente.agente import ChamadaFerramenta, Turno


def chamada(nome: str, resultado: dict, **argumentos) -> ChamadaFerramenta:
    return ChamadaFerramenta(nome=nome, argumentos=argumentos, resultado=json.dumps(resultado), segundos=0.0)


def busca(*unidades: tuple[int, str, str]) -> ChamadaFerramenta:
    estabelecimentos = [{"codigo_cnes": c, "nome": n, "telefone": t} for c, n, t in unidades]
    resultado = {"local": {"codigo_municipio": "431490"}, "estabelecimentos": estabelecimentos}
    return chamada("buscar_estabelecimentos", resultado, cep="90570-001", codigos_tipo_unidade=[5])


def nao_verificados(resposta: str, *unidades: tuple[int, str, str]) -> list[str]:
    return checar_fidelidade(resposta, dados_das_ferramentas([busca(*unidades)]))[2]


def test_telefone_com_ramal_no_cadastro():
    unidade = (2237881, "HOSPITAL BANCO DE OLHOS", "5131354444 RAMAL3413")
    assert nao_verificados("Telefone: 5131354444 ramal 3413.", unidade) == []


def test_telefone_com_ou_sem_ddd_e_zero():
    unidade = (2262568, "HOSPITAL SAO LUCAS DA PUCRS", "051 33203000")
    assert nao_verificados("Telefone: (51) 3320-3000 ou 3320-3000.", unidade) == []


def test_telefone_reformatado_com_todos_os_digitos():
    unidade = (6716911, "SMS CER CENTRO AP 10", "2131112607")
    assert nao_verificados("Telefone: 2131-1126-07 (conforme cadastro).", unidade) == []


def test_telefone_com_digito_a_menos_e_falha():
    unidade = (6537650, "PRONTO ATENDIMENTO BOM JESUS", "51-32889-5424")
    assert nao_verificados("Telefone: 51 3288-9542.", unidade) == ["telefone 51 3288-9542"]


def test_telefone_inventado_e_falha():
    unidade = (2265060, "HOSPITAL CRISTO REDENTOR", "51 33574100")
    assert nao_verificados("Telefone: 51 3357-4199.", unidade) == ["telefone 51 3357-4199"]


def test_cep_sem_hifen_nao_e_telefone():
    assert nao_verificados("Envie o CEP completo (por exemplo, 90570-001 ou 90570001).") == []


def test_nome_com_espaco_diferente():
    unidade = (6220584, "SES RJ UPA 24 H BOTAFOGO", "21 22992577")
    assert nao_verificados("1. **SES RJ UPA 24H BOTAFOGO** (CNES 6220584)", unidade) == []


def test_nome_inventado_e_falha():
    unidade = (6220584, "SES RJ UPA 24 H BOTAFOGO", "21 22992577")
    assert nao_verificados("1. **UPA COPACABANA**", unidade) == ["nome 'UPA COPACABANA'"]


CASO = {
    "id": "saude_mental",
    "pergunta": "Preciso de atendimento em saúde mental. CEP 90570-001.",
    "ferramentas_esperadas": ["listar_tipos_unidade", "buscar_estabelecimentos"],
    "ferramentas_alternativas": [["buscar_por_servico"]],
}


def avaliar(*chamadas: ChamadaFerramenta, gabarito=None) -> dict:
    turno = Turno(pergunta=CASO["pergunta"], resposta="Procure a UBS do seu bairro.", chamadas=list(chamadas))
    return avaliar_execucao(CASO, 1, [turno], None, gabarito)


def test_caminho_alternativo_de_ferramentas_e_aceito_sem_gabarito():
    r = avaliar(chamada("buscar_por_servico", {}, cep="90570-001", servico="psicossocial"), gabarito=[1, 2, 3])
    assert r["acerto_ferramentas"] and r["passou"]
    assert r["hit5"] is None and r["precisao5"] is None


def test_caminho_principal_de_ferramentas_mede_o_gabarito():
    r = avaliar(chamada("listar_tipos_unidade", {}, busca="caps"), busca(), gabarito=[1, 2, 3])
    assert r["acerto_ferramentas"] and r["hit5"] is False


def test_nenhum_dos_caminhos_de_ferramentas_e_falha():
    r = avaliar(chamada("detalhar_estabelecimento", {}, codigo_cnes=1))
    assert not r["acerto_ferramentas"] and not r["passou"]


def test_lista_de_servicos_reescrita_nao_e_nome_inventado():
    servicos = {
        "codigo_cnes": 2264382,
        "nome": "UNIDADE DE SAUDE SANTA CECILIA",
        "servicos": [
            {"servico": "112 - SERVICO DE ATENCAO AO PRE-NATAL, PARTO E NASCIMENTO", "classificacoes": []},
            {"servico": "119 - SERVICO DE CONTROLE DE TABAGISMO", "classificacoes": ["001 - ABORDAGEM DO FUMANTE"]},
        ],
    }
    dados = dados_das_ferramentas([chamada("servicos_do_estabelecimento", servicos, codigo_cnes=2264382)])
    resposta = "1. **Pré-natal, parto e nascimento**\n2. **Controle do tabagismo**\n3. **Quimioterapia**"
    assert checar_fidelidade(resposta, dados)[2] == ["nome 'Quimioterapia'"]


def test_aviso_de_confirmacao_aceita_ligue_antes_de_ir():
    assert tem_aviso_de_confirmacao("Confirme por telefone antes de ir.")
    assert tem_aviso_de_confirmacao("Ligue antes de ir e pergunte se a unidade aplica a vacina.")
    assert not tem_aviso_de_confirmacao("Telefone: 51 33574100. Em emergência, ligue 192.")


def test_tipo_de_apoio_e_aceito_nos_argumentos():
    caso = {**CASO, "tipos_aceitos": [70], "tipos_de_apoio": [1, 2]}
    turno = Turno(pergunta="", resposta="Procure a UBS.", chamadas=[
        chamada("buscar_por_servico", {}, cep="90570-001", servico="psicossocial"),
        chamada("buscar_estabelecimentos", {"local": {"codigo_municipio": "431490"}}, codigos_tipo_unidade=[1, 2]),
        chamada("buscar_estabelecimentos", {"local": {"codigo_municipio": "431490"}}, codigos_tipo_unidade=[5]),
    ])
    r = avaliar_execucao(caso, 1, [turno], None, None)
    assert (r["args_ok"], r["args_total"]) == (2, 3)
