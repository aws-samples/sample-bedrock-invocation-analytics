import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for sub in ("deploy/lambda", "deploy/custom_resources", "webui/api", "."):
    sys.path.insert(0, str(ROOT / sub))
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
