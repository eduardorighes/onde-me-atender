"""Interface de chat em Streamlit.

Rodar (com o servidor MCP já no ar):  uv run streamlit run cliente/app.py

O Streamlit reexecuta este arquivo inteiro a cada interação. Por isso o que precisa
sobreviver entre interações fica em `st.session_state`:
- historico: mensagens no formato da Messages API (o que o modelo vê);
- turnos: um objeto Turno por pergunta (o que mostramos no chat e no painel).
"""

import json
import sys
from pathlib import Path

import streamlit as st

# Permite `from cliente.agente import ...` quando o Streamlit roda este arquivo direto.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cliente.agente import (  # noqa: E402
    ESFORCO,
    MAX_CHAMADAS_POR_TURNO,
    MCP_URL,
    MODELO,
    responder_sincrono,
    servidor_no_ar,
)
from cliente.mapa import montar_mapa, ponto_clicado, pontos_do_mapa  # noqa: E402

st.set_page_config(page_title="Onde me atender", page_icon="🏥")

# Os links do Google Maps que o modelo escreve na resposta viram botões (só CSS: o texto
# da resposta continua sendo Markdown comum).
st.markdown(
    """
    <style>
    /* Só links do texto (Markdown): o botão abaixo do mapa já tem o próprio 📍. */
    [data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] a[href*="google.com/maps"] {
        display: block;  /* linha própria, abaixo do texto do item */
        width: fit-content;
        margin: 0.35rem 0 0.2rem;
        padding: 0.15rem 0.7rem;
        border-radius: 999px;
        background: #1a73e8;
        color: #fff !important;
        font-size: 0.85rem;
        font-weight: 600;
        text-decoration: none !important;
        white-space: nowrap;
    }
    [data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] a[href*="google.com/maps"]::before {
        content: "📍 ";
    }
    [data-testid="stChatMessage"] [data-testid="stMarkdownContainer"] a[href*="google.com/maps"]:hover {
        background: #1558b0;
    }
    /* Botão do ponto clicado no mapa: mesmo azul dos links da resposta. */
    [class*="st-key-link_mapa_"] a {
        border-radius: 999px;
        background: #1a73e8;
        border-color: #1a73e8;
        color: #fff !important;
        font-weight: 600;
    }
    [class*="st-key-link_mapa_"] a:hover { background: #1558b0; border-color: #1558b0; }
    /* Botão "Nova conversa": cheio e com borda grossa, para ser fácil de achar. */
    .st-key-nova_conversa button {
        background: #1a73e8;
        border: 3px solid #1558b0;
        color: #fff;
        font-weight: 700;
    }
    .st-key-nova_conversa button:hover,
    .st-key-nova_conversa button:focus:not(:active) {
        background: #1558b0;
        border-color: #0d3c7a;
        color: #fff;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

if "historico" not in st.session_state:
    st.session_state.historico = []
    st.session_state.turnos = []


def nova_conversa():
    st.session_state.historico = []
    st.session_state.turnos = []


def resumir(texto: str, limite: int = 600) -> str:
    return texto if len(texto) <= limite else texto[:limite] + " …"


# --- Barra lateral: painel didático de depuração ----------------------------------
with st.sidebar:
    st.header("Painel de depuração")
    if servidor_no_ar():
        st.success(f"Servidor MCP no ar: {MCP_URL}")
    else:
        st.error(f"Servidor MCP fora do ar: {MCP_URL}\n\nRode `uv run uvicorn servidor_mcp.app:app`.")
    st.caption(f"Modelo: `{MODELO}` · esforço: `{ESFORCO}` · teto: {MAX_CHAMADAS_POR_TURNO} ferramentas/turno")

    for numero, turno in enumerate(st.session_state.turnos, start=1):
        with st.expander(f"Turno {numero}: {len(turno.chamadas)} ferramenta(s), {turno.segundos_total:.1f} s"):
            st.markdown(f"**Pergunta:** {turno.pergunta}")
            for chamada in turno.chamadas:
                st.markdown(f"🔧 **{chamada.nome}** · {chamada.segundos:.2f} s" + (" · ⚠️ erro" if chamada.erro else ""))
                st.code(json.dumps(chamada.argumentos, ensure_ascii=False, indent=1), language="json")
                st.caption("Resultado (resumido):")
                st.text(resumir(chamada.resultado))
            custo = turno.custo_estimado_usd
            st.markdown(
                f"**Tokens:** {turno.tokens_entrada:,} entrada / {turno.tokens_saida:,} saída"
                + (f" (~US$ {custo:.4f})" if custo is not None else "")
                + f"  \n**Chamadas ao modelo:** {turno.chamadas_modelo}"
                + f"  \n**Latência:** {turno.segundos_total:.1f} s (modelo {turno.segundos_modelo:.1f} s, "
                f"ferramentas {turno.segundos_ferramentas:.1f} s)"
            )

# --- Chat ------------------------------------------------------------------------
# Título e instrução à esquerda; o botão fica centralizado na altura dos dois.
coluna_titulo, coluna_botao = st.columns([3, 1], vertical_alignment="center")
with coluna_titulo:
    st.title("🏥 Onde me atender")
    st.caption("Informe seu CEP e o que precisa (vacina, posto de saúde, pronto atendimento...).")
coluna_botao.button("Nova conversa", key="nova_conversa", on_click=nova_conversa, width="stretch")

# Aviso de privacidade (LGPD): aparece antes da primeira pergunta.
if not st.session_state.turnos:
    st.info(
        "🔒 **Privacidade (LGPD).** Para achar unidades próximas, basta o **CEP** e o que você precisa. "
        "Não informe nome, CPF, telefone nem detalhes de saúde além do necessário."
    )

for numero, turno in enumerate(st.session_state.turnos):
    with st.chat_message("user"):
        st.markdown(turno.pergunta)
    with st.chat_message("assistant"):
        st.markdown(turno.resposta)
        # Mapa com você (azul) e as unidades citadas (vermelho), feito com os
        # resultados das ferramentas: o modelo não participa.
        pontos = pontos_do_mapa(turno, st.session_state.turnos[:numero])
        if pontos:
            evento = st.pydeck_chart(
                montar_mapa(pontos),
                height=350,
                key=f"mapa_{numero}",
                on_select="rerun",  # o clique num ponto redesenha a página com a seleção
                selection_mode="single-object",
            )
            clicado = ponto_clicado(evento.selection)
            if clicado:
                st.link_button(
                    f"📍 Abrir {clicado['nome']} no Google Maps", clicado["link"], key=f"link_mapa_{numero}"
                )
            st.caption(
                "🔵 você (aproximado) · 🔴 unidades citadas, na posição cadastrada no CNES "
                "(o cadastro pode ter erros de localização) · passe o mouse para ver nome e telefone; "
                "clique numa unidade para ver o link do Google Maps"
            )

pergunta = st.chat_input("Ex.: CEP 90570-001, onde tomo vacina?")
if pergunta:
    with st.chat_message("user"):
        st.markdown(pergunta)
    with st.chat_message("assistant"):
        with st.spinner("Consultando o CNES..."):
            tamanho_antes = len(st.session_state.historico)
            try:
                turno = responder_sincrono(st.session_state.historico, pergunta)
            except Exception as erro:
                # Descarta as mensagens do turno incompleto para o histórico continuar válido.
                del st.session_state.historico[tamanho_antes:]
                st.error(
                    f"Não foi possível responder: {erro}\n\n"
                    "Confira se o servidor MCP está no ar e se ANTHROPIC_API_KEY está no arquivo .env."
                )
                st.stop()
    st.session_state.turnos.append(turno)
    st.rerun()  # redesenha a página com o turno novo no chat e no painel

# --- Rodapé fixo -------------------------------------------------------------------
st.divider()
st.caption(
    "ℹ️ Dados do CNES (Ministério da Saúde), um cadastro: não informam vaga, fila nem plantão do dia. "
    "**Confirme por telefone antes de ir.** Em emergência, ligue **192 (SAMU)**.  \n"
    "🔒 Informe só o CEP e o que precisa (LGPD)."
)
