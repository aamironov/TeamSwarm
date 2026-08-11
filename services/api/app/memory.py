"""Small, dependency-free host-memory checks for local model execution."""

import platform
import re
import subprocess
from pathlib import Path


def available_memory_bytes() -> int | None:
    """Return host memory available for a new process, when the OS exposes it."""
    system = platform.system()
    if system == "Linux":
        return _linux_available_memory_bytes()
    if system == "Darwin":
        return _macos_available_memory_bytes()
    return None


def _linux_available_memory_bytes() -> int | None:
    try:
        values = {
            key: int(value) * 1024
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
            if (parts := line.split()) and len(parts) >= 2
            if (key := parts[0].rstrip(":")) == "MemAvailable"
            if (value := parts[1]).isdigit()
        }
    except OSError:
        return None
    return values.get("MemAvailable")


def _macos_available_memory_bytes() -> int | None:
    try:
        output = subprocess.run(
            ["vm_stat"], capture_output=True, check=True, text=True, timeout=2
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    page_size_match = re.search(r"page size of (\d+) bytes", output)
    if page_size_match is None:
        return None
    releasable_pages = 0
    for label in ("Pages free", "Pages inactive", "Pages speculative"):
        match = re.search(rf"^{re.escape(label)}:\s+(\d+)\.", output, re.MULTILINE)
        if match:
            releasable_pages += int(match.group(1))
    return releasable_pages * int(page_size_match.group(1))
