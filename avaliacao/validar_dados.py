"""Validação dos dados do CNES (usa a rede; não usa a API da Anthropic).

Para Porto Alegre e mais 3 municípios do RS, confere:
- Consistência: o total de buscar_estabelecimentos bate com uma paginação direta da API.
- Qualidade: % de registros com coordenadas, telefone e atendimento SUS; coordenadas no Brasil.
- Atualidade: data de atualização mediana e mais antiga.
- Formato do código: a mesma busca com 6 e com 7 dígitos dá o mesmo resultado.

    uv run python avaliacao/validar_dados.py
"""

import logging
import statistics
import sys
from datetime import date
from pathlib import Path

import httpx

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from servidor_mcp import cnes  # noqa: E402
from servidor_mcp.ferramentas import estabelecimentos_do_municipio  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)

MUNICIPIOS = {  # código IBGE de 7 dígitos
    "Porto Alegre": "4314902",
    "Canoas": "4304606",
    "Caxias do Sul": "4305108",
    "Pelotas": "4314407",
}
TIPOS = {1: "posto de saúde", 2: "centro de saúde/UBS", 73: "pronto atendimento"}
# Retângulo que envolve o Brasil (aproximado).
LIMITES_BRASIL = {"lat": (-33.8, 5.3), "lon": (-74.0, -34.8)}


def contar_paginando_direto(codigo_municipio_6: str, tipo: int) -> int:
    """Conta os registros percorrendo as páginas da API diretamente (sem o nosso cliente)."""
    total, pagina = 0, 0
    with httpx.Client(timeout=60) as cliente:
        while True:
            resposta = cliente.get(
                f"{cnes.URL_BASE}/cnes/estabelecimentos",
                params={"codigo_municipio": codigo_municipio_6, "codigo_tipo_unidade": tipo, "status": 1,
                        "limit": 20, "offset": pagina},
            )
            resposta.raise_for_status()
            lote = resposta.json()["estabelecimentos"]
            total += len(lote)
            if not lote:
                return total
            pagina += 1


def porcentagem(parte: int, todo: int) -> str:
    return f"{parte / todo:.0%}" if todo else "n/a"


def validar() -> str:
    linhas = [
        f"# Validação dos dados do CNES: {date.today().isoformat()}",
        "",
        "| Município | Tipo | Registros | Bate com a API? | 6 = 7 dígitos? | Coordenadas | No Brasil | Telefone | SUS | Atualização mediana | Mais antiga |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for nome, codigo_7 in MUNICIPIOS.items():
        codigo_6 = cnes.converter_codigo_municipio(codigo_7)
        for tipo, descricao in TIPOS.items():
            registros, _ = cnes.listar_estabelecimentos(codigo_6, tipo)

            # Consistência: total da ferramenta (sem filtro SUS) x paginação direta.
            total_ferramenta = estabelecimentos_do_municipio(codigo_6, [tipo], apenas_sus=False)["total_encontrado"]
            total_direto = contar_paginando_direto(codigo_6, tipo)
            bate = "✅" if total_ferramenta == total_direto else f"❌ ({total_ferramenta} x {total_direto})"

            # Formato do código: 6 e 7 dígitos devem dar a mesma resposta.
            mesmo = estabelecimentos_do_municipio(codigo_6, [tipo]) == estabelecimentos_do_municipio(codigo_7, [tipo])

            # Qualidade.
            com_coordenadas = [
                r for r in registros
                if r["latitude_estabelecimento_decimo_grau"] is not None
                and r["longitude_estabelecimento_decimo_grau"] is not None
            ]
            no_brasil = [
                r for r in com_coordenadas
                if LIMITES_BRASIL["lat"][0] <= r["latitude_estabelecimento_decimo_grau"] <= LIMITES_BRASIL["lat"][1]
                and LIMITES_BRASIL["lon"][0] <= r["longitude_estabelecimento_decimo_grau"] <= LIMITES_BRASIL["lon"][1]
            ]
            com_telefone = [r for r in registros if (r["numero_telefone_estabelecimento"] or "").strip()]
            sus = [r for r in registros if r["estabelecimento_faz_atendimento_ambulatorial_sus"] == "SIM"]

            # Atualidade.
            datas = sorted(r["data_atualizacao"] for r in registros if r["data_atualizacao"])
            mediana = statistics.median_low(datas) if datas else "n/a"
            mais_antiga = datas[0] if datas else "n/a"

            linhas.append(
                f"| {nome} | {tipo} ({descricao}) | {len(registros)} | {bate} | {'✅' if mesmo else '❌'} "
                f"| {porcentagem(len(com_coordenadas), len(registros))} "
                f"| {porcentagem(len(no_brasil), len(com_coordenadas))} "
                f"| {porcentagem(len(com_telefone), len(registros))} | {porcentagem(len(sus), len(registros))} "
                f"| {mediana} | {mais_antiga} |"
            )
            print(linhas[-1])
    linhas += [
        "",
        "Observação: a ferramenta usa cache de 24 h; se a API mudou nesse intervalo, a contagem pode diferir.",
    ]
    return "\n".join(linhas)


if __name__ == "__main__":
    relatorio = validar()
    pasta = Path(__file__).resolve().parent / "resultados"
    pasta.mkdir(exist_ok=True)
    arquivo = pasta / f"validacao_{date.today().isoformat()}.md"
    arquivo.write_text(relatorio + "\n", encoding="utf-8")
    print(f"\nRelatório salvo em avaliacao/resultados/{arquivo.name}")
