import asyncio
import logging
from typing import Callable

from src.constants import (
    DRAIN_CHUNK_SIZE,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_SAMPLE_WIDTH,
    ENABLE_JITTER_BUFFER,
    OPENAI_OUTPUT_RATE,
)
from src.utils import AudioConverter
from src.jitter_buffer import OutputBuffer

logger = logging.getLogger(__name__)
RTP_PCM_FRAME = DEFAULT_SAMPLE_RATE * DEFAULT_SAMPLE_WIDTH // 50  # 20 мс


class AudioHandler:
    def __init__(
        self,
        session_uuid: str,
        send_pcm_callback: Callable[[bytes], None],
    ):
        self.session_uuid = session_uuid
        self.send_pcm_callback = send_pcm_callback
        self.audio_queue = asyncio.Queue()
        self.playback_task = None
        self.running = False
        self._pending_pcm = bytearray()
        self._packets_sent = 0
        
        # Создаём output-buffer для сглаживания исходящего потока (OpenAI → Asterisk)
        # Если буфер отключен, callback будет напрямую вызывать send_pcm_callback
        self.output_buffer = (
            OutputBuffer(output_callback=send_pcm_callback)
            if ENABLE_JITTER_BUFFER
            else None
        )

    def enqueue_audio(self, audio_data: bytes) -> None:
        if not self.running:
            self.running = True
            self.playback_task = asyncio.create_task(self._playback_loop())
            logger.info(
                "Старт playback loop (session_uuid=%s)",
                self.session_uuid,
            )

        self.audio_queue.put_nowait(audio_data)
        queue_size = self.audio_queue.qsize()
        logger.debug(
            "Аудио добавлено в очередь (session_uuid=%s, chunk=%d байт, "
            "queue_size=%d)",
            self.session_uuid,
            len(audio_data),
            queue_size,
        )
        if queue_size > 50:
            logger.warning(
                "Очередь аудио для воспроизведения разрастается "
                "(session_uuid=%s, size=%d)",
                self.session_uuid,
                queue_size,
            )

    async def _playback_loop(self) -> None:
        batch = bytearray()

        while self.running or not self.audio_queue.empty():
            try:
                audio_data = await asyncio.wait_for(
                    self.audio_queue.get(),
                    timeout=0.1,
                )
                batch.extend(audio_data)

                if len(batch) >= DRAIN_CHUNK_SIZE:
                    await self._flush_batch(batch, reason="batch_full")

            except asyncio.TimeoutError:
                if batch:
                    await self._flush_batch(batch, reason="timeout")

            except Exception as e:
                logger.error(
                    "Ошибка в playback loop (session_uuid=%s): %s",
                    self.session_uuid,
                    e,
                    exc_info=True,
                )

        if batch:
            try:
                await self._flush_batch(batch, reason="final_flush")
            except Exception as e:
                logger.error(
                    "Ошибка при финальной отправке (session_uuid=%s): %s",
                    self.session_uuid,
                    e,
                )

        if self._pending_pcm:
            logger.info(
                "Сброс незавершённого PCM хвоста (%d байт, session_uuid=%s)",
                len(self._pending_pcm),
                self.session_uuid,
            )
            self._pending_pcm.clear()

    async def _flush_batch(self, batch: bytearray, reason: str) -> None:
        if not batch:
            return

        payload = bytes(batch)
        batch.clear()

        if not payload:
            return

        logger.debug(
            "Флаш аудио батча (session_uuid=%s, reason=%s, raw_len=%d байт)",
            self.session_uuid,
            reason,
            len(payload),
        )

        resampled = AudioConverter.resample_audio(
            payload,
            OPENAI_OUTPUT_RATE,
            DEFAULT_SAMPLE_RATE,
        )

        if not resampled:
            logger.debug(
                "Результат ресемплинга пуст (session_uuid=%s)",
                self.session_uuid,
            )
            return

        # Добавляем ресемпленные данные в output-buffer (если включен)
        # или сразу нарезаем на фреймы и отправляем
        if self.output_buffer:
            # OutputBuffer сам нарежет на фреймы и отдаст в ровном ритме
            self.output_buffer.add_chunk(resampled)
        else:
            # Старое поведение: нарезаем на фреймы и сразу отправляем
            self._pending_pcm.extend(resampled)
            while len(self._pending_pcm) >= RTP_PCM_FRAME:
                chunk = bytes(self._pending_pcm[:RTP_PCM_FRAME])
                del self._pending_pcm[:RTP_PCM_FRAME]
                self.send_pcm_callback(chunk)
                self._packets_sent += 1
                # Логируем только периодически (каждые 500 пакетов)
                if self._packets_sent % 500 == 0:
                    logger.debug(
                        "RTP фрейм отправлен (session_uuid=%s, packets_sent=%d, tail=%d)",
                        self.session_uuid,
                        self._packets_sent,
                        len(self._pending_pcm),
                    )

        if self._pending_pcm:
            logger.debug(
                "Остаток PCM после нарезки (session_uuid=%s, tail_len=%d байт)",
                self.session_uuid,
                len(self._pending_pcm),
            )

    def stop_playback(self) -> None:
        self.running = False
        if self.playback_task:
            self.playback_task.cancel()
            logger.debug(
                "Playback task отменён (session_uuid=%s)",
                self.session_uuid,
            )

    def interrupt_playback(self) -> None:
        """
        Останавливает воспроизведение и очищает накопленную очередь,
        чтобы не говорить поверх абонента.
        """
        self.stop_playback()
        drained = 0
        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                drained += 1

        if drained:
            logger.debug(
                "Очередь аудио очищена (session_uuid=%s, drained_chunks=%d)",
                self.session_uuid,
                drained,
            )
        if self._pending_pcm:
            logger.debug(
                "Сброшен хвост PCM при interrupt (session_uuid=%s, tail_len=%d)",
                self.session_uuid,
                len(self._pending_pcm),
            )
            self._pending_pcm.clear()

    async def cleanup(self) -> None:
        self.stop_playback()
        if self.playback_task:
            try:
                await self.playback_task
            except asyncio.CancelledError:
                pass
        
        # Очищаем output-buffer
        if self.output_buffer:
            self.output_buffer.stop()
            try:
                await self.output_buffer.flush()
            except Exception as e:
                logger.error(
                    "Ошибка при очистке output-buffer (session_uuid=%s): %s",
                    self.session_uuid,
                    e,
                )
