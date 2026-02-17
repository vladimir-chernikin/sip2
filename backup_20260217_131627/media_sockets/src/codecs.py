from __future__ import annotations

import audioop


def alaw_to_pcm16(data: bytes) -> bytes:
    """
    Декодирует G.711 A-law в линейный PCM16 (little-endian).

    :param data: Моно PCM поток в формате A-law (1 байт на сэмпл).
    :return: Линейный PCM16 LE (2 байта на сэмпл, 8 kHz).
    """
    if not data:
        return b""
    return audioop.alaw2lin(data, 2)


def pcm16_to_alaw(data: bytes) -> bytes:
    """
    Кодирует линейный PCM16 (little-endian) в G.711 A-law.

    :param data: PCM16 поток (2 байта на сэмпл).
    :return: A-law байты (1 байт на сэмпл).
    """
    if not data:
        return b""
    if len(data) % 2 != 0:
        raise ValueError("PCM16 данные должны иметь чётную длину")
    return audioop.lin2alaw(data, 2)

