"""副作用工具的最小恢复标记存储与文件状态采集。"""

import hashlib
from pathlib import Path

from .storage import write_json_atomic

RECOVERY_SCHEMA_VERSION = 1
RECOVERY_CLEAN = "clean"
RECOVERY_INSPECTION_REQUIRED = "inspection_required"
SIDE_EFFECT_TOOLS = frozenset({"write_file", "patch_file", "run_shell"})
READ_ONLY_INSPECTION_TOOLS = frozenset({"read_file", "search", "list_files"})


def normalize_target_path(raw_path, workspace_root):
    """规范化工作区内路径；越界路径不能进入 recovery 文件。"""
    root = Path(workspace_root).resolve()
    candidate = Path(str(raw_path))
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        return resolved.relative_to(root).as_posix(), resolved
    except ValueError as exc:
        raise ValueError(f"path escapes workspace: {raw_path}") from exc


def capture_file_state(raw_path, workspace_root):
    """采集完整 exists/hash 状态，避免把不存在与空文件混为一谈。"""
    relative, resolved = normalize_target_path(raw_path, workspace_root)
    if resolved.exists() and not resolved.is_file():
        raise ValueError(f"recovery target is not a file: {relative}")
    if not resolved.exists():
        return relative, {"exists": False, "sha256": None}
    try:
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(f"unable to read recovery target before execution: {relative}: {exc}") from exc
    return relative, {"exists": True, "sha256": digest}


class RecoveryCheckpointStore:
    """每个 session 最多管理一个 active recovery checkpoint。"""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, session_id):
        return self.root / f"{session_id}.recovery.json"

    def load(self, session_id):
        path = self.path(session_id)
        if not path.is_file():
            return None
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("recovery checkpoint must be a JSON object")
        return payload

    def prepare(self, session_id, payload):
        return write_json_atomic(self.path(session_id), payload)

    def clear(self, session_id):
        path = self.path(session_id)
        path.unlink(missing_ok=True)
        return path
