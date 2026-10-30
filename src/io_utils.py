"""Atomic artifacts with bounded retries for transient Windows sharing locks."""
import os
import tempfile
import time
from pathlib import Path


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    descriptor,temporary=tempfile.mkstemp(prefix=path.name+".",suffix=".tmp",dir=path.parent)
    try:
        with os.fdopen(descriptor,"wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(12):
            try:
                os.replace(temporary,path)
                return
            except PermissionError:
                if attempt==11:raise
                time.sleep(min(0.05*(attempt+1),0.5))
    finally:
        if os.path.exists(temporary):
            try:os.unlink(temporary)
            except PermissionError:pass
