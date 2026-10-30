"""Эмбеддинги (решение 0.4 от 30.08): одна функция embed(texts, kind) -> list[vector].

Модель: multilingual-e5-base в ONNX int8, локально (onnxruntime + tokenizers, без torch).
768 измерений ровно под колонки halfvec(768), mean pooling, L2-нормировка.
Префиксы «passage: » и «query: » обязательны: без них качество e5 тихо проседает,
поэтому они зашиты здесь, и вызывающий код их не пишет.

Файлы модели лежат в EMBED_MODEL_DIR (по умолчанию /opt/workos/models/multilingual-e5-base):
model_quantized.onnx (или model.onnx) и tokenizer.json. Скачать: python tools/fetch_embed_model.py.

Переменные окружения:
  EMBED_MODEL_DIR          каталог с моделью
  EMBED_THREADS            потоки onnxruntime (0 = решает сам)
  EMBED_MAX_BATCH_TOKENS   потолок токенов в одном прогоне (по умолчанию 4096): держит пик памяти
  WORKOS_EMBED_FAKE=1      детерминированные хеш-векторы для тестов, модель не грузится

Интерфейс один: если локальная модель не пойдёт, переезд на API меняет только эту обёртку
и model_name(), а коллекторы, бэкфилл и поиск не трогаются.
"""
from __future__ import annotations

import hashlib
import math
import re
import threading
from pathlib import Path

from core import config

MODEL_NAME = "multilingual-e5-base-int8"
FAKE_MODEL_NAME = "fake-hash-768"
DIM = 768
MAX_TOKENS = 512
BATCH = 16
PREFIX = {"passage": "passage: ", "query": "query: "}
DEFAULT_DIR = "/opt/workos/models/multilingual-e5-base"

_lock = threading.Lock()
_state: tuple | None = None   # (session, tokenizer, input_names, pad_id)


def is_fake() -> bool:
    return config.env("WORKOS_EMBED_FAKE") == "1"


def model_name() -> str:
    """Что писать в embedding_model: векторы разных моделей несравнимы."""
    return FAKE_MODEL_NAME if is_fake() else MODEL_NAME


def model_dir() -> Path:
    return Path(config.env("EMBED_MODEL_DIR", DEFAULT_DIR) or DEFAULT_DIR)


def embed(texts: list[str], kind: str = "passage") -> list[list[float]]:
    """Тексты -> единичные векторы 768. kind: 'passage' для хранимого, 'query' для вопроса."""
    if kind not in PREFIX:
        raise ValueError(f"kind должен быть passage или query, а не {kind!r}")
    clean = [re.sub(r"\s+", " ", t or "").strip() for t in texts]
    if not clean:
        return []
    if is_fake():
        return [_fake_vec(t) for t in clean]
    return _onnx_embed([PREFIX[kind] + t for t in clean])


# ---------- тестовый режим ----------
def _fake_vec(text: str) -> list[float]:
    """Мешок слов, захешированный в 768 измерений: детерминированно и с похожестью по словам."""
    v = [0.0] * DIM
    words = re.findall(r"\w+", text.lower()) or [text]
    for w in words:
        h = hashlib.blake2b(w.encode("utf-8"), digest_size=8).digest()
        v[int.from_bytes(h[:4], "little") % DIM] += 1.0 if h[4] & 1 else -1.0
    norm = math.sqrt(sum(x * x for x in v))
    if norm == 0:
        h = hashlib.blake2b(text.encode("utf-8"), digest_size=4).digest()
        v[int.from_bytes(h, "little") % DIM] = 1.0
        return v
    return [x / norm for x in v]


# ---------- ONNX ----------
def _load():
    global _state
    with _lock:
        if _state is not None:
            return _state
        d = model_dir()
        model = next((d / n for n in ("model_quantized.onnx", "model.onnx") if (d / n).exists()), None)
        if model is None or not (d / "tokenizer.json").exists():
            raise FileNotFoundError(f"нет модели эмбеддингов в {d}: запусти python tools/fetch_embed_model.py")
        import onnxruntime as ort
        from tokenizers import Tokenizer

        tok = Tokenizer.from_file(str(d / "tokenizer.json"))
        tok.enable_truncation(max_length=MAX_TOKENS)
        tok.no_padding()
        so = ort.SessionOptions()
        so.intra_op_num_threads = config.env_int("EMBED_THREADS", 0)
        so.inter_op_num_threads = 1
        so.enable_cpu_mem_arena = False          # на дроплете с 1 ГБ пик памяти важнее скорости
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess = ort.InferenceSession(str(model), so, providers=["CPUExecutionProvider"])
        names = {i.name for i in sess.get_inputs()}
        pad_id = tok.token_to_id("<pad>")
        _state = (sess, tok, names, 1 if pad_id is None else pad_id)
        return _state


def _onnx_embed(texts: list[str]) -> list[list[float]]:
    import numpy as np

    sess, tok, names, pad_id = _load()
    encs = tok.encode_batch(texts)
    budget = config.env_int("EMBED_MAX_BATCH_TOKENS", 4096)
    order = sorted(range(len(texts)), key=lambda i: len(encs[i].ids))   # меньше паддинга
    out: list = [None] * len(texts)
    i = 0
    while i < len(order):
        batch = [order[i]]
        i += 1
        while i < len(order) and len(batch) < BATCH and \
                (len(batch) + 1) * len(encs[order[i]].ids) <= budget:
            batch.append(order[i])
            i += 1
        width = max(len(encs[j].ids) for j in batch)
        ids = np.full((len(batch), width), pad_id, dtype=np.int64)
        mask = np.zeros((len(batch), width), dtype=np.int64)
        for row, j in enumerate(batch):
            n = len(encs[j].ids)
            ids[row, :n] = encs[j].ids
            mask[row, :n] = 1
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in names:
            feeds["token_type_ids"] = np.zeros_like(ids)
        feeds = {k: v for k, v in feeds.items() if k in names}
        hidden = sess.run(None, feeds)[0]
        if hidden.ndim == 3:   # last_hidden_state -> mean pooling по маске
            m = mask[..., None].astype(hidden.dtype)
            vec = (hidden * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None)
        else:
            vec = hidden
        vec = vec / np.clip(np.linalg.norm(vec, axis=1, keepdims=True), 1e-12, None)
        for row, j in enumerate(batch):
            out[j] = vec[row].astype(float).tolist()
    return out


# ---------- хранение в Postgres ----------
def vector_type(conn, table: str = "work.events", column: str = "embedding") -> str | None:
    """'halfvec' или 'vector', если стоит pgvector и колонка векторная; None, если это real[] (тесты)."""
    if not conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'").fetchone():
        return None
    row = conn.execute(
        "SELECT format_type(a.atttypid, a.atttypmod) AS t FROM pg_attribute a "
        "WHERE a.attrelid = %s::regclass AND a.attname = %s AND NOT a.attisdropped",
        (table, column)).fetchone()
    t = (row or {}).get("t") or ""
    for name in ("halfvec", "vector"):
        if t.startswith(name):
            return name
    return None


def literal(vec: list[float]) -> str:
    """Текстовый литерал pgvector: '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{x:.6g}" for x in vec) + "]"
