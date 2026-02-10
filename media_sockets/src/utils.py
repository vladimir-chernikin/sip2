import numpy as np
from scipy import signal


class AudioConverter:
    @staticmethod
    def resample_audio(
        pcm_in: bytes,
        sr_in: int,
        sr_out: int,
    ) -> bytes:
        if not pcm_in:
            return b""

        if sr_in == sr_out:
            return pcm_in

        audio_array = np.frombuffer(pcm_in, dtype=np.int16)
        audio_float = audio_array.astype(np.float32) / 32768.0

        if audio_float.size == 0:
            return b""

        num_samples_out = int(len(audio_float) * sr_out / sr_in)
        resampled_float = signal.resample_poly(
            audio_float,
            sr_out,
            sr_in,
        )

        resampled_int16 = (resampled_float * 32768.0).astype(np.int16)
        return resampled_int16.tobytes()

