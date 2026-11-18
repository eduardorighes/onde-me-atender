"""Cache simples: um dicionário em memória + arquivos JSON na pasta .cache/.

Cada valor é guardado junto com o instante em que foi criado. Ao ler, se o valor
tiver mais de `ttl_segundos`, ele é ignorado (expirou) e a função devolve None.
O arquivo JSON permite reaproveitar as consultas entre reinícios do servidor.
"""

import hashlib
import json
import time
from pathlib import Path

PASTA_PADRAO = Path(__file__).resolve().parent.parent / ".cache"
TTL_PADRAO = 24 * 60 * 60  # 24 horas


class Cache:
    def __init__(self, pasta: Path = PASTA_PADRAO, ttl_segundos: int = TTL_PADRAO):
        self.pasta = Path(pasta)
        self.ttl_segundos = ttl_segundos
        self.memoria: dict[str, dict] = {}

    def _arquivo(self, chave: str) -> Path:
        # O nome do arquivo é um hash da chave, para evitar caracteres estranhos.
        nome = hashlib.sha1(chave.encode("utf-8")).hexdigest()[:16]
        return self.pasta / f"{nome}.json"

    def obter(self, chave: str):
        """Devolve o valor guardado ou None se não existir ou tiver expirado."""
        item = self.memoria.get(chave)
        if item is None:
            arquivo = self._arquivo(chave)
            if arquivo.exists():
                try:
                    item = json.loads(arquivo.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    item = None  # arquivo corrompido: trata como ausente
        if item is None:
            return None
        if time.time() - item["criado_em"] > self.ttl_segundos:
            return None
        self.memoria[chave] = item
        return item["valor"]

    def guardar(self, chave: str, valor) -> None:
        item = {"chave": chave, "criado_em": time.time(), "valor": valor}
        self.memoria[chave] = item
        self.pasta.mkdir(parents=True, exist_ok=True)
        self._arquivo(chave).write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")


# Instância única usada pelo servidor.
cache = Cache()
