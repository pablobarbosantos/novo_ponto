"""
Utilitários compartilhados por todas as fases do pipeline.

Cada fase (fN_*.py) importa daqui: caminhos padronizados, config.yaml,
logging estruturado em logs/pipeline.log, e um helper de download
idempotente (spec ESPEC_CLAUDE_CODE.md §1.1 e §1.4).

Nenhuma fase deve depender de estado em memória de outra fase — só de
arquivos em disco. Este módulo não guarda estado entre fases, apenas
oferece as mesmas convenções para todas.
"""

from __future__ import annotations

import hashlib
import logging
import sys
import time
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Caminhos (todos relativos à raiz do repositório, onde este arquivo mora em
# pipeline/_common.py — a raiz é o pai de pipeline/)
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA_INBOX = DATA / "inbox"
DATA_RAW = DATA / "raw"
DATA_INTERIM = DATA / "interim"
DATA_INTERIM_MD = DATA_INTERIM / "md"
DATA_MANUAL = DATA / "manual"
DATA_PROCESSED = DATA / "processed"
PIPELINE_DIR = ROOT / "pipeline"
OUTPUT_DIR = ROOT / "output"
OUTPUT_FICHAS = OUTPUT_DIR / "fichas"
LOGS_DIR = ROOT / "logs"
LOG_FILE = LOGS_DIR / "pipeline.log"
CONFIG_PATH = ROOT / "config.yaml"

for _d in (
    DATA_INBOX, DATA_RAW, DATA_INTERIM_MD, DATA_MANUAL, DATA_PROCESSED,
    OUTPUT_DIR, OUTPUT_FICHAS, LOGS_DIR,
):
    _d.mkdir(parents=True, exist_ok=True)

load_dotenv(ROOT / ".env")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Lê config.yaml. Falha alto e claro se não existir — é obrigatório."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"config.yaml não encontrado em {CONFIG_PATH}. "
            "Rode o bootstrap do projeto antes de qualquer fase."
        )
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Logging estruturado — timestamp, fase, ação, contagem
# ---------------------------------------------------------------------------

_LOG_FORMAT = "%(asctime)s | %(faseid)s | %(levelname)s | %(message)s"


class _FaseFilter(logging.Filter):
    def __init__(self, faseid: str):
        super().__init__()
        self.faseid = faseid

    def filter(self, record: logging.LogRecord) -> bool:
        record.faseid = self.faseid
        return True


def get_logger(faseid: str) -> logging.Logger:
    """
    Logger que escreve em logs/pipeline.log (append, compartilhado entre
    fases) e no console. `faseid` é algo como "fase1_ibge".
    """
    logger = logging.getLogger(faseid)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        fmt = logging.Formatter(_LOG_FORMAT)
        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        file_handler.setFormatter(fmt)
        file_handler.addFilter(_FaseFilter(faseid))

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(fmt)
        console_handler.addFilter(_FaseFilter(faseid))

        logger.addHandler(file_handler)
        logger.addHandler(console_handler)

    return logger


def log_resumo_fase(
    logger: logging.Logger,
    *,
    entrada: int,
    saida: int,
    descartados: int = 0,
    motivo_descarte: str | None = None,
) -> None:
    """Resumo padronizado ao final de uma fase (spec §1.4)."""
    logger.info(
        "RESUMO — entrada=%d saida=%d descartados=%d%s",
        entrada, saida, descartados,
        f" motivo='{motivo_descarte}'" if motivo_descarte else "",
    )


# ---------------------------------------------------------------------------
# Download idempotente
# ---------------------------------------------------------------------------

def download_if_needed(
    url: str,
    dest: Path,
    logger: logging.Logger,
    *,
    min_size_bytes: int = 1024,
    timeout: int = 120,
    headers: dict | None = None,
) -> tuple[Path, bool]:
    """
    Baixa `url` para `dest` só se `dest` não existir ou for suspeitosamente
    pequeno (< min_size_bytes, indício de download truncado/erro salvo como
    arquivo). Retorna (caminho, foi_baixado_agora).

    Idempotência: spec §1.1 — "Todo download vai para data/raw/ e é
    verificado por existência + tamanho antes de baixar de novo."
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists() and dest.stat().st_size >= min_size_bytes:
        logger.info("cache hit, não baixa de novo: %s", dest)
        return dest, False

    logger.info("baixando %s -> %s", url, dest)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=timeout, headers=headers) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        with open(tmp, "wb") as f, tqdm(
            total=total or None, unit="B", unit_scale=True, desc=dest.name
        ) as bar:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)
                    bar.update(len(chunk))
    tmp.replace(dest)
    logger.info("download concluído: %s (%d bytes)", dest, dest.stat().st_size)
    return dest, True


def get_with_retry(
    url: str,
    logger: logging.Logger,
    *,
    method: str = "GET",
    max_retries: int = 5,
    backoff_base: float = 2.0,
    **kwargs,
) -> requests.Response:
    """
    GET/POST com retry e backoff exponencial — usado para Overpass (429
    frequente) e ORS (rate limit). Spec §6 Fase 2/5.
    """
    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = requests.request(method, url, timeout=kwargs.pop("timeout", 90), **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = backoff_base ** attempt
                logger.warning(
                    "status=%d tentativa=%d/%d, aguardando %.1fs",
                    resp.status_code, attempt + 1, max_retries, wait,
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            wait = backoff_base ** attempt
            logger.warning(
                "erro de request (%s), tentativa=%d/%d, aguardando %.1fs",
                exc, attempt + 1, max_retries, wait,
            )
            time.sleep(wait)
    raise RuntimeError(f"falha após {max_retries} tentativas em {url}: {last_exc}")


def coord_hash(*coords: float) -> str:
    """Hash estável de um conjunto de coordenadas — chave de cache (Fase 5)."""
    raw = ",".join(f"{c:.6f}" for c in coords)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
