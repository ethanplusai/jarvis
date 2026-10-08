
import sys as _sys

# The interpreter that runs a stand-in `claude` script. On Windows a venv's
# python.exe is a launcher that starts the base interpreter as ITS child and
# keeps a copy of every pipe handle, so a stand-in that closes stdout never
# produces EOF and a kill of the launcher leaves the real child behind. The
# base interpreter is the process itself; the stand-ins use only the stdlib.
STANDIN_PYTHON = (getattr(_sys, "_base_executable", None) or _sys.executable
                  if _sys.platform == "win32" else _sys.executable)
