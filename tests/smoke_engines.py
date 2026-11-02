"""Smoke test for every FL ClearVoice engine, outside ComfyUI.

Usage (CPU only, from the fork root, with .testenv.sh sourced):
    CUDA_VISIBLE_DEVICES= "$P" tests/smoke_engines.py [ModelName ...]

Checks that each model downloads, loads and processes a synthetic noisy signal
into a finite waveform. It says nothing about audio quality: judge that with a
real clip. Aborts itself (never other processes) if free RAM drops below MIN_FREE_GB.
"""
import ctypes
import os
import sys
import threading
import time
import traceback

import numpy as np
import torch

MIN_FREE_GB = 3.0
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class _Mem(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong), ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]


def free_gb():
    m = _Mem()
    m.dwLength = ctypes.sizeof(m)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.ullAvailPhys / 1e9


def _watchdog():
    while True:
        if free_gb() < MIN_FREE_GB:
            print(f"!! free RAM {free_gb():.1f} GB < {MIN_FREE_GB} GB, aborting the test", flush=True)
            os._exit(3)
        time.sleep(1)


def synthetic_noisy_speech(sr=16000, seconds=4.0, snr_db=10.0):
    """Harmonic 'voice' with syllable-like envelope plus white noise."""
    t = np.arange(int(sr * seconds)) / sr
    f0 = 120 + 20 * np.sin(2 * np.pi * 0.7 * t)
    phase = 2 * np.pi * np.cumsum(f0) / sr
    voice = sum(np.sin(k * phase) / k for k in range(1, 12))
    env = np.clip(np.sin(2 * np.pi * 3 * t), 0, None) ** 0.5
    voice = voice * env
    voice /= np.max(np.abs(voice)) + 1e-9
    noise = np.random.default_rng(0).standard_normal(len(t))
    noise *= np.sqrt(np.mean(voice ** 2) / (10 ** (snr_db / 10)) / np.mean(noise ** 2))
    return torch.from_numpy((voice + noise).astype(np.float32)).reshape(1, 1, -1), sr


def main(names):
    try:
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)  # below normal
    except Exception:
        pass
    threading.Thread(target=_watchdog, daemon=True).start()
    torch.set_num_threads(4)

    from fl_utils import model_manager as mm
    from fl_nodes.process import FL_ClearVoice_Process

    names = names or list(mm.ALL_MODELS)
    wave, sr = synthetic_noisy_speech()
    proc = FL_ClearVoice_Process()
    results = {}
    for name in names:
        print(f"\n=== {name} (free RAM {free_gb():.1f} GB, cuda={torch.cuda.is_available()})", flush=True)
        t0 = time.time()
        try:
            info = mm.get_model(name)
            out, status = proc.process_audio(info, {"waveform": wave, "sample_rate": sr})
            w = out["waveform"]
            ok = bool(torch.isfinite(w).all()) and w.numel() > 0
            results[name] = f"{'OK' if ok else 'BAD OUTPUT'} out_sr={out['sample_rate']} shape={tuple(w.shape)} {time.time() - t0:.0f}s"
        except Exception as exc:  # report and continue with the next engine
            traceback.print_exc()
            results[name] = f"FAIL {type(exc).__name__}: {str(exc)[:160]}"
        finally:
            mm.clear_cache()
        print(results[name], flush=True)

    print("\n=== SUMMARY")
    for name, res in results.items():
        print(f"{name:24s} {res}")


if __name__ == "__main__":
    main(sys.argv[1:])
