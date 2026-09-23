"""Fallback GPU compute-capability probe using the installed NVIDIA driver."""

import ctypes


driver = ctypes.CDLL("libcuda.so.1")
if driver.cuInit(0) != 0:
    raise SystemExit("Could not initialize the NVIDIA CUDA driver")
count = ctypes.c_int()
if driver.cuDeviceGetCount(ctypes.byref(count)) != 0 or count.value < 1:
    raise SystemExit("No CUDA devices found")
for ordinal in range(count.value):
    major = ctypes.c_int()
    minor = ctypes.c_int()
    if driver.cuDeviceComputeCapability(ctypes.byref(major), ctypes.byref(minor), ordinal) != 0:
        raise SystemExit(f"Could not read compute capability for GPU {ordinal}")
    print(f"{major.value}.{minor.value}")
