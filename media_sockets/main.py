import asyncio
import logging
import struct
import uuid
from typing import Callable

from src.codecs import alaw_to_pcm16, pcm16_to_alaw
from src.constants import (
    DEFAULT_SAMPLE_WIDTH,
    ENABLE_JITTER_BUFFER,
    HOST,
    PORT,
)
from src.audio_websocket_client import AudioWebSocketClient
from src.audio_handler import AudioHandler
from src.jitter_buffer import JitterBuffer

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Отключаем DEBUG-логирование websockets, чтобы не логировать API ключ
logging.getLogger("websockets.client").setLevel(logging.WARNING)


def make_send_pcm_callback(
    write_queue: asyncio.Queue[bytes],
) -> Callable[[bytes], None]:
    """
    Создаёт синхронный callback для отправки PCM данных.
    Данные помещаются в очередь, из которой их читает write_loop.
    """

    def send_pcm(data: bytes) -> None:
        try:
            write_queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning(
                "Очередь записи переполнена, PCM данные пропущены (размер=%d байт)",
                len(data),
            )

    return send_pcm


async def write_loop(
    transport: asyncio.DatagramTransport,
    remote_addr: tuple[str, int],
    write_queue: asyncio.Queue[bytes],
    session: "UdpSession",
) -> None:
    """
    Асинхронный цикл записи PCM данных в UDP-соединение, упаковывая их в RTP пакеты.
    """
    # Время последней отправки пакета (для соблюдения темпа RTP)
    last_send_time: float | None = None
    RTP_PACKET_INTERVAL = 0.020  # 20 мс между пакетами (для G.711 A-law)
    
    try:
        while True:
            try:
                data = await asyncio.wait_for(write_queue.get(), timeout=1.0)
                
                # Если inbound_pt/ssrc ещё не известны, используем дефолты
                # Для G.711 A-law стандартный payload type = 8
                pt = session.inbound_pt if session.inbound_pt is not None else 8
                ssrc = session.ssrc if session.ssrc is not None else 0x12345678
                
                # Проверка: для A-law ожидается pt=8
                if pt != 8:
                    logger.warning(
                        "Исходящий RTP использует pt=%s вместо ожидаемого 8 "
                        "(G.711 A-law). Возможны проблемы с аудио! "
                        "(session_uuid=%s)",
                        pt,
                        session.session_uuid,
                    )
                
                # Инкремент sequence number
                session.seq_out = (session.seq_out + 1) & 0xFFFF
                
                if len(data) == 0 or len(data) % DEFAULT_SAMPLE_WIDTH != 0:
                    logger.warning(
                        "Неверный размер PCM чанка, пропущено (len=%d, "
                        "session_uuid=%s)",
                        len(data),
                        session.session_uuid,
                    )
                    continue

                samples = len(data) // DEFAULT_SAMPLE_WIDTH
                session.ts_out = (session.ts_out + samples) & 0xFFFFFFFF
                
                # Логируем timestamp только очень редко (для отладки)
                # if session.seq_out % 1000 == 0:
                #     logger.debug(
                #         "RTP timestamp: seq=%s, ts=%s, samples=%s (session_uuid=%s)",
                #         session.seq_out,
                #         session.ts_out,
                #         samples,
                #         session.session_uuid,
                #     )
                
                # Конвертируем PCM16 → G.711 A-law
                try:
                    payload = pcm16_to_alaw(data)
                except ValueError as exc:
                    logger.error(
                        "Ошибка кодирования PCM16→A-law (session_uuid=%s): %s",
                        session.session_uuid,
                        exc,
                    )
                    continue

                # Проверка размера payload: для G.711 A-law ожидается 160 байт (20 мс)
                expected_payload_size = len(data) // 2  # PCM16 → A-law: 2:1
                if len(payload) != expected_payload_size:
                    logger.warning(
                        "[RTP] Неожиданный размер payload после кодирования "
                        "(session_uuid=%s, PCM_len=%d, payload_len=%d, expected=%d)",
                        session.session_uuid,
                        len(data),
                        len(payload),
                        expected_payload_size,
                    )

                # Проверка: для G.711 A-law стандартный размер = 160 байт (20 мс)
                if len(payload) != 160:
                    logger.warning(
                        "[RTP] НЕСТАНДАРТНЫЙ размер payload! "
                        "(session_uuid=%s, payload_len=%d байт, ожидается 160 байт) "
                        "Это может вызывать скрипы/искажения!",
                        session.session_uuid,
                        len(payload),
                    )

                # Сборка RTP заголовка (12 байт)
                # v=2, p=0, x=0, cc=0 → первый байт = 0x80
                # m=0, pt → второй байт = pt & 0x7F
                header = struct.pack(
                    "!BBHII",
                    0x80,  # V=2, P=0, X=0, CC=0
                    pt & 0x7F,  # без marker
                    session.seq_out,  # sequence number
                    session.ts_out,  # timestamp
                    ssrc,  # SSRC
                )
                
                # Объединение заголовка и PCM данных
                packet = header + payload
                
                # Задержка для соблюдения темпа RTP: 20 мс между пакетами
                # (для G.711 A-law: 160 байт = 20 мс при 8 кГц)
                # Это предотвращает отправку пакетов пачками и устраняет скрипы
                if last_send_time is not None:
                    elapsed = asyncio.get_running_loop().time() - last_send_time
                    if elapsed < RTP_PACKET_INTERVAL:
                        await asyncio.sleep(RTP_PACKET_INTERVAL - elapsed)
                
                # Отправка через transport
                transport.sendto(packet, remote_addr)
                last_send_time = asyncio.get_running_loop().time()
                
                # Логируем только при проблемах или очень редко
                if len(payload) != 160:
                    logger.warning(
                        "[RTP] Отправлен RTP с нестандартным размером: "
                        "addr=%s, pt=%s, seq=%s, payload_len=%d байт (ожидается 160) "
                        "(session_uuid=%s)",
                        remote_addr,
                        pt,
                        session.seq_out,
                        len(payload),
                        session.session_uuid,
                    )
                elif session.seq_out % 500 == 0:
                    logger.debug(
                        "[RTP] Отправлен RTP в Asterisk: addr=%s, pt=%s, seq=%s, ts=%s, "
                        "payload_len=%d байт (session_uuid=%s)",
                        remote_addr,
                        pt,
                        session.seq_out,
                        session.ts_out,
                        len(payload),
                        session.session_uuid,
                    )
                
            except asyncio.TimeoutError:
                # Проверяем, не закрыт ли transport
                if transport.is_closing():
                    break
                continue
    except Exception as e:
        logger.error(
            "Ошибка в write_loop: %s",
            e,
            exc_info=True,
        )


