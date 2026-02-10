import asyncio
import base64
import json
import logging
from collections.abc import Callable

import numpy as np
import websockets

from src.constants import (
    BARGE_IN_FRAMES_THRESHOLD,
    DEFAULT_SAMPLE_RATE,
    ENABLE_LOCAL_BARGE_IN,
    MIN_OPENAI_INPUT_CHUNK,
    OPENAI_INPUT_RATE,
    OPENAI_OUTPUT_RATE,
    REALTIME_INPUT_FORMAT,
    REALTIME_MODEL,
    REALTIME_MODALITIES,
    REALTIME_OUTPUT_FORMAT,
    REALTIME_URL,
    REALTIME_VOICE,
    VAD_RMS_THRESHOLD,
    VAD_SILENCE_MS,
    get_openai_api_key,
    load_instructions,
)
from src.audio_handler import AudioHandler
from src.utils import AudioConverter

logger = logging.getLogger(__name__)


class AudioWebSocketClient:
    """
    Клиент для OpenAI Realtime API:
    - принимает PCM16 8 kHz из Asterisk (push_pcm)
    - отправляет в Realtime через input_audio_buffer.append
    - принимает ответы (response.audio.delta) и отдаёт их в AudioHandler
    """

    def __init__(
        self,
        session_uuid: str,
        audio_handler: AudioHandler,
        voice: str = "alloy",
        transcript_callback: Callable[[str], None] | None = None,
    ):
        self.session_uuid = session_uuid
        self.voice = voice or REALTIME_VOICE
        self.api_key = get_openai_api_key()
        self.model = REALTIME_MODEL
        self.url = REALTIME_URL
        self.instructions = load_instructions()
        if not self.instructions:
            logger.warning(
                "Инструкции ассистента пусты, Realtime сессия может отвечать "
                "без заданного контекста (session_uuid=%s)",
                self.session_uuid,
            )
        else:
            logger.info(
                "Загружены инструкции ассистента (session_uuid=%s, "
                "длина=%d символов, первые 200 символов: %s...)",
                self.session_uuid,
                len(self.instructions),
                self.instructions[:200],
            )
        self.audio_handler = audio_handler
        self.input_rate = OPENAI_INPUT_RATE
        self.output_rate = OPENAI_OUTPUT_RATE
        self.response_modalities = REALTIME_MODALITIES
        self.transcript_callback = transcript_callback
        self._connected_event = asyncio.Event()
        self.vad_threshold = VAD_RMS_THRESHOLD
        self.vad_silence_window = VAD_SILENCE_MS / 1000.0
        self._last_voice_ts: float | None = None
        self._awaiting_response = False
        self._commit_in_progress = False  # Защита от повторных commit
        self._last_commit_ts: float | None = None  # Время последнего commit
        self._silence_task: asyncio.Task | None = None
        self._transcript_buffers: dict[str, list[str]] = {}
        self._active_response_id: str | None = None
        self._response_audio_bytes = 0
        self._response_audio_chunks = 0
        
        # Локальный barge-in: отслеживание RMS фреймов во время воспроизведения
        self._enable_local_barge_in = ENABLE_LOCAL_BARGE_IN
        self._barge_in_frames_threshold = BARGE_IN_FRAMES_THRESHOLD
        self._recent_rms_values: list[float] = []  # Последние RMS значения
        self._consecutive_high_rms = 0  # Количество подряд идущих фреймов с высоким RMS
        if not self._enable_local_barge_in:
            logger.warning(
                "Локальный barge-in отключён (session_uuid=%s); перебивание "
                "зависит только от server VAD",
                self.session_uuid,
            )

        self.ws: websockets.WebSocketClientProtocol | None = None
        self.connected: bool = False

        # Очередь входящего PCM от Asterisk → OpenAI
        # maxsize можно подкрутить при необходимости
        self.pcm_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)

        # Для контроля, сколько байт отправили
        self._bytes_sent_to_openai: int = 0

    # ========= ПУБЛИЧНЫЙ ВХОД ДЛЯ ASTERISK =========

    def push_pcm(self, data: bytes) -> None:
        """
        Вызывается из UdpSession (через jitter-buffer, если включен).
        Складывает сырые PCM16 (8 kHz, mono) в очередь для отправки в OpenAI.
        Ресемплинг до 24 кГц выполняется в _forward_pcm_to_openai.
        """
        if not data:
            return

        self._track_voice_activity(data)

        try:
            self.pcm_queue.put_nowait(data)
            # Логируем периодически, чтобы не спамить (каждые ~100 пакетов)
            if self.pcm_queue.qsize() % 100 == 0 or self.pcm_queue.qsize() == 1:
                logger.info(
                    "PCM получен от Asterisk: session_uuid=%s, размер=%d байт, "
                    "размер очереди=%d, всего отправлено в OpenAI=%d байт",
                    self.session_uuid,
                    len(data),
                    self.pcm_queue.qsize(),
                    self._bytes_sent_to_openai,
                )
            else:
                logger.debug(
                    "PCM добавлен в очередь: session_uuid=%s, размер=%d байт, "
                    "размер очереди=%d",
                    self.session_uuid,
                    len(data),
                    self.pcm_queue.qsize(),
                )
        except asyncio.QueueFull:
            logger.warning(
                "Очередь PCM переполнена для session_uuid=%s, пакет пропущен",
                self.session_uuid,
            )

    # ========= БАЗОВЫЕ WS-ОПЕРАЦИИ =========

    async def send_event(self, event_dict: dict) -> None:
        """
        Отправка события в Realtime API.
        """
        if not self.ws:
            logger.warning(
                "Попытка отправить событие без активного WebSocket "
                "(session_uuid=%s): %s",
                self.session_uuid,
                event_dict.get("type"),
            )
            return

        try:
            message = json.dumps(event_dict, ensure_ascii=False)
            await self.ws.send(message)
            # Не логируем каждый input_audio_buffer.append (слишком много)
            # Логируем только важные события
            event_type = event_dict.get("type")
            if event_type not in ("input_audio_buffer.append",):
                logger.debug(
                    "Отправлено событие в Realtime API "
                    "(session_uuid=%s, type=%s)",
                    self.session_uuid,
                    event_type,
                )
        except Exception as e:
            logger.error(
                "Ошибка при отправке события в WebSocket "
                "(session_uuid=%s, type=%s): %s",
                self.session_uuid,
                event_dict.get("type"),
                e,
                exc_info=True,
            )

    async def connect(self) -> None:
        """
        Устанавливает WebSocket соединение и настраивает сессию.
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "OpenAI-Beta": "realtime=v1",
        }

        logger.info(
            "Подключение к OpenAI Realtime API: url=%s, model=%s, voice=%s, "
            "session_uuid=%s",
            self.url,
            self.model,
            self.voice,
            self.session_uuid,
        )

        self.ws = await websockets.connect(self.url, extra_headers=headers)
        self.connected = True
        self._connected_event.set()

        logger.info(
            "[CONNECT] Подключено к OpenAI Realtime API для session_uuid=%s",
            self.session_uuid,
        )

        # Конфигурация сессии с server_vad
        # Примечание: input_sample_rate и output_sample_rate не поддерживаются
        # в session.update, они определяются через input_audio_format и output_audio_format.
        # Оптимизация: используем 24 кГц (нативный формат Realtime) вместо 16 кГц.
        session_config = {
            "type": "session.update",
            "session": {
                "model": self.model,
                "instructions": self.instructions,
                "modalities": self.response_modalities,
                "voice": self.voice,
                "input_audio_format": REALTIME_INPUT_FORMAT,
                "output_audio_format": REALTIME_OUTPUT_FORMAT,
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 500,
                    "silence_duration_ms": 800,
                },
                "input_audio_transcription": {
                    "model": "whisper-1",
                    "language": "ru",
                },
            },
        }
        
        logger.info(
            "Конфигурация сессии: model=%s, voice=%s, instructions_len=%d, "
            "первые 300 символов инструкций: %s...",
            self.model,
            self.voice,
            len(self.instructions),
            self.instructions[:300],
        )

        await self.send_event(session_config)
        logger.info(
            "[SESSION] Отправлена конфигурация сессии (session.update), "
            "session_uuid=%s, instructions_len=%d",
            self.session_uuid,
            len(self.instructions),
        )

        # Отправляем событие для создания первого ответа (приветствие)
        # OpenAI Realtime НЕ генерирует ответ автоматически, нужно явно запросить
        greeting_event = {
            "type": "response.create",
            "response": {
                "modalities": self.response_modalities,
            }
        }
        await self.send_event(greeting_event)
        logger.info(
            "[GREETING] Отправлен запрос на создание приветствия (response.create), "
            "session_uuid=%s",
            self.session_uuid,
        )

        logger.info(
            "Сессия настроена: modalities=%s, input_rate=%s, output_rate=%s, "
            "instructions_len=%d, session_uuid=%s",
            self.response_modalities,
            self.input_rate,
            self.output_rate,
            len(self.instructions),
            self.session_uuid,
        )

    # ========= ОТПРАВКА PCM В OPENAI =========

    async def _forward_pcm_to_openai(self) -> None:
        """
        Читает PCM из очереди и отправляет в Realtime API
        через input_audio_buffer.append.
        
        Оптимизация: ресемплинг выполняется с 8 кГц до 24 кГц
        (нативный формат для OpenAI Realtime API) для лучшего качества.
        """
        logger.info(
            "Старт _forward_pcm_to_openai (session_uuid=%s)",
            self.session_uuid,
        )

        try:
            while True:
                pcm = await self.pcm_queue.get()

                # На всякий случай пропускаем пустые чанки
                if not pcm:
                    continue

                if len(pcm) < MIN_OPENAI_INPUT_CHUNK:
                    aggregated = bytearray(pcm)
                    while len(aggregated) < MIN_OPENAI_INPUT_CHUNK:
                        try:
                            extra = self.pcm_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        if extra:
                            aggregated.extend(extra)
                    pcm = bytes(aggregated)

                if self.input_rate != DEFAULT_SAMPLE_RATE:
                    try:
                        pcm = AudioConverter.resample_audio(
                            pcm,
                            DEFAULT_SAMPLE_RATE,
                            self.input_rate,
                        )
                    except Exception as e:
                        logger.error(
                            "Ошибка ресемплинга входящего PCM "
                            "(session_uuid=%s): %s",
                            self.session_uuid,
                            e,
                            exc_info=True,
                        )
                        continue

                if not self.ws:
                    logger.error(
                        "Нет WebSocket при отправке PCM "
                        "(session_uuid=%s), дроп чанка",
                        self.session_uuid,
                    )
                    continue

                audio_base64 = base64.b64encode(pcm).decode("utf-8")
                await self.send_event(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": audio_base64,
                    }
                )

                self._bytes_sent_to_openai += len(pcm)
                # Логируем периодически, чтобы не спамить
                if self._bytes_sent_to_openai % 6400 == 0 or self._bytes_sent_to_openai < 6400:
                    logger.info(
                        "[PCM] Отправлен PCM в OpenAI: session_uuid=%s, "
                        "chunk=%d байт, всего отправлено=%d байт "
                        "(awaiting_response=%s)",
                        self.session_uuid,
                        len(pcm),
                        self._bytes_sent_to_openai,
                        self._awaiting_response,
                    )
                else:
                    logger.debug(
                        "[PCM] Отправлен PCM в OpenAI: session_uuid=%s, "
                        "chunk=%d байт, всего отправлено=%d байт",
                        self.session_uuid,
                        len(pcm),
                        self._bytes_sent_to_openai,
                    )

        except asyncio.CancelledError:
            logger.info(
                "_forward_pcm_to_openai отменён (session_uuid=%s)",
                self.session_uuid,
            )
        except Exception as e:
            logger.error(
                "Ошибка при отправке PCM в OpenAI (session_uuid=%s): %s",
                self.session_uuid,
                e,
                exc_info=True,
            )

    async def _wait_pcm_queue_flush(self, timeout: float = 0.6) -> bool:
        """
        УСТАРЕЛО: При server_vad commit не нужен, метод не используется.
        Оставляем для совместимости.
        """
        logger.debug(
            "[DEPRECATED] _wait_pcm_queue_flush вызван (session_uuid=%s)",
            self.session_uuid,
        )
        return False

    def _calculate_rms(self, pcm: bytes) -> float:
        """
        Вычисляет RMS (Root Mean Square) для PCM данных.
        
        :param pcm: PCM16 данные
        :return: Нормализованный RMS (0.0 - 1.0)
        """
        if not pcm:
            return 0.0

        samples = np.frombuffer(pcm, dtype="<i2")
        if samples.size == 0:
            return 0.0

        rms = float(
            np.sqrt(np.mean(np.square(samples.astype(np.float32))))
        )

        # Нормализуем RMS для 16-bit PCM (диапазон -32768..32767)
        # Максимальный RMS для полной громкости ≈ 23170
        return rms / 32768.0

    def _check_local_barge_in(self, rms_normalized: float) -> bool:
        """
        Проверяет локальный barge-in по нескольким фреймам подряд с высоким RMS.
        
        :param rms_normalized: Нормализованное RMS значение
        :return: True если обнаружен barge-in
        """
        if not self._enable_local_barge_in:
            return False

        if not self.audio_handler.running:
            # Воспроизведение не активно - не проверяем barge-in
            self._consecutive_high_rms = 0
            return False

        if rms_normalized >= self.vad_threshold:
            self._consecutive_high_rms += 1
            self._recent_rms_values.append(rms_normalized)
            # Храним только последние 10 значений
            if len(self._recent_rms_values) > 10:
                self._recent_rms_values.pop(0)

            if self._consecutive_high_rms >= self._barge_in_frames_threshold:
                avg_rms = sum(self._recent_rms_values[-self._barge_in_frames_threshold:]) / self._barge_in_frames_threshold
                logger.info(
                    "[LOCAL-BARGE-IN] ✅ Обнаружено перебивание по локальному VAD "
                    "(session_uuid=%s, consecutive_frames=%d, avg_rms=%.6f, threshold=%.6f)",
                    self.session_uuid,
                    self._consecutive_high_rms,
                    avg_rms,
                    self.vad_threshold,
                )
                self._consecutive_high_rms = 0
                return True
        else:
            # RMS ниже порога - сбрасываем счётчик
            self._consecutive_high_rms = 0

        return False

    def _track_voice_activity(self, pcm: bytes) -> None:
        """
        Отслеживает голосовую активность (VAD) и проверяет локальный barge-in.
        """
        if self.vad_silence_window <= 0 or not pcm:
            return

        rms_normalized = self._calculate_rms(pcm)
        if rms_normalized == 0.0:
            return

        # Проверка локального barge-in (если включен)
        if self._check_local_barge_in(rms_normalized):
            self.audio_handler.interrupt_playback()
            # Также сбрасываем состояние активного ответа
            if self._active_response_id:
                logger.info(
                    "[LOCAL-BARGE-IN] Сброс активного ответа "
                    "(session_uuid=%s, response_id=%s)",
                    self.session_uuid,
                    self._active_response_id,
                )
                self._active_response_id = None
                self._awaiting_response = False

        # Обновляем время последней голосовой активности
        if rms_normalized >= self.vad_threshold:
            try:
                now = asyncio.get_running_loop().time()
            except RuntimeError:
                return

            self._last_voice_ts = now
            logger.debug(
                "VAD: обнаружена речь (session_uuid=%s, rms_normalized=%.6f, "
                "threshold=%.6f, consecutive_high_rms=%d)",
                self.session_uuid,
                rms_normalized,
                self.vad_threshold,
                self._consecutive_high_rms,
            )

    async def _silence_watchdog(self) -> None:
        # При server_vad watchdog не нужен - сервер сам управляет turn detection
        # Оставляем метод для совместимости, но он не будет вызывать commit
        if self.vad_silence_window <= 0:
            return

        logger.info(
            "[VAD] Watchdog отключён (server_vad активен, session_uuid=%s)",
            self.session_uuid,
        )

    def _extract_response_id(self, event: dict) -> str | None:
        return event.get("response_id") or event.get("response", {}).get("id")

    @staticmethod
    def _extract_item_id(event: dict) -> str | None:
        item = event.get("item", {})
        return event.get("item_id") or item.get("id")

    def _remember_transcript_delta(self, event: dict) -> None:
        transcript_delta = event.get("delta")
        if not transcript_delta:
            return

        response_id = self._extract_response_id(event) or "default"
        buffer = self._transcript_buffers.setdefault(response_id, [])
        buffer.append(transcript_delta)

    def _finalize_transcript(self, event: dict) -> str:
        response_id = self._extract_response_id(event) or "default"
        text = event.get("text", "").strip()

        if not text:
            fragments = self._transcript_buffers.get(response_id, [])
            text = "".join(fragments).strip()

        self._transcript_buffers.pop(response_id, None)

        if text and self.transcript_callback:
            self.transcript_callback(text)

        return text

    def _begin_response(self, response_id: str | None) -> None:
        if not response_id:
            return

        if self._active_response_id and self._active_response_id != response_id:
            logger.info(
                "Переключение активного ответа "
                "(session_uuid=%s, old=%s, new=%s) — останавливаем воспроизведение",
                self.session_uuid,
                self._active_response_id,
                response_id,
            )
            self.audio_handler.interrupt_playback()

        self._active_response_id = response_id
        self._response_audio_bytes = 0
        self._response_audio_chunks = 0
        logger.info(
            "Старт воспроизведения ответа (session_uuid=%s, response_id=%s)",
            self.session_uuid,
            response_id,
        )

    def _finish_response(self, response_id: str | None, status: str) -> None:
        if not response_id:
            logger.info(
                "Завершение ответа без response_id (status=%s, session_uuid=%s)",
                status,
                self.session_uuid,
            )
            return

        if self._active_response_id != response_id:
            logger.info(
                "Получен %s для неактивного ответа "
                "(session_uuid=%s, response_id=%s, active=%s)",
                status,
                self.session_uuid,
                response_id,
                self._active_response_id,
            )
        else:
            logger.info(
                "Ответ %s (session_uuid=%s, response_id=%s, "
                "audio_chunks=%d, audio_bytes=%d)",
                status,
                self.session_uuid,
                response_id,
                self._response_audio_chunks,
                self._response_audio_bytes,
            )
            self._active_response_id = None
            self._response_audio_bytes = 0
            self._response_audio_chunks = 0
    # ========= ПРИЁМ СОБЫТИЙ ОТ OPENAI =========

    async def receive_events(self) -> None:
        """
        Читает события из Realtime API и:
        - при response.audio.delta → кладёт аудио в AudioHandler
        - логирует транскрипты
        - при начале речи абонента останавливает воспроизведение
        - при окончании речи абонента — коммитит буфер и создаёт ответ
        """
        if not self.ws:
            logger.error(
                "Невозможно запустить receive_events без WebSocket "
                "(session_uuid=%s)",
                self.session_uuid,
            )
            return

        logger.info(
            "Старт receive_events (session_uuid=%s)",
            self.session_uuid,
        )

        try:
            async for message in self.ws:
                try:
                    event = json.loads(message)
                    event_type = event.get("type")
                    response_id = self._extract_response_id(event)
                    item_id = self._extract_item_id(event)

                    logger.debug(
                        "Realtime событие (session_uuid=%s, type=%s, "
                        "response_id=%s, item_id=%s, active_response=%s)",
                        self.session_uuid,
                        event_type,
                        response_id,
                        item_id,
                        self._active_response_id,
                    )

                    # ---- Аудио от модели → в Asterisk ----
                    if event_type == "response.audio.delta":
                        audio_base64 = event.get("delta", "")
                        if audio_base64:
                            audio_bytes = base64.b64decode(audio_base64)
                            chunk_len = len(audio_bytes)
                            if response_id and not self._active_response_id:
                                self._begin_response(response_id)
                            if (
                                self._active_response_id
                                and response_id
                                and response_id != self._active_response_id
                            ):
                                logger.warning(
                                    "[AUDIO] Получен audio.delta чужого ответа — ИГНОРИРУЕМ "
                                    "(session_uuid=%s, response_id=%s, "
                                    "active=%s, chunk=%d байт, total_chunks=%d, total_bytes=%d)",
                                    self.session_uuid,
                                    response_id,
                                    self._active_response_id,
                                    chunk_len,
                                    self._response_audio_chunks,
                                    self._response_audio_bytes,
                                )
                                continue

                            self._response_audio_chunks += 1
                            self._response_audio_bytes += chunk_len
                            # Логируем только периодически (каждые 10 чанков) или при завершении
                            if self._response_audio_chunks % 10 == 0 or chunk_len < 12000:
                                logger.info(
                                    "response.audio.delta (session_uuid=%s, "
                                    "response_id=%s, chunk=%d байт, "
                                    "total_chunks=%d, total_bytes=%d)",
                                    self.session_uuid,
                                    response_id or self._active_response_id,
                                    chunk_len,
                                    self._response_audio_chunks,
                                    self._response_audio_bytes,
                                )
                            self.audio_handler.enqueue_audio(audio_bytes)
                        else:
                            logger.warning(
                                "response.audio.delta без данных "
                                "(session_uuid=%s)",
                                self.session_uuid,
                            )
                    
                    # ---- Проверка output_audio_buffer событий ----
                    elif event_type and event_type.startswith("output_audio_buffer."):
                        logger.info(
                            "Событие output_audio_buffer: "
                            "session_uuid=%s, type=%s, event=%s",
                            self.session_uuid,
                            event_type,
                            event,
                        )

                    # ---- ВХОДЯЩАЯ ТРАНСКРИПЦИЯ (что говорит пользователь) ----
                    elif event_type == "conversation.item.input_audio_transcription.delta":
                        delta = event.get("delta", "")
                        if delta:
                            logger.info(
                                "🎤 ПОЛЬЗОВАТЕЛЬ говорит (session_uuid=%s): %s",
                                self.session_uuid,
                                delta,
                            )
                    
                    elif event_type == "conversation.item.input_audio_transcription.done":
                        text = event.get("text", "").strip()
                        if text:
                            logger.info(
                                "🎤 ПОЛЬЗОВАТЕЛЬ сказал (session_uuid=%s): %s",
                                self.session_uuid,
                                text,
                            )

                    # ---- ИСХОДЯЩАЯ ТРАНСКРИПЦИЯ (что отвечает бот) ----
                    elif event_type == "response.audio_transcript.delta":
                        transcript_delta = event.get("delta", "")
                        if transcript_delta:
                            logger.info(
                                "🤖 БОТ отвечает (session_uuid=%s): %s",
                                self.session_uuid,
                                transcript_delta,
                            )
                            self._remember_transcript_delta(event)

                    elif event_type == "response.output_text.delta":
                        delta = event.get("delta", "")
                        if delta:
                            logger.info(
                                "🤖 БОТ (текст, session_uuid=%s, response_id=%s): %s",
                                self.session_uuid,
                                response_id,
                                delta.strip(),
                            )

                    elif event_type == "response.output_text.done":
                        text = event.get("text", "").strip()
                        if text:
                            logger.info(
                                "🤖 БОТ ответил полностью (session_uuid=%s, response_id=%s): %s",
                                self.session_uuid,
                                response_id,
                                text,
                            )

                    # ---- Финальный транскрипт ответа бота ----
                    elif event_type == "response.audio_transcript.done":
                        transcript = self._finalize_transcript(event)
                        if transcript:
                            logger.info(
                                "🤖 БОТ ответил (полная транскрипция, session_uuid=%s): %s",
                                self.session_uuid,
                                transcript,
                            )

                    # ---- Сигнал: пользователь начал говорить (barge-in от OpenAI) ----
                    elif event_type == "input_audio_buffer.speech_started":
                        # Server-side barge-in: останавливаем воспроизведение текущего ответа
                        was_running = self.audio_handler.running
                        self.audio_handler.interrupt_playback()
                        # Сбрасываем состояние активного ответа, если есть
                        if self._active_response_id:
                            logger.info(
                                "[SERVER-BARGE-IN] ✅ Речь абонента началась (OpenAI VAD), "
                                "прерываем ответ (session_uuid=%s, active_response_id=%s, "
                                "was_running=%s)",
                                self.session_uuid,
                                self._active_response_id,
                                was_running,
                            )
                            self._active_response_id = None
                            self._awaiting_response = False
                        else:
                            logger.info(
                                "[SERVER-BARGE-IN] ✅ Речь абонента началась (OpenAI VAD) "
                                "(session_uuid=%s, was_running=%s)",
                                self.session_uuid,
                                was_running,
                            )

                    # ---- Сигнал: пользователь закончил говорить ----
                    elif event_type == "input_audio_buffer.speech_stopped":
                        # При server_vad сервер сам создаст response, commit не нужен
                        silence_duration = (
                            (asyncio.get_event_loop().time() - self._last_voice_ts)
                            if self._last_voice_ts
                            else None
                        )
                        logger.info(
                            "[VAD] Речь абонента закончена (session_uuid=%s), "
                            "ожидаем автоматический ответ от сервера "
                            "(awaiting_response=%s, silence_duration=%.3f сек)",
                            self.session_uuid,
                            self._awaiting_response,
                            silence_duration if silence_duration else 0.0,
                        )
                        # Не вызываем commit и response.create - сервер сделает это сам

                    elif event_type == "response.created":
                        created_id = event.get("response", {}).get("id")
                        old_active = self._active_response_id
                        
                        logger.info(
                            "[RESPONSE] response.created получен "
                            "(session_uuid=%s, response_id=%s, "
                            "previous_active=%s, awaiting_response=%s)",
                            self.session_uuid,
                            created_id,
                            old_active,
                            self._awaiting_response,
                        )
                        
                        # Если уже есть активный ответ — останавливаем его воспроизведение
                        # и переключаемся на новый (server_vad может создать новый ответ)
                        if old_active and old_active != created_id:
                            logger.info(
                                "[RESPONSE] Переключение на новый ответ "
                                "(session_uuid=%s, old=%s, new=%s)",
                                self.session_uuid,
                                old_active,
                                created_id,
                            )
                            self.audio_handler.interrupt_playback()
                        
                        self._awaiting_response = True
                        self._begin_response(created_id)

                    elif event_type == "response.completed":
                        self._awaiting_response = False
                        self._last_voice_ts = None
                        self._commit_in_progress = False  # Сбрасываем флаг
                        completed_id = event.get("response", {}).get("id")
                        if completed_id:
                            self._transcript_buffers.pop(completed_id, None)
                        self._finish_response(completed_id, "completed")

                    elif event_type == "response.canceled":
                        self._awaiting_response = False
                        self._commit_in_progress = False  # Сбрасываем флаг
                        canceled_id = event.get("response", {}).get("id")
                        self._finish_response(canceled_id, "canceled")

                    elif event_type == "response.error":
                        self._awaiting_response = False
                        self._commit_in_progress = False  # Сбрасываем флаг
                        response = event.get("response", {}) or {}
                        error_id = response.get("id")
                        if error_id:
                            self._transcript_buffers.pop(error_id, None)
                        self._finish_response(error_id, "error")
                        logger.error(
                            "Ошибка ответа модели (session_uuid=%s): %s",
                            self.session_uuid,
                            event,
                        )

                    # Можно дополнительно логировать другие типы для отладки
                    else:
                        # Не спамим INFO, но для отладки можно включить
                        logger.debug(
                            "Получено неиспользуемое событие: "
                            "session_uuid=%s, type=%s, raw=%s",
                            self.session_uuid,
                            event_type,
                            event,
                        )

                except json.JSONDecodeError as e:
                    logger.error(
                        "Ошибка парсинга JSON от Realtime API "
                        "(session_uuid=%s): %s, raw=%s",
                        self.session_uuid,
                        e,
                        message,
                    )
                except Exception as e:
                    logger.error(
                        "Ошибка при обработке события Realtime API "
                        "(session_uuid=%s): %s",
                        self.session_uuid,
                        e,
                        exc_info=True,
                    )

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(
                "WebSocket соединение закрыто (session_uuid=%s): %s",
                self.session_uuid,
                e,
            )
        except asyncio.CancelledError:
            logger.info(
                "receive_events отменён (session_uuid=%s)",
                self.session_uuid,
            )
        except Exception as e:
            logger.error(
                "Ошибка в receive_events (session_uuid=%s): %s",
                self.session_uuid,
                e,
                exc_info=True,
            )

    # ========= ОСНОВНОЙ ЦИКЛ КЛИЕНТА =========

    async def run(self) -> None:
        """
        Основной метод: устанавливает соединение и запускает
        две параллельные задачи:
        - отправка PCM в OpenAI
        - приём событий от OpenAI
        """
        logger.info(
            "Запуск AudioWebSocketClient.run для session_uuid=%s",
            self.session_uuid,
        )

        forward_task: asyncio.Task | None = None
        receive_task: asyncio.Task | None = None
        silence_task: asyncio.Task | None = None

        try:
            # 1. Подключаемся
            await self.connect()

            if not self.ws:
                logger.error(
                    "Не удалось установить WebSocket соединение "
                    "(session_uuid=%s)",
                    self.session_uuid,
                )
                return

            # 2. После успешного connect запускаем задачи
            forward_task = asyncio.create_task(
                self._forward_pcm_to_openai()
            )
            receive_task = asyncio.create_task(self.receive_events())
            # При server_vad watchdog не нужен - сервер сам управляет turn detection
            # if self.vad_silence_window > 0:
            #     silence_task = asyncio.create_task(self._silence_watchdog())
            #     self._silence_task = silence_task

            logger.info(
                "AudioWebSocketClient задачи запущены "
                "(session_uuid=%s)",
                self.session_uuid,
            )

            # 3. Ждём, пока одна из задач завершится (ошибка/закрытие)
            done, pending = await asyncio.wait(
                {forward_task, receive_task},
                return_when=asyncio.FIRST_EXCEPTION,
            )

            # Если одна упала — отменяем вторую
            for task in pending:
                task.cancel()

            # Пробрасываем исключения, если были
            for task in done:
                exc = task.exception()
                if exc:
                    raise exc

        except asyncio.CancelledError:
            logger.info(
                "AudioWebSocketClient.run отменён (session_uuid=%s)",
                self.session_uuid,
            )
        except Exception as e:
            logger.error(
                "Ошибка в AudioWebSocketClient.run (session_uuid=%s): %s",
                self.session_uuid,
                e,
                exc_info=True,
            )
        finally:
            self.connected = False
            self._connected_event.clear()

            # Аккуратно отменяем задачи, если ещё живы
            for task in (forward_task, receive_task, silence_task):
                if task and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            self._silence_task = None

            # Закрываем WebSocket
            if self.ws:
                try:
                    await self.ws.close()
                    logger.info(
                        "WebSocket закрыт (session_uuid=%s)",
                        self.session_uuid,
                    )
                except Exception as e:
                    logger.error(
                        "Ошибка при закрытии WebSocket "
                        "(session_uuid=%s): %s",
                        self.session_uuid,
                        e,
                    )
                self.ws = None

            logger.info(
                "AudioWebSocketClient.run завершён (session_uuid=%s)",
                self.session_uuid,
            )

    async def wait_until_connected(self, timeout: float = 10.0) -> bool:
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def request_response(self, reason: str = "manual") -> None:
        """
        УСТАРЕЛО: При server_vad ответы создаются автоматически сервером.
        Метод оставлен для совместимости (может использоваться для текстовых сценариев).
        """
        logger.warning(
            "[DEPRECATED] request_response вызван при server_vad "
            "(reason=%s, session_uuid=%s). Это не должно происходить для голосового потока.",
            reason,
            self.session_uuid,
        )

    async def _commit_and_request(self, reason: str) -> None:
        """
        УСТАРЕЛО: При server_vad commit и response.create не нужны для голосового потока.
        Оставляем метод для совместимости (может использоваться для текстовых сценариев),
        но для голосового потока он не должен вызываться.
        """
        logger.warning(
            "[DEPRECATED] _commit_and_request вызван при server_vad "
            "(reason=%s, session_uuid=%s). Это не должно происходить для голосового потока.",
            reason,
            self.session_uuid,
        )
