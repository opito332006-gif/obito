"""OBITO Stems - htdemucs separation core.

A direct Python port of the chunked overlap-add used by the Demixr app
(lib/helpers/separation/onnx_demixing_engine.dart). The ONNX graph has a fixed
input of [1, 2, 343980] ("mix") and outputs [1, 4, 2, 343980] ("stems").
"""
import subprocess
import wave

import numpy as np

SAMPLE_RATE = 44100
CHANNELS = 2
SEGMENT = 343980                    # 7.8 s, baked into the ONNX graph
OVERLAP = SEGMENT // 4
STRIDE = SEGMENT - OVERLAP
SOURCES = ["drums", "bass", "other", "vocals"]   # htdemucs native output order
INPUT_NAME = "mix"
OUTPUT_NAME = "stems"


class SeparationError(Exception):
    pass


def decode_audio(path, max_seconds=None):
    """Decode any audio file to float32 array shaped (2, frames) @ 44.1 kHz."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-vn",
           "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE)]
    if max_seconds:
        cmd += ["-t", str(max_seconds + 1)]   # read 1s extra to detect "too long"
    cmd += ["-f", "f32le", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        raise SeparationError("Could not read this audio file")
    data = np.frombuffer(proc.stdout, dtype="<f4")
    frames = len(data) // CHANNELS
    if frames == 0:
        raise SeparationError("Decoded audio is empty")
    audio = data[: frames * CHANNELS].reshape(frames, CHANNELS).T.copy()
    if max_seconds and frames > max_seconds * SAMPLE_RATE:
        raise SeparationError(f"Song is longer than {max_seconds // 60} minutes")
    return audio


def transition_window():
    w = np.ones(SEGMENT, dtype=np.float32)
    ramp = np.linspace(0.0, 1.0, OVERLAP, dtype=np.float32)
    w[:OVERLAP] = ramp
    w[SEGMENT - OVERLAP:] = ramp[::-1]
    return w


def load_onnx_infer(model_path, threads=None):
    """Returns (infer_fn, close_fn). Settings mirror the Dart engine: graph
    optimisation OFF and CPU arena OFF to keep peak RAM around ~2 GB."""
    import onnxruntime as ort
    opts = ort.SessionOptions()
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    opts.enable_cpu_mem_arena = False
    if threads:
        opts.intra_op_num_threads = threads
    session = ort.InferenceSession(model_path, opts,
                                   providers=["CPUExecutionProvider"])

    def infer(mix):                                  # mix: (1, 2, SEGMENT)
        return session.run([OUTPUT_NAME], {INPUT_NAME: mix})[0]

    def close():
        nonlocal session
        session = None

    return infer, close


def separate(audio, infer, out_dir, on_progress=None, should_cancel=None):
    """Run overlap-add separation. Streams each stem to <out_dir>/<stem>.wav
    (16-bit stereo) so memory stays bounded to roughly one segment."""
    total = audio.shape[1]
    n_chunks = max(1, (total + STRIDE - 1) // STRIDE)
    window = transition_window()

    writers, paths = {}, {}
    for stem in SOURCES:
        path = f"{out_dir}/{stem}.wav"
        wf = wave.open(path, "wb")
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        writers[stem], paths[stem] = wf, path

    acc = np.zeros((len(SOURCES), CHANNELS, SEGMENT), dtype=np.float32)
    weight = np.zeros(SEGMENT, dtype=np.float32)
    base = 0
    try:
        for i in range(n_chunks):
            if should_cancel and should_cancel():
                raise SeparationError("Cancelled")
            start = i * STRIDE
            end = min(start + SEGMENT, total)
            n = end - start

            buf = np.zeros((1, CHANNELS, SEGMENT), dtype=np.float32)
            buf[0, :, :n] = audio[:, start:end]
            out = np.asarray(infer(buf), dtype=np.float32)
            out = out.reshape(len(SOURCES), CHANNELS, SEGMENT)

            acc[:, :, :n] += out[:, :, :n] * window[:n]
            weight[:n] += window[:n]

            is_last = i == n_chunks - 1
            flush_end = total if is_last else (i + 1) * STRIDE
            flush = flush_end - base

            done = acc[:, :, :flush] / np.maximum(weight[:flush], 1e-8)
            pcm = np.clip(done, -1.0, 1.0)
            pcm = (pcm * 32767.0).astype("<i2")
            for row, stem in enumerate(SOURCES):
                writers[stem].writeframes(pcm[row].T.tobytes())

            if not is_last:
                keep = SEGMENT - flush
                acc[:, :, :keep] = acc[:, :, flush:].copy()
                acc[:, :, keep:] = 0.0
                weight[:keep] = weight[flush:].copy()
                weight[keep:] = 0.0
                base += flush

            if on_progress:
                on_progress((i + 1) / n_chunks)
    finally:
        for wf in writers.values():
            wf.close()
    return paths
