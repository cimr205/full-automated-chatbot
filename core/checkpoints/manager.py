import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

CHECKPOINT_DIR = Path("/tmp/checkpoints")


class CheckpointManager:
    def __init__(self):
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

    def save(self, task_id: str, state: dict):
        path = CHECKPOINT_DIR / f"{task_id}.json"
        path.write_text(json.dumps(state))
        log.debug("Checkpoint saved: %s", task_id)

    def load(self, task_id: str) -> Optional[dict]:
        path = CHECKPOINT_DIR / f"{task_id}.json"
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception as e:
                log.warning("Checkpoint load error for %s: %s", task_id, e)
        return None

    def delete(self, task_id: str):
        path = CHECKPOINT_DIR / f"{task_id}.json"
        if path.exists():
            path.unlink()
