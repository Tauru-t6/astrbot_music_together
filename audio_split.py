#!/usr/bin/env python3
"""Split MP3 bytes into time-based chunks, in memory, no subprocess.

Parses MP3 frame headers (skipping ID3v2), walks frame boundaries to map
time to byte offsets, and yields (start_second, chunk_bytes) tuples.
Non-MP3 input (m4a/flac) can't be frame-split and yields nothing.
"""
from __future__ import annotations

BITRATES_V1L3 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
BITRATES_V2L3 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
SAMPLE_RATES = {3: 44100, 0: 44100, 1: 48000, 2: 32000}  # MPEG1 default to 44100
SAMPLE_RATES_V2 = {0: 22050, 1: 24000, 2: 16000, 3: 44100}


def _skip_id3(data: bytes) -> int:
    if len(data) > 10 and data[:3] == b"ID3":
        size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
        return 10 + size
    return 0


def _frame_info(data: bytes, pos: int) -> tuple[int, int] | None:
    """Return (frame_length, frame_duration_sec) at pos, or None."""
    if pos + 4 > len(data):
        return None
    b0, b1, b2 = data[pos], data[pos + 1], data[pos + 2]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return None
    version_bits = (b1 >> 3) & 0x03   # 3=MPEG1, 2=MPEG2, 0=MPEG2.5
    layer_bits = (b1 >> 1) & 0x03     # 1=Layer3
    if version_bits == 1 or layer_bits == 0:
        return None
    bitrate_idx = (b2 >> 4) & 0x0F
    sr_idx = (b2 >> 2) & 0x03
    padding = (b2 >> 1) & 0x01
    if bitrate_idx in (0, 15) or sr_idx == 3:
        return None
    if version_bits == 3:
        bitrate = BITRATES_V1L3[bitrate_idx] * 1000
        sample_rate = {0: 44100, 1: 48000, 2: 32000}.get(sr_idx, 44100)
        samples = 1152
    else:
        bitrate = BITRATES_V2L3[bitrate_idx] * 1000
        sample_rate = SAMPLE_RATES_V2.get(sr_idx, 22050)
        samples = 576
    if bitrate == 0 or sample_rate == 0:
        return None
    frame_len = (samples // 8) * bitrate // sample_rate + padding
    if frame_len <= 4:
        return None
    return frame_len, samples / sample_rate


def split_mp3_seconds(data: bytes, segment_seconds: float,
                      total_seconds: float) -> list[tuple[float, bytes]]:
    """Cut MP3 bytes into chunks of ~segment_seconds. Non-mp3 yields []."""
    chunks: list[tuple[float, bytes]] = []
    pos = _skip_id3(data)
    seg_start_byte = pos
    seg_start_time = 0.0
    t = 0.0
    scanned = pos
    limit = len(data)
    next_boundary = segment_seconds
    while scanned + 4 <= limit:
        info = _frame_info(data, scanned)
        if info is None:
            scanned += 1  # resync byte-by-byte
            continue
        frame_len, frame_dur = info
        if scanned + frame_len > limit:
            break
        scanned += frame_len
        t += frame_dur
        if t >= next_boundary and (total_seconds <= 0 or seg_start_time < total_seconds - 2):
            chunks.append((round(seg_start_time, 1), data[seg_start_byte:scanned]))
            seg_start_byte = scanned
            seg_start_time = t
            next_boundary += segment_seconds
    if t > 0 and seg_start_byte < limit and (total_seconds <= 0 or seg_start_time < total_seconds - 2):
        chunks.append((round(seg_start_time, 1), data[seg_start_byte:limit]))
    return chunks
