"""Diagnose PyTorch and NVIDIA CUDA support for optional FinBERT acceleration.

Usage:
    python diagnose_gpu.py

Prints whether PyTorch sees a GPU, its compiled CUDA runtime, the installed
NVIDIA driver state, and targeted installation or driver remediation.
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
        print("[FAIL] torch is not installed. Install with:")
        print("       pip install torch --index-url https://download.pytorch.org/whl/cu121")
        return 1

    print(f"torch version    : {torch.__version__}")
    print(f"torch.version.cuda: {getattr(torch.version, 'cuda', None)}")
    print(f"torch.cuda.is_available(): {torch.cuda.is_available()}")
    print(f"torch.cuda.device_count(): {torch.cuda.device_count()}")

    if torch.cuda.is_available():
        for i in range(torch.cuda.device_count()):
            print(f"  device {i}: {torch.cuda.get_device_name(i)}  "
                  f"(compute {torch.cuda.get_device_capability(i)})")
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
        print("nvidia-smi not found — either no NVIDIA GPU, or the driver isn't installed.")
        print()
    except Exception as e:
        print(f"nvidia-smi error: {e}\n")

    cv = getattr(torch.version, "cuda", None)
    if cv is None:
        print("DIAGNOSIS: your torch is the CPU-only build.")
        print("FIX:")
        print("  pip uninstall -y torch")
        print("  pip install torch --index-url https://download.pytorch.org/whl/cu121")
        print()
        print("(Use cu118 if your NVIDIA driver only supports CUDA 11.8;")
        print(" cu124 if you're on a brand-new driver with CUDA 12.4 support.)")
    else:
        print(f"DIAGNOSIS: torch was built against CUDA {cv} but the runtime "
              "isn't reachable.")
        print("Most common cause: NVIDIA driver too old.")
        print("FIX OPTIONS:")
        print(f"  1) Update your NVIDIA driver to support CUDA {cv} or newer.")
        print("  2) Install a torch wheel that matches your driver:")
        print("       pip uninstall -y torch")
        print("       pip install torch --index-url https://download.pytorch.org/whl/cu118")
        print("       (or cu121 / cu124 — pick the one ≤ your driver's CUDA version)")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
