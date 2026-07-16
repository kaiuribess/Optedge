"""Diagnose PyTorch and NVIDIA CUDA support for optional FinBERT acceleration.

Usage:
    python diagnose_gpu.py

Prints whether PyTorch sees a GPU, its compiled CUDA runtime, the installed
NVIDIA driver state, and platform-neutral remediation guidance.
"""

import platform
import subprocess
import sys


def main() -> int:
    print(f"Platform: {platform.platform()}")
    print(f"Python  : {sys.version.split()[0]}\n")

    try:
        import torch
    except ImportError:
        print("[FAIL] torch is not installed. From the repository root, run:")
        print('       python -m pip install -e ".[sentiment]"')
        print("For NVIDIA acceleration, use the official PyTorch install selector")
        print("for the wheel matching this machine's OS, Python, and driver.")
        return 1

    print(f"torch version    : {torch.__version__}")
    print(f"torch.version.cuda: {getattr(torch.version, 'cuda', None)}")
    print(f"torch.cuda.is_available(): {torch.cuda.is_available()}")
    print(f"torch.cuda.device_count(): {torch.cuda.device_count()}")

    if torch.cuda.is_available():
        for i in range(torch.cuda.device_count()):
            print(
                f"  device {i}: {torch.cuda.get_device_name(i)}  "
                f"(compute {torch.cuda.get_device_capability(i)})"
            )
        print("\n[OK] GPU should work. FinBERT will pick this up automatically.")
        return 0

    print("\n[FAIL] CUDA not available.\n")

    # Try nvidia-smi to see what the driver reports
    try:
        r = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            print("nvidia-smi output (first 25 lines):")
            for line in r.stdout.splitlines()[:25]:
                print("  " + line)
            print()
    except FileNotFoundError:
        print("nvidia-smi not found - either no NVIDIA GPU is present or its driver is missing.")
        print()
    except Exception as e:
        print(f"nvidia-smi error: {e}\n")

    cv = getattr(torch.version, "cuda", None)
    if cv is None:
        print("DIAGNOSIS: your torch is the CPU-only build.")
        print("CPU inference remains supported. For GPU inference, use the official")
        print("PyTorch install selector to replace torch with a wheel compatible with")
        print("this machine's current NVIDIA driver.")
    else:
        print(f"DIAGNOSIS: torch was built against CUDA {cv} but the runtime isn't reachable.")
        print("Most common cause: NVIDIA driver too old.")
        print("FIX OPTIONS:")
        print(f"  1) Update your NVIDIA driver to support CUDA {cv} or newer.")
        print("  2) Use the official PyTorch install selector to choose a torch wheel")
        print("     compatible with the driver currently installed on this machine.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
