"""Bark 3.0 Pro speech engine.

The model reads a whole utterance at a time. On this GPU that pass is much
faster than speech, so the engine re-decodes the utterance several times a
second and sends the latest text. The microphone is opened only after a
start command, and it is closed before the final decode of a toggle-off.

This process never calls pactl set-*. It only records the device named in
the start command, through a parec stream.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import warnings
from collections import deque

warnings.filterwarnings(
    "ignore",
    message="Using the model-agnostic default",
)

# Keep the CPU feature extractor from grabbing every core. Set before torch.
os.environ.setdefault("OMP_NUM_THREADS", "4")
os.environ.setdefault("MKL_NUM_THREADS", "4")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

SAMPLE_RATE = 16000
LEADING_SILENCE = 4000
PARTIAL_INTERVAL = 0.32
MIN_SPEECH_SECONDS = 0.28
ENDPOINT_SILENCE_SECONDS = 0.50
MAX_UTTERANCE_SECONDS = 20.0
PREROLL_SECONDS = 0.20
READ_SECONDS = 0.05


def emit(payload: dict, lock: threading.Lock) -> None:
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    with lock:
        sys.stdout.write(line)
        sys.stdout.flush()


class Engine:
    def __init__(self) -> None:
        self.out_lock = threading.Lock()
        self.capture_lock = threading.Lock()
        self.model = None
        self.processor = None
        self.torch = None
        self.np = None
        self.capture_on = False
        self.stop_flag = False
        self.quit = False
        self.quiet = False
        self.proc: subprocess.Popen | None = None
        self.loop_thread: threading.Thread | None = None
        self.audio_q: queue.Queue = queue.Queue()
        self.last_audio = 0.0
        self.stderr_chunks: list[bytes] = []

    def send(self, payload: dict) -> None:
        if self.quiet:
            if payload.get("event") == "error":
                print(payload.get("message") or "Speech engine error", file=sys.stderr)
            return
        try:
            emit(payload, self.out_lock)
        except BrokenPipeError:
            self.quit = True

    def load(self, model_dir: str, kind: str = "safetensors", processor_dir: str = "", title: str = "Bark") -> None:
        self.send({"event": "status", "state": "loading"})
        self.model_title = title or "Bark"
        try:
            import numpy as np
            import torch
            from transformers import AutoProcessor, ParakeetForTDT
        except Exception as exc:
            self.send({"event": "error", "message": f"The speech engine is missing a library. {exc}"})
            return
        self.np = np
        self.torch = torch
        self._processor_cls = AutoProcessor
        self._parakeet_cls = ParakeetForTDT
        if not torch.cuda.is_available():
            self.send(
                {
                    "event": "error",
                    "message": "CUDA is not available. Bark stays on the GPU and does not silently switch to the CPU.",
                }
            )
            return
        if not torch.cuda.is_bf16_supported():
            self.send(
                {
                    "event": "error",
                    "message": "This GPU cannot run BF16, which these Bark models need.",
                }
            )
            return
        try:
            if kind == "bark":
                model = _load_bark_file(model_dir, processor_dir or model_dir, torch)
            else:
                model = ParakeetForTDT.from_pretrained(
                    model_dir,
                    dtype=torch.bfloat16,
                    local_files_only=True,
                ).to("cuda").eval()
            source = processor_dir or model_dir
            processor = AutoProcessor.from_pretrained(source, local_files_only=True)
            torch.set_grad_enabled(False)
            self.processor = processor
            self.model = model
            try:
                self.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32))
                torch.cuda.synchronize()
            except Exception as exc:
                # Pure silence is only a warmup. A CUDA failure means the GPU path is unusable.
                detail = str(exc)
                if "CUDA" in detail or "out of memory" in detail.lower():
                    raise
                print(f"warmup skipped: {detail}", file=sys.stderr, flush=True)
        except Exception as exc:
            self.model = None
            self.processor = None
            self.send({"event": "error", "message": f"{self.model_title} failed to load. {exc}"})
            return
        self.send({"event": "ready", "gpu": torch.cuda.get_device_name(0)})

    def transcribe(self, wave) -> str:
        np = self.np
        torch = self.torch
        assert self.model is not None and self.processor is not None
        audio = np.ascontiguousarray(wave, dtype=np.float32)
        if audio.size == 0:
            return ""
        padded = np.concatenate([np.zeros(LEADING_SILENCE, dtype=np.float32), audio])
        inputs = self.processor(padded, sampling_rate=SAMPLE_RATE, return_tensors="pt")
        inputs = _move_inputs(inputs, "cuda", torch.bfloat16)
        with torch.inference_mode():
            output = self.model.generate(**inputs)
        ids = output.sequences if hasattr(output, "sequences") else output
        text = self.processor.batch_decode(ids, skip_special_tokens=True)[0]
        return " ".join(text.replace("\n", " ").split())

    def start_mic(self, device: str) -> None:
        if self.model is None:
            self.send({"event": "error", "message": "The speech model is not loaded yet."})
            return
        if not device or "DEFAULT" in device:
            self.send(
                {
                    "event": "error",
                    "message": "No microphone is selected. Talk Anywhere will not open the system default device.",
                }
            )
            return
        if device.endswith(".monitor"):
            self.send(
                {
                    "event": "error",
                    "message": "That device is a speaker monitor, not a microphone.",
                }
            )
            return
        if not _source_exists(device):
            self.send(
                {
                    "event": "error",
                    "message": f"The selected microphone is not available ({device}).",
                }
            )
            return
        if self.capture_on:
            self._halt_capture()
        self.stop_flag = False
        self.audio_q = queue.Queue()
        try:
            self.proc = subprocess.Popen(
                [
                    "parec",
                    "--device",
                    device,
                    "--rate=16000",
                    "--channels=1",
                    "--format=float32le",
                    "--latency-msec=40",
                    "--client-name=TalkAnywhere",
                    "--stream-name=Talk Anywhere dictation",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            self.send({"event": "error", "message": "parec is not installed, so the microphone cannot be opened."})
            return
        time.sleep(0.2)
        if self.proc.poll() is not None:
            err = _read_stderr(self.proc)
            self.send(
                {
                    "event": "error",
                    "message": "Could not open the microphone. " + (err or "parec exited immediately."),
                }
            )
            self.proc = None
            return
        self.capture_on = True
        self.last_audio = time.monotonic()
        print(f"opening microphone stream {device}", file=sys.stderr, flush=True)
        threading.Thread(target=self._read_audio, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._drain_stderr, args=(self.proc,), daemon=True).start()
        self.loop_thread = threading.Thread(target=self._vad_loop, daemon=True)
        self.loop_thread.start()
        self.send({"event": "listening", "device": device})

    def stop_mic(self) -> None:
        self._halt_capture()
        self.send({"event": "idle"})

    def _halt_capture(self) -> None:
        if not self.capture_on and self.proc is None:
            return
        # Close the device before the last transcription so the mic is free.
        self.stop_flag = True
        self._kill_parec()
        thread = self.loop_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=30)
        self.capture_on = False
        self.loop_thread = None

    def handle(self, command: dict) -> None:
        name = command.get("cmd")
        if name == "load":
            self.load(
                str(command.get("model_dir") or ""),
                kind=str(command.get("kind") or "safetensors"),
                processor_dir=str(command.get("processor_dir") or ""),
                title=str(command.get("title") or "Bark"),
            )
        elif name == "start":
            self.start_mic(str(command.get("device") or ""))
        elif name == "stop":
            self.stop_mic()
        elif name == "shutdown":
            self.stop_mic()
            self.quit = True
        else:
            self.send({"event": "error", "message": f"Unknown engine command: {name}"})

    def _kill_parec(self) -> None:
        with self.capture_lock:
            proc = self.proc
            self.proc = None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()

    def _read_audio(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        np = self.np
        frame_bytes = int(SAMPLE_RATE * READ_SECONDS) * 4
        try:
            while True:
                data = proc.stdout.read(frame_bytes)
                if not data:
                    break
                usable = len(data) - (len(data) % 4)
                if usable < 4 or np is None:
                    continue
                samples = np.frombuffer(data[:usable], dtype=np.float32).copy()
                self.last_audio = time.monotonic()
                self.audio_q.put(samples)
        finally:
            code = proc.poll()
            if code not in (None, 0, -15, -9) and not self.stop_flag:
                err = b"".join(self.stderr_chunks).decode(errors="replace").strip()
                self.send(
                    {
                        "event": "error",
                        "message": "Microphone capture stopped. " + (err or f"parec exited with status {code}."),
                    }
                )

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        if proc.stderr is None:
            return
        for line in proc.stderr:
            self.stderr_chunks.append(line)
            del self.stderr_chunks[:-40]

    def _vad_loop(self) -> None:
        np = self.np
        assert np is not None
        gate = _NoiseGate()
        preroll: deque = deque()
        preroll_n = 0
        preroll_limit = int(PREROLL_SECONDS * SAMPLE_RATE)
        utter: list = []
        utter_n = 0
        speech_n = 0
        silence_n = 0
        in_utterance = False
        want_final = False
        last_partial = 0.0
        last_text = ""
        min_speech = int(MIN_SPEECH_SECONDS * SAMPLE_RATE)
        endpoint = int(ENDPOINT_SILENCE_SECONDS * SAMPLE_RATE)
        max_samples = int(MAX_UTTERANCE_SECONDS * SAMPLE_RATE)

        while True:
            if self.stop_flag and not in_utterance:
                break
            chunk = None
            try:
                chunk = self.audio_q.get(timeout=0.05)
            except queue.Empty:
                chunk = None
            now = time.monotonic()
            if chunk is not None:
                rms = float(np.sqrt(np.mean(np.square(chunk)) + 1e-12))
                speech = gate.speech(rms, active=in_utterance)
                if not in_utterance:
                    preroll.append(chunk)
                    preroll_n += len(chunk)
                    while preroll_n > preroll_limit and preroll:
                        preroll_n -= len(preroll.popleft())
                    if speech:
                        in_utterance = True
                        utter = list(preroll)
                        utter_n = sum(len(piece) for piece in utter)
                        speech_n = len(chunk)
                        silence_n = 0
                        want_final = False
                        last_text = ""
                        last_partial = now
                        preroll.clear()
                        preroll_n = 0
                else:
                    utter.append(chunk)
                    utter_n += len(chunk)
                    if speech:
                        silence_n = 0
                        speech_n += len(chunk)
                    else:
                        silence_n += len(chunk)
                    if speech_n >= min_speech and silence_n >= endpoint:
                        want_final = True
                    if utter_n >= max_samples:
                        want_final = True
            elif self.capture_on and not self.stop_flag and (now - self.last_audio) > 3.0:
                self.send(
                    {
                        "event": "error",
                        "message": "The microphone stopped sending audio. Dictation was turned off.",
                    }
                )
                self.stop_flag = True
                self._kill_parec()

            if self.stop_flag and in_utterance:
                want_final = True
            if not in_utterance:
                continue
            due = (now - last_partial) >= PARTIAL_INTERVAL and speech_n >= min_speech
            if not due and not want_final:
                continue
            if speech_n < min_speech:
                if want_final:
                    in_utterance = False
                    utter = []
                    utter_n = speech_n = silence_n = 0
                    want_final = False
                continue
            audio = np.concatenate(utter)
            try:
                text = self.transcribe(audio)
            except Exception as exc:
                message = str(exc)
                fatal = "CUDA" in message or "cuda" in message or "out of memory" in message.lower()
                self.send(
                    {
                        "event": "error",
                        "message": "Transcription failed. " + message,
                    }
                )
                if fatal:
                    self.stop_flag = True
                    self._kill_parec()
                    in_utterance = False
                    break
                last_partial = time.monotonic()
                continue
            last_partial = time.monotonic()
            if want_final or self.stop_flag:
                if text:
                    self.send({"event": "final", "text": text})
                in_utterance = False
                utter = []
                utter_n = speech_n = silence_n = 0
                want_final = False
                last_text = ""
                if self.stop_flag:
                    break
            elif text and text != last_text:
                last_text = text
                self.send({"event": "partial", "text": text})


class _NoiseGate:
    def __init__(self) -> None:
        self.floor = 0.004

    def speech(self, rms: float, *, active: bool) -> bool:
        high = max(0.012, self.floor * 4.0)
        low = max(0.008, self.floor * 2.2)
        is_speech = rms >= (low if active else high)
        if not is_speech:
            self.floor = 0.96 * self.floor + 0.04 * rms
        return is_speech


def _load_bark_file(model_dir: str, processor_dir: str, torch):
    """Unpack a .bark file with the loader shipped beside it."""
    import importlib.util
    from pathlib import Path

    from transformers import GenerationConfig

    folder = Path(model_dir)
    loader_path = folder / "bark_loader.py"
    if not loader_path.is_file():
        raise FileNotFoundError(f"bark_loader.py is missing in {folder}")
    original = GenerationConfig.from_pretrained

    def local_config(name, *args, **kwargs):
        if "parakeet" in str(name):
            return original(processor_dir, local_files_only=True)
        return original(name, *args, **kwargs)

    # The published loader asks Hugging Face for the Parakeet generation config.
    # Point that lookup at files we already have when they sit next to the weights.
    GenerationConfig.from_pretrained = local_config
    try:
        spec = importlib.util.spec_from_file_location("talk_anywhere_bark_loader", loader_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not read {loader_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.load_model(str(folder / "model.bark"), device="cuda", dtype=torch.bfloat16)
    finally:
        GenerationConfig.from_pretrained = original


def _move_inputs(inputs, device: str, dtype):
    try:
        return inputs.to(device, dtype)
    except Exception:
        moved = inputs.to(device)
        for key, value in list(moved.items()):
            if value.is_floating_point():
                moved[key] = value.to(dtype=dtype)
        return moved


def _source_exists(device: str) -> bool:
    """Read-only check. Does not change the default source."""
    try:
        result = subprocess.run(
            ["pactl", "list", "short", "sources"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1] == device:
            return True
    return False


def _read_stderr(proc: subprocess.Popen) -> str:
    if proc.stderr is None:
        return ""
    try:
        data = proc.stderr.read()
    except Exception:
        return ""
    return data.decode(errors="replace").strip()


def run_wav(path: str) -> int:
    from pathlib import Path

    import numpy as np
    import soundfile as sf
    import soxr

    from talk_anywhere.models import directory, get_model, model_ready, processor_source

    audio_path = Path(path)
    if not audio_path.is_file():
        print(f"Audio file not found: {audio_path}", file=sys.stderr)
        return 1
    spec = get_model("pro")
    if not model_ready(spec):
        print(f"{spec.title} is not downloaded in {directory(spec)}", file=sys.stderr)
        return 1
    engine = Engine()
    engine.quiet = True
    engine.load(
        str(directory(spec)),
        kind=spec.kind,
        processor_dir=str(processor_source(spec)),
        title=spec.title,
    )
    if engine.model is None:
        return 1
    wave, sample_rate = sf.read(audio_path, dtype="float32", always_2d=True)
    wave = wave.mean(axis=1)
    if sample_rate != SAMPLE_RATE:
        wave = soxr.resample(wave, sample_rate, SAMPLE_RATE)
    # load() already prepends the model's leading silence inside transcribe().
    text = engine.transcribe(np.asarray(wave, dtype=np.float32))
    print(text)
    return 0


def run_server() -> int:
    engine = Engine()
    try:
        for line in sys.stdin:
            if engine.quit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                command = json.loads(line)
            except json.JSONDecodeError:
                engine.send({"event": "error", "message": "The app sent a command the engine could not read."})
                continue
            try:
                engine.handle(command)
            except Exception as exc:
                engine.send({"event": "error", "message": f"Speech engine error. {exc}"})
            if engine.quit:
                break
    finally:
        engine.stop_mic()
    return 0


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--wav":
        return run_wav(sys.argv[2])
    return run_server()


if __name__ == "__main__":
    raise SystemExit(main())
