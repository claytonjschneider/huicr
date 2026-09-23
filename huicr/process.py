import os
import subprocess

from .config import HuicrError


def run(argv, *, cwd=None, env=None, input=None, timeout=30, check=True):
    try:
        result = subprocess.run(
            [str(a) for a in argv], cwd=cwd,
            env={**os.environ, **(env or {})}, input=input,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HuicrError(f"{argv[0]}: {e}") from e
    if check and result.returncode:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise HuicrError(message or f"{argv[0]} exited {result.returncode}")
    return result
