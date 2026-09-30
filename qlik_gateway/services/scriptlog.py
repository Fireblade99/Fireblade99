"""Pulls the actual error out of a Qlik reload script log."""

import re

# Qlik writes the reason after this line (the text depends on the server locale)
_MARKERS = (
    "the following error occurred:",
    "произошла следующая ошибка:",
    "возникла следующая ошибка:",
)
_ERROR_LINE = re.compile(r"(error|ошибка|failed|denied|not found|не найден)", re.IGNORECASE)
# script log lines start with a timestamp: "20260929T140512.123+0500 ..."
_TS = re.compile(r"^\s*\d{8}T\d{6}(?:\.\d+)?(?:[+-]\d{4})?\s+")
_NOISE = ("execution failed", "execution finished", "script error. first error", "---")


def _clean(line: str) -> str:
    return _TS.sub("", line).strip()


def extract_script_error(log: str, max_len: int = 2000) -> str | None:
    """Returns the error message of a failed reload, or None if nothing recognizable was found."""
    lines = [_clean(x) for x in (log or "").splitlines()]
    lines = [x for x in lines if x]
    for i in range(len(lines) - 1, -1, -1):
        low = lines[i].lower()
        for marker in _MARKERS:
            if marker in low:
                after = lines[i][low.index(marker) + len(marker) :].strip()
                tail = [after] if after else []
                for nxt in lines[i + 1 : i + 6]:
                    if any(n in nxt.lower() for n in _NOISE):
                        break
                    tail.append(nxt)
                text = "\n".join(tail).strip()
                if text:
                    return text[:max_len]
    for line in reversed(lines[-40:]):  # no marker: the last line that looks like an error
        if _ERROR_LINE.search(line) and not any(n in line.lower() for n in _NOISE):
            return line[:max_len]
    return None
