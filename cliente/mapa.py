"""Mapa embutido no chat: mostra o usuário e as unidades citadas na resposta.

O mapa é desenhado pela INTERFACE, com os resultados das ferramentas que o agente já
recebeu. O modelo não participa: não gasta tokens nem chamadas de ferramenta.

- `pontos_do_mapa(turno)`: função pura (testável sem Streamlit) que lê o turno.
- `montar_mapa(pontos)`: monta o mapa do pydeck, com nome e telefone no tooltip.
- `ponto_clicado(selecao)`: a unidade em que o usuário clicou (o app mostra o link dela).
"""

import json
import re

import pydeck

COR_USUARIO = [30, 100, 220]  # azul
COR_UNIDADE = [220, 50, 50]  # vermelho
FERRAMENTAS_COM_LOCAL = ("buscar_estabelecimentos", "buscar_por_servico")  # recebem o CEP
CAMADA = "pontos"  # id da camada: a seleção do Streamlit vem agrupada por ele


def codigos_citados(resposta: str) -> set[int]:
    """Códigos CNES que aparecem na resposta, no formato '(CNES 1234567)'."""
    return {int(codigo) for codigo in re.findall(r"CNES\D{0,3}(\d{6,7})", resposta)}


def posicao_do_usuario(turnos) -> dict | None:
    """Ponto do usuário segundo o `local` da busca mais recente da conversa.

    Vale o último CEP consultado com sucesso: se o usuário trocar de CEP, o mapa acompanha.
    Se esse CEP só tiver o centro do município (`lat` null), devolve None, para não sugerir
    uma localização que não é a dele.
    """
    for turno in reversed(turnos):
        for chamada in reversed(turno.chamadas):
            if chamada.nome not in FERRAMENTAS_COM_LOCAL or chamada.erro:
                continue
            try:
                dados = json.loads(chamada.resultado).get("local")
            except json.JSONDecodeError:
                continue
            if not dados:
                continue  # a busca falhou (ex.: CEP inválido)
            if dados.get("lat") is None:
                return None
            return {
                "nome": "Você (posição aproximada)",
                "detalhe": f"CEP {dados['cep']}: {dados.get('logradouro') or ''}, {dados.get('bairro') or ''}",
                "lat": dados["lat"],
                "lon": dados["lon"],
                "cor": COR_USUARIO,
                "link": None,
                "dica": "",
            }
    return None


def pontos_do_mapa(turno, anteriores=()) -> list[dict]:
    """Pontos para o mapa: a posição do usuário (se for da rua) e as unidades citadas.

    Só entram unidades que o modelo citou na resposta (pelo código CNES) e que têm
    coordenadas. A posição do usuário pode vir de um turno `anterior`: numa pergunta de
    seguimento (ex.: detalhes de uma unidade) o modelo não busca pelo CEP de novo.
    """
    citados = codigos_citados(turno.resposta)
    unidades = {}  # codigo_cnes -> ponto (evita repetir a mesma unidade)

    for chamada in turno.chamadas:
        try:
            dados = json.loads(chamada.resultado)
        except json.JSONDecodeError:
            continue

        if chamada.nome in FERRAMENTAS_COM_LOCAL:
            candidatos = dados.get("estabelecimentos", [])
        elif chamada.nome == "detalhar_estabelecimento":
            candidatos = [dados]
        else:
            continue

        for unidade in candidatos:
            if unidade.get("codigo_cnes") in citados and unidade.get("lat") is not None and unidade.get("lon") is not None:
                distancia = ""
                if unidade.get("distancia_km") is not None:
                    distancia = f" · ~{unidade['distancia_km']:.1f} km".replace(".", ",")  # 0.5 -> 0,5
                corrigida = " · posição corrigida pelo endereço" if unidade.get("localizacao_corrigida") else ""
                unidades[unidade["codigo_cnes"]] = {
                    "nome": unidade["nome"],
                    "detalhe": f"{unidade.get('endereco') or ''} · Tel.: {unidade.get('telefone') or 'não informado'}{distancia}{corrigida}",
                    "lat": unidade["lat"],
                    "lon": unidade["lon"],
                    "cor": COR_UNIDADE,
                    # O mesmo link que a ferramenta deu ao modelo (busca pelo nome e endereço).
                    "link": unidade.get("link_google_maps"),
                    "dica": "Clique para ver o link do Google Maps" if unidade.get("link_google_maps") else "",
                }

    if not unidades:
        return []  # sem unidades, não há mapa
    usuario = posicao_do_usuario([*anteriores, turno])
    return ([usuario] if usuario else []) + list(unidades.values())


def zoom_para(pontos: list[dict]) -> int:
    """Zoom do mapa conforme a distância entre os pontos (em graus, aproximado)."""
    extensao = max(
        max(p["lat"] for p in pontos) - min(p["lat"] for p in pontos),
        max(p["lon"] for p in pontos) - min(p["lon"] for p in pontos),
    )
    for limite, zoom in [(0.01, 15), (0.03, 14), (0.06, 13), (0.12, 12), (0.25, 11)]:
        if extensao < limite:
            return zoom
    return 10


def montar_mapa(pontos: list[dict]) -> pydeck.Deck:
    """Mapa com um círculo por ponto. Ao passar o mouse, aparecem nome e detalhes."""
    camada = pydeck.Layer(
        "ScatterplotLayer",
        id=CAMADA,
        data=pontos,
        get_position="[lon, lat]",
        get_fill_color="cor",
        get_radius=60,  # metros
        radius_min_pixels=7,  # continua visível com o mapa afastado
        pickable=True,  # necessário para o tooltip e o clique
    )
    centro = pydeck.ViewState(
        latitude=sum(p["lat"] for p in pontos) / len(pontos),
        longitude=sum(p["lon"] for p in pontos) / len(pontos),
        zoom=zoom_para(pontos),
    )
    return pydeck.Deck(
        layers=[camada],
        initial_view_state=centro,
        tooltip={"html": "<b>{nome}</b><br/>{detalhe}<br/><i>{dica}</i>"},
    )


def ponto_clicado(selecao) -> dict | None:
    """Unidade clicada no mapa, se tiver link. `selecao` é o `selection` do st.pydeck_chart.

    O tooltip some quando o mouse sai do ponto, então não dá para pôr um link nele: o app
    mostra um botão com o link da unidade clicada, abaixo do mapa.
    """
    objetos = (selecao or {}).get("objects", {}).get(CAMADA, [])
    if objetos and objetos[0].get("link"):
        return objetos[0]
    return None
