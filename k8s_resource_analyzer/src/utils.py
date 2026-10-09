from typing import Optional


def fmt_bytes(b: Optional[float]) -> str:
    """Format bytes to a human-readable string (Ki/Mi/Gi)."""
    if b is None:
        return "N/A"
    sign = ""
    if b < 0:
        sign = "-"
        b = abs(b)
    for unit in ("B", "Ki", "Mi", "Gi", "Ti"):
        if b < 1024.0:
            return f"{sign}{b:.1f}{unit}"
        b /= 1024.0
    return f"{sign}{b:.1f}Pi"


def fmt_cores(c: Optional[float]) -> str:
    """Format CPU cores to a human-readable string."""
    if c is None:
        return "N/A"
    if abs(c) < 1.0:
        return f"{c * 1000:.0f}m"
    return f"{c:.3f}"
