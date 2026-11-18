"""Consulta de CEP: ViaCEP (endereço e código IBGE) + coordenadas aproximadas.

- ViaCEP: https://viacep.com.br/ws/{cep}/json/ -> campo `ibge` com 7 dígitos.
  Para CEP inexistente responde HTTP 200 com {"erro": "true"}.
- Coordenadas, em duas tentativas:
  1. Nominatim (OpenStreetMap), geocodificando "logradouro + cidade + UF" e escolhendo,
     entre os trechos da rua, o que tem o mesmo CEP (ou o mesmo bairro).
     Gratuito e sem chave, mas pede no máximo 1 requisição por segundo e um
     User-Agent identificado (o cache de 24 h ajuda a respeitar isso).
  2. BrasilAPI (https://brasilapi.com.br/api/cep/v2/{cep}, campo `location.coordinates`).
     ATENÇÃO: testando com CEPs reais, ela devolve o CENTRO DO MUNICÍPIO (todos os
     CEPs de Porto Alegre dão -30.03283, -51.23019). Serve só como último recurso.

Também tem `coordenadas_do_endereco`, que procura um endereço do CNES no OpenStreetMap
para conferir unidades com localização suspeita no cadastro.
"""

import time

import httpx

from servidor_mcp.cache import cache
from servidor_mcp.cnes import converter_codigo_municipio

TIMEOUT = 15.0  # segundos
USER_AGENT = "onde-me-atender/0.1 (projeto didatico)"
_ultima_consulta_nominatim = 0.0


class ErroCEP(Exception):
    """Erro com mensagem amigável, pronta para mostrar ao usuário."""


def normalizar_cep(cep) -> str:
    """Remove pontos, traços e espaços. '90570-001' -> '90570001'."""
    digitos = "".join(c for c in str(cep) if c.isdigit())
    if len(digitos) != 8:
        raise ErroCEP(f"CEP inválido: '{cep}'. Um CEP tem 8 dígitos (ex.: 90570-001).")
    return digitos


def escolher_trecho(resultados: list[dict], cep: str, bairro: str | None) -> dict:
    """Escolhe, entre os resultados do Nominatim, o trecho de rua que corresponde ao CEP.

    Ruas longas aparecem no OpenStreetMap divididas em vários trechos, cada um com o
    seu CEP e bairro (a Rua Doutor Barcelos, em Porto Alegre, passa por Cavalhada,
    Camaquã e Tristeza). Pegar só o primeiro resultado pode cair a quilômetros do
    usuário. Preferência: mesmo CEP > mesmo bairro > primeiro resultado.
    """
    for resultado in resultados:
        cep_trecho = "".join(c for c in resultado.get("address", {}).get("postcode", "") if c.isdigit())
        if cep_trecho == cep:
            return resultado
    if bairro:
        for resultado in resultados:
            if bairro.casefold() in resultado.get("display_name", "").casefold():
                return resultado
    return resultados[0]


def _esperar_vez_no_nominatim() -> None:
    """Política de uso do Nominatim: no máximo 1 requisição por segundo."""
    global _ultima_consulta_nominatim
    espera = 1.0 - (time.monotonic() - _ultima_consulta_nominatim)
    if espera > 0:
        time.sleep(espera)
    _ultima_consulta_nominatim = time.monotonic()


def _coordenadas_nominatim(
    logradouro: str, bairro: str | None, cidade: str, uf: str, cep: str, cliente: httpx.Client
) -> tuple[float | None, float | None]:
    """Tenta obter (lat, lon) do trecho da rua do CEP no OpenStreetMap.

    Qualquer falha devolve (None, None).
    """
    _esperar_vez_no_nominatim()
    try:
        resposta = cliente.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "street": logradouro,
                "city": cidade,
                "state": uf,
                "country": "Brasil",
                "format": "jsonv2",
                "limit": 10,  # vários trechos da mesma rua
                "addressdetails": 1,  # traz o CEP (`postcode`) de cada trecho
            },
            headers={"User-Agent": USER_AGENT},
        )
        resultados = resposta.json() if resposta.status_code == 200 else []
        if not resultados:
            return None, None
        trecho = escolher_trecho(resultados, cep, bairro)
        return float(trecho["lat"]), float(trecho["lon"])
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        return None, None