class UdpSession:
    def __init__(
        self,
        addr: tuple[str, int],
        transport: asyncio.DatagramTransport,
    ):
        self.addr = addr
        self.transport = transport
        self.session_uuid = str(uuid.uuid4())
        self.remote_addr = addr
        
        # RTP параметры
        self.inbound_pt: int | None = None
        self.ssrc: int | None = None
        self.seq_out: int = 0
        self.ts_out: int = 0
        
        logger.info(
            "Создана новая UDP-сессия для %s, uuid=%s",
            addr,
            self.session_uuid,
        )
        
        # Очередь для записи PCM данных обратно в Asterisk
        write_queue: asyncio.Queue[bytes] = asyncio.Queue()
        
        # Создаём callback для отправки PCM
        send_pcm_callback = make_send_pcm_callback(write_queue)
        
        # Создаём AudioHandler и AudioWebSocketClient
        self.audio_handler = AudioHandler(
            session_uuid=self.session_uuid,
            send_pcm_callback=send_pcm_callback,
        )
        self.client = AudioWebSocketClient(
            session_uuid=self.session_uuid,
            audio_handler=self.audio_handler,
        )
        
        # Создаём jitter-buffer для входящего потока (Asterisk → OpenAI)
        # Если буфер отключен, callback будет напрямую вызывать client.push_pcm
        jitter_callback = self.client.push_pcm
        self.jitter_buffer = JitterBuffer(
            output_callback=jitter_callback,
        ) if ENABLE_JITTER_BUFFER else None
        
        # Запускаем задачи
        self.client_task = asyncio.create_task(self.client.run())
        self.write_task = asyncio.create_task(
            write_loop(self.transport, self.remote_addr, write_queue, self)
        )
    
    async def handle_incoming_payload(self, payload: bytes, pt: int) -> None:
        """
        Обрабатывает входящие RTP payload от Asterisk (после парсинга RTP).
        """
        try:
            if pt == 8:
                pcm_payload = alaw_to_pcm16(payload)
                codec = "G.711 A-law"
            else:
                pcm_payload = payload
                codec = "linear PCM"

            # Логируем только периодически
            # (убрано избыточное логирование каждого пакета)

            if len(pcm_payload) % DEFAULT_SAMPLE_WIDTH != 0:
                logger.warning(
                    "PCM после декодирования имеет некорректную длину (%d байт), "
                    "session_uuid=%s",
                    len(pcm_payload),
                    self.session_uuid,
                )
                return

            # Передаём PCM16 8 kHz в jitter-buffer (если включен) или напрямую в клиент
            # Jitter-buffer сглаживает сетевой джиттер перед ресемплингом до 24 кГц
            if self.jitter_buffer:
                self.jitter_buffer.add_frame(pcm_payload)
            else:
                self.client.push_pcm(pcm_payload)
            
        except Exception as e:
            logger.error(
                "Ошибка обработки PCM от %s (session_uuid=%s): %s",
                self.addr,
                self.session_uuid,
                e,
                exc_info=True,
            )
    
    async def cleanup(self) -> None:
        logger.info(
            "[CLEANUP] Завершение сессии (session_uuid=%s, addr=%s)",
            self.session_uuid,
            self.addr,
        )
        
        # Останавливаем клиент
        self.client_task.cancel()
        try:
            await self.client_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(
                "Ошибка при завершении client_task (session_uuid=%s): %s",
                self.session_uuid,
                e,
            )
        
        # Останавливаем jitter-buffer
        if self.jitter_buffer:
            self.jitter_buffer.stop()
            try:
                await self.jitter_buffer.flush()
            except Exception as e:
                logger.error(
                    "Ошибка при очистке jitter-buffer (session_uuid=%s): %s",
                    self.session_uuid,
                    e,
                )
        
        # Останавливаем audio_handler
        await self.audio_handler.cleanup()
        
        # Останавливаем write_loop
        self.write_task.cancel()
        try:
            await self.write_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(
                "Ошибка при завершении write_task (session_uuid=%s): %s",
                self.session_uuid,
                e,
            )
        
        logger.info(
            "[CLEANUP] Сессия завершена (session_uuid=%s)",
            self.session_uuid,
        )


class AudioSocketUdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.transport = None
        self.sessions: dict[tuple[str, int], UdpSession] = {}
    
    def connection_made(self, transport):
        self.transport = transport
        sockname = transport.get_extra_info("sockname")
        logger.info(
            "UDP AudioSocket сервер (RTP/G.711 A-law) запущен, локальный адрес: %s",
            sockname,
        )
    
    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            ip, port = addr
            
            # Фильтрация self-test пакетов
            if ip == "127.0.0.1" and data.startswith(b"TEST-UDP-SELF"):
                logger.debug(
                    "UDP self-test пакет от %s:%d, игнорируем",
                    ip,
                    port,
                )
                return
            
            logger.info(
                "Получен UDP пакет: %d байт от %s:%d",
                len(data),
                ip,
                port,
            )
            
            # Минимальная проверка RTP заголовка
            if len(data) < 12:
                logger.warning(
                    "UDP пакет слишком короткий: %d байт (минимум 12 для RTP)",
                    len(data),
                )
                return
            
            # Парсинг RTP заголовка
            first_byte = data[0]
            version = (first_byte >> 6) & 0x03
            
            if version != 2:
                logger.warning(
                    "Неверная версия RTP: %d (ожидается 2), игнорируем пакет",
                    version,
                )
                return
            
            # Второй байт: payload type
            second_byte = data[1]
            pt = second_byte & 0x7F
            
            # Читаем seq, ts, ssrc
            seq = struct.unpack("!H", data[2:4])[0]
            ts = struct.unpack("!I", data[4:8])[0]
            ssrc = struct.unpack("!I", data[8:12])[0]
            
            # Извлекаем payload (после 12-байтного заголовка)
            header_len = 12
            payload = data[header_len:]
            
            # Получаем или создаём сессию
            session = self.sessions.get(addr)
            if session is None:
                logger.info(
                    "[SESSION] Создание новой UDP-сессии для адреса %s "
                    "(всего активных сессий: %d)",
                    addr,
                    len(self.sessions),
                )
                session = UdpSession(addr, self.transport)
                self.sessions[addr] = session
            else:
                logger.debug(
                    "[SESSION] Использование существующей сессии для %s "
                    "(session_uuid=%s)",
                    addr,
                    session.session_uuid,
                )
            
            # Если у сессии ещё не было inbound_pt/ssrc, сохраняем
            if session.inbound_pt is None or session.ssrc is None:
                session.inbound_pt = pt
                session.ssrc = ssrc
                session.seq_out = seq
                session.ts_out = ts
                logger.info(
                    "Инициализированы RTP параметры для %s: pt=%s, ssrc=%s, seq=%s, ts=%s",
                    addr,
                    pt,
                    ssrc,
                    seq,
                    ts,
                )
            
            # Логируем только периодически или при проблемах
            if seq % 500 == 0:
                logger.debug(
                    "Получен RTP от Asterisk: addr=%s, pt=%s, seq=%s, ts=%s, payload_len=%s",
                    addr,
                    pt,
                    seq,
                    ts,
                    len(payload),
                )
            
            # Передаём payload в сессию
            self.loop.create_task(session.handle_incoming_payload(payload, pt))
            
        except Exception as e:
            logger.exception(
                "Ошибка в datagram_received при обработке пакета от %s", addr
            )
    
    def connection_lost(self, exc):
        if exc:
            logger.error("UDP соединение потеряно: %s", exc)
        else:
            logger.info("UDP соединение закрыто")


