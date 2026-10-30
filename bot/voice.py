"""1.2, вторая половина · Голос: whisper.cpp локально, дальше как обычный захват.

ffmpeg -> 16 кГц моно wav -> whisper-cli (base, квантованная: надёжно встаёт на 1 ГБ).
Временные файлы в приватном каталоге, удаляются сразу после расшифровки.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile

from core import config
from core.tg import scrub

from bot import capture
from bot.topics import Ctx

log = logging.getLogger("workos.bot")


def whisper_bin() -> str:
    return config.env("WHISPER_BIN", "/opt/whisper.cpp/build/bin/whisper-cli")


def whisper_model() -> str:
    return config.env("WHISPER_MODEL", "/opt/whisper.cpp/models/ggml-base-q5_1.bin")


def configured() -> bool:
    return (os.access(whisper_bin(), os.X_OK) and os.path.exists(whisper_model())
            and shutil.which(config.env("FFMPEG_BIN", "ffmpeg")) is not None)


def transcribe(data: bytes) -> str:
    tmp = tempfile.mkdtemp(prefix="workos-voice-")      # 0700: чужие пользователи не видят
    try:
        src, wav, out = (os.path.join(tmp, n) for n in ("in.oga", "in.wav", "out"))
        with open(src, "wb") as f:
            f.write(data)
        subprocess.run([config.env("FFMPEG_BIN", "ffmpeg"), "-nostdin", "-loglevel", "error", "-y",
                        "-i", src, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav],
                       check=True, capture_output=True, timeout=120)
        subprocess.run([whisper_bin(), "-m", whisper_model(), "-f", wav, "-l", "ru",
                        "-otxt", "-of", out, "-nt", "-np"],
                       check=True, capture_output=True, timeout=600)
        with open(out + ".txt", encoding="utf-8", errors="replace") as f:
            return " ".join(f.read().split())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def handle(ctx: Ctx, msg: dict) -> int | None:
    media = msg.get("voice") or msg.get("audio") or msg.get("video_note")
    if not media:
        return None
    if not configured():
        eid = capture.store_event(ctx, msg, body="", meta={"voice": True, "transcribed": False})
        if eid is not None:
            ctx.reply(msg, "Голос сохранён, расшифровка не настроена.")
        return eid
    # повторная доставка: расшифровывать второй раз незачем
    seen = ctx.conn.execute("SELECT 1 FROM work.events WHERE source_id = %s AND external_id = %s",
                            (capture.SOURCE, str(msg["message_id"]))).fetchone()
    if seen:
        return None
    ctx.action(msg.get("message_thread_id"))
    try:
        text = transcribe(ctx.bot.download(media["file_id"]))
    except Exception as e:
        err = scrub(f"{type(e).__name__}: {e}")[:300]      # в тексте ошибки скачивания мог быть токен
        log.warning("расшифровка не удалась: %s", err)
        eid = capture.store_event(ctx, msg, body="", meta={"voice": True, "transcribed": False,
                                                          "error": err})
        if eid is not None:
            ctx.reply(msg, "Голос сохранён, но расшифровать не вышло.")
        return eid
    if not text:
        eid = capture.store_event(ctx, msg, body="", meta={"voice": True, "transcribed": True})
        if eid is not None:
            ctx.reply(msg, "Голос сохранён, слов не разобрал.")
        return eid
    return capture.handle(ctx, msg, text=text, voice=True, extra_meta={"transcribed": True})
