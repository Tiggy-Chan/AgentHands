"""Shared venv bootstrap for AgentHands skills: create a venv, install deps, exec into it.

Ubuntu marks the system Python as externally managed (PEP 668), so pip refuses
to install into it. Each skill keeps a venv under its own data directory instead.

Proxies are inherited from the caller's environment and never hardcoded here;
when a download fails, the error tells the user to set HTTPS_PROXY and retry.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


class VenvError(RuntimeError):
    """Bootstrap failed; the message states the next step the user can take."""


def venv_dir(home) -> Path:
    return Path(home) / "venv"


def venv_python(home) -> Path:
    return venv_dir(home) / "bin" / "python"


def in_venv(home) -> bool:
    """Whether the current process is already running the venv's interpreter.

    Compares the unresolved sys.executable path: realpath would follow the venv's
    python symlink back to the system interpreter and always report False.
    """
    return os.path.abspath(sys.executable) == os.path.abspath(str(venv_python(home)))


def ensure(home, packages, *, note=None) -> Path:
    """Make sure <home>/venv exists with packages installed; return its python.

    note is an optional progress callback (defaults to stderr). Every failure
    raises VenvError and the caller decides how to present it.
    """
    say = note or (lambda msg: print(msg, file=sys.stderr))
    root = venv_dir(home)
    py = venv_python(home)
    if py.exists():
        return py
    say(f"创建 venv：{root}")
    try:
        import ensurepip  # noqa: F401
    except ModuleNotFoundError as exc:
        fix = ("sudo apt install python3-venv" if sys.platform.startswith("linux")
               else "brew install python3")
        raise VenvError(f"venv 缺 ensurepip，请先执行：\n  {fix}") from exc
    import venv

    try:
        # Pass the venv root, not bin/ — bin/ would put the interpreter at venv/bin/bin/.
        venv.EnvBuilder(with_pip=True).create(root)
    except Exception as exc:
        raise VenvError(f"创建 venv 失败：{exc}") from exc
    pip = [str(root / "bin" / "pip"), "install", "-q", *packages]
    if subprocess.run(pip).returncode != 0:
        raise VenvError(
            "装依赖失败。若是网络问题，挂上代理再跑一次：\n"
            f"  HTTPS_PROXY=http://127.0.0.1:<代理端口> {' '.join(pip)}"
        )
    return py


def reexec(home, script) -> None:
    """Replace the current process with the same script under the venv interpreter."""
    py = str(venv_python(home))
    os.execv(py, [py, str(script), *sys.argv[1:]])