async def udp_self_test():
    """
    Отправляет тестовый UDP пакет для проверки работы сервера.
    """
    await asyncio.sleep(1.0)
    logger.info("UDP self-test: отправка тестового пакета на 127.0.0.1:7575")
    try:
        loop = asyncio.get_event_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: asyncio.DatagramProtocol(),
            remote_addr=("127.0.0.1", 7575),
        )
        transport.sendto(b"TEST-UDP-SELF", ("127.0.0.1", 7575))
        await asyncio.sleep(0.1)
        transport.close()
        logger.info("UDP self-test: тестовый пакет отправлен")
    except Exception as e:
        logger.error("UDP self-test: ошибка отправки тестового пакета: %s", e)


async def main() -> None:
    """
    Запускает UDP AudioSocket сервер для RTP (G.711 A-law).
    """
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: AudioSocketUdpProtocol(loop),
        local_addr=(HOST, PORT),
    )
    
    try:
        logger.info(
            "Запуск AudioSocket UDP сервера (RTP/G.711 A-law) на %s:%s",
            HOST,
            PORT,
        )
        # Опциональный self-test (можно закомментировать)
        # loop.create_task(udp_self_test())
        await asyncio.Future()
    except KeyboardInterrupt:
        logger.info("Остановка сервера...")
    finally:
        for session in protocol.sessions.values():
            await session.cleanup()
        transport.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановка сервера...")
