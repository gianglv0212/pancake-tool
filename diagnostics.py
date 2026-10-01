"""Rotating UTF-8 logs with credential redaction, including tracebacks."""
import json
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote, quote_plus

SECRETS = set()


def register_secret(value):
    if value:
        SECRETS.update((value, quote(value, safe=''), quote_plus(value), json.dumps(value)[1:-1]))


def redact(value):
    text = str(value)
    for secret in sorted(SECRETS, key=len, reverse=True):
        text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(?i)((?:page_access_token|access_token|authorization|token|secret)["\s]*[:=]\s*["\s]*)([^"\s&,}]+)', r'\1[REDACTED]', text)
    text = re.sub(r'(/webhooks/pancake/)[^\s?"\']+', r'\1[REDACTED]', text)
    return text


class SafeFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


def setup(level='INFO', path='logs/pancake.log'):
    formatter = SafeFormatter('%(asctime)s %(levelname)s %(name)s %(message)s')
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    file = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5, encoding='utf-8')
    file.setFormatter(formatter)
    logging.basicConfig(level=getattr(logging, level), handlers=[console, file], force=True)


def response_text(value):
    return redact(json.dumps(value, ensure_ascii=False, default=str))[:6000]


def log_http_error(logger, stage, error):
    try:
        raw = error.read(65536).decode('utf-8', errors='replace')
    except Exception:
        raw = '<response body unavailable>'
    logger.error('%s HTTP=%s reason=%s response=%s', stage, error.code,
                 redact(error.reason), redact(raw)[:6000])