def _coordenadas_brasilapi(cep: str, cliente: httpx.Client) -> tuple[float | None, float | None]:
    """Tenta obter (lat, lon) na BrasilAPI. Qualquer falha devolve (None, None)."""
    try:
        resposta = cliente.get(f"https://brasilapi.com.br/api/cep/v2/{cep}")
        if resposta.status_code != 200:
            return None, None
        coordenadas = (resposta.json().get("location") or {}).get("coordinates") or {}
        return float(coordenadas["latitude"]), float(coordenadas["longitude"])
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        return None, None


def consultar_cep(cep, cliente: httpx.Client | None = None) -> dict:
    """Endereço, município (IBGE 6 e 7 dígitos) e coordenadas aproximadas de um CEP."""
    if cliente is None:
        with httpx.Client(timeout=TIMEOUT) as novo_cliente:
            return consultar_cep(cep, novo_cliente)

    cep = normalizar_cep(cep)
    guardado = cache.obter(f"cep:{cep}")
    if guardado is not None:
        return guardado

    try:
        resposta = cliente.get(f"https://viacep.com.br/ws/{cep}/json/")
        resposta.raise_for_status()
        dados = resposta.json()
    except httpx.HTTPError as erro:
        raise ErroCEP("O serviço de CEP (ViaCEP) não respondeu. Tente novamente em instantes.") from erro

    if dados.get("erro"):
        raise ErroCEP(f"CEP {cep[:5]}-{cep[5:]} não encontrado. Confira os números e tente de novo.")

    lat, lon, fonte_coordenadas = None, None, None
    if dados.get("logradouro"):  # CEPs "gerais" de cidades pequenas não têm logradouro
        lat, lon = _coordenadas_nominatim(
            dados["logradouro"], dados.get("bairro"), dados["localidade"], dados["uf"], cep, cliente
        )
        fonte_coordenadas = "OpenStreetMap (rua)" if lat is not None else None
    if lat is None:
        lat, lon = _coordenadas_brasilapi(cep, cliente)
        fonte_coordenadas = "BrasilAPI (centro do município)" if lat is not None else None

    resultado = {
        "cep": f"{cep[:5]}-{cep[5:]}",
        "logradouro": dados.get("logradouro") or None,
        "bairro": dados.get("bairro") or None,
        "cidade": dados.get("localidade"),
        "uf": dados.get("uf"),
        "codigo_ibge_7": dados.get("ibge"),
        "codigo_municipio": converter_codigo_municipio(dados["ibge"]),  # 6 dígitos, para o CNES
        "lat": lat,
        "lon": lon,
        "fonte_coordenadas": fonte_coordenadas,
    }
    cache.guardar(f"cep:{cep}", resultado)
    return resultado


def coordenadas_do_endereco(
    endereco: str, lat_referencia: float, lon_referencia: float, cliente: httpx.Client | None = None
) -> tuple[float | None, float | None]:
    """Procura um endereço no OpenStreetMap, só numa caixa de ~5 km ao redor da referência.

    Os endereços do CNES não trazem o nome da cidade ("RUA RAMIRO BARCELOS 910"); limitar
    a busca à região do usuário evita cair numa rua de mesmo nome em outra cidade.
    Devolve (None, None) se não achar. O resultado fica em cache por 24 h.
    """
    if cliente is None:
        with httpx.Client(timeout=TIMEOUT) as novo_cliente:
            return coordenadas_do_endereco(endereco, lat_referencia, lon_referencia, novo_cliente)

    chave = f"endereco:{endereco}:{lat_referencia:.2f}:{lon_referencia:.2f}"
    guardado = cache.obter(chave)
    if guardado is not None:
        return guardado["lat"], guardado["lon"]

    raio = 0.05  # graus, ~5 km
    _esperar_vez_no_nominatim()
    try:
        resposta = cliente.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": endereco,
                # caixa: oeste, norte, leste, sul; bounded=1 descarta o que estiver fora dela
                "viewbox": f"{lon_referencia - raio},{lat_referencia + raio},{lon_referencia + raio},{lat_referencia - raio}",
                "bounded": 1,
                "countrycodes": "br",
                "format": "jsonv2",
                "limit": 1,
            },
            headers={"User-Agent": USER_AGENT},
        )
        resultados = resposta.json() if resposta.status_code == 200 else []
        lat, lon = (float(resultados[0]["lat"]), float(resultados[0]["lon"])) if resultados else (None, None)
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        return None, None  # falha de rede: não guarda em cache, tenta de novo na próxima

    cache.guardar(chave, {"lat": lat, "lon": lon})
    return lat, lon
