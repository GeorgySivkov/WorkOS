#!/usr/bin/env python3
"""Скачать multilingual-e5-base (ONNX int8, сборка Xenova) в EMBED_MODEL_DIR.

Запуск на дроплете один раз (сеть нужна только здесь, тесты модель не качают):
    sudo mkdir -p /opt/workos/models && sudo chown $USER /opt/workos/models
    .venv/bin/python tools/fetch_embed_model.py                  # в EMBED_MODEL_DIR или /opt/workos/models/multilingual-e5-base
    .venv/bin/python tools/fetch_embed_model.py --dir ./models/e5 --force

Проверки: размер равен Content-Length, файл не меньше разумного минимума, а для файлов
из LFS ещё и sha256 сверяется с x-linked-etag, который отдаёт huggingface.co.
HF_TOKEN из окружения подставляется, если задан (для публичной модели не нужен).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

import httpx

REPO = "Xenova/multilingual-e5-base"
DEFAULT_DIR = "/opt/workos/models/multilingual-e5-base"
# (путь в репозитории, имя на диске, минимальный разумный размер в байтах)
FILES = [
    ("onnx/model_quantized.onnx", "model_quantized.onnx", 100_000_000),
    ("tokenizer.json", "tokenizer.json", 1_000_000),
    ("config.json", "config.json", 100),
]


def _linked_sha(resp: httpx.Response) -> str | None:
    for r in [*resp.history, resp]:
        etag = (r.headers.get("x-linked-etag") or "").strip('"')
        if re.fullmatch(r"[0-9a-f]{64}", etag):
            return etag
    return None


def fetch(client: httpx.Client, url: str, dest: Path, min_size: int) -> int:
    part = dest.with_suffix(dest.suffix + ".part")
    sha = hashlib.sha256()
    written = 0
    with client.stream("GET", url) as r:
        if r.status_code != 200:
            raise SystemExit(f"{url}: HTTP {r.status_code}")
        expected = int(r.headers.get("content-length") or 0)
        want_sha = _linked_sha(r)
        with open(part, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                sha.update(chunk)
                written += len(chunk)
                if expected and written % (50 << 20) < len(chunk):
                    print(f"    {written >> 20} / {expected >> 20} МБ", flush=True)
    if expected and written != expected:
        part.unlink(missing_ok=True)
        raise SystemExit(f"{dest.name}: скачано {written} байт, ожидалось {expected}")
    if written < min_size:
        part.unlink(missing_ok=True)
        raise SystemExit(f"{dest.name}: подозрительно мал ({written} байт), проверь ссылку {url}")
    if want_sha and sha.hexdigest() != want_sha:
        part.unlink(missing_ok=True)
        raise SystemExit(f"{dest.name}: sha256 не совпал ({sha.hexdigest()} против {want_sha})")
    part.replace(dest)
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", default=os.environ.get("EMBED_MODEL_DIR") or DEFAULT_DIR)
    ap.add_argument("--revision", default="main")
    ap.add_argument("--force", action="store_true", help="перекачать, даже если файл уже есть")
    args = ap.parse_args()

    target = Path(args.dir)
    target.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "workos-fetch-embed/1.0"}
    if os.environ.get("HF_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['HF_TOKEN']}"
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(120.0, connect=20.0), headers=headers) as client:
        for remote, local, min_size in FILES:
            dest = target / local
            if dest.exists() and dest.stat().st_size >= min_size and not args.force:
                print(f"  {local}: уже есть ({dest.stat().st_size >> 20} МБ), пропускаю")
                continue
            url = f"https://huggingface.co/{REPO}/resolve/{args.revision}/{remote}"
            print(f"→ {local} из {url}")
            n = fetch(client, url, dest, min_size)
            print(f"  {local}: {n:,} байт, проверено".replace(",", " "))

    print(f"""
Готово: {target}

Дальше:
  1. Права на чтение для сервисных пользователей:
       sudo chmod -R a+rX {target}
  2. В .env.reader и .env.bot (если путь не по умолчанию):
       EMBED_MODEL_DIR={target}
       EMBED_THREADS=1            # на дроплете с одним vCPU
  3. Проверка (модель грузится ~10 с, в памяти держит ~350-450 МБ):
       WORKOS_ROLE=reader .venv/bin/python -c "from core import embed; print(len(embed.embed(['проверка'])[0]))"
     должно напечатать 768.
  4. Бэкфилл за 90 дней, во втором окне смотри free -m (своп не должен расти):
       WORKOS_ROLE=reader .venv/bin/python workctl.py embed
""")


if __name__ == "__main__":
    sys.exit(main())
