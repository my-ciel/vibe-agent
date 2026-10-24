"""OS 폴더 선택창. Streamlit 스레드와 충돌하지 않도록(특히 macOS) 별도 프로세스로 띄운다."""
from __future__ import annotations

import subprocess
import sys

_SCRIPT = """
import tkinter as tk
from tkinter import filedialog
root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
print(filedialog.askdirectory(title="작업 폴더 선택") or "", end="")
"""


def pick_folder() -> str | None:
    """선택한 폴더 경로. 취소하거나 tkinter를 쓸 수 없으면 None."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", _SCRIPT], capture_output=True, text=True, timeout=300
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    path = result.stdout.strip()
    return path if result.returncode == 0 and path else None
