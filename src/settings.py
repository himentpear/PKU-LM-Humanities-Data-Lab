"""External-only user settings; never serialize secrets into run manifests."""
import os
from pathlib import Path

DEFAULT_SETTINGS=Path.home()/".config/archaeology-digitization/.env"


def load_settings(path: Path | None = None) -> dict:
    path=path or DEFAULT_SETTINGS
    values={}
    if path.exists():
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line=raw.strip()
            if not line or line.startswith("#"):
                continue
            key,separator,value=line.partition("=")
            if not separator or not key.strip().replace("_","").isalnum():
                raise ValueError("Invalid external settings format")
            values[key.strip()]=value.strip().strip('"').strip("'")
    # Explicit process environment overrides file settings.
    for key in ["DEEPSEEK_API_KEY","DEEPSEEK_BASE_URL","DEEPSEEK_MODEL","ALLOW_CLOUD_FALLBACK",
                "MAX_CLOUD_CALLS_PER_DOCUMENT","MAX_CLOUD_TOKENS_PER_DOCUMENT","LOCAL_MODEL_PATH",
                "LOCAL_MODEL_THREADS","MAX_LOCAL_BLOCKS_PER_DOCUMENT"]:
        if key in os.environ:values[key]=os.environ[key]
    return values
