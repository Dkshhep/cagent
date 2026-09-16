"""项目/全局卡片存储；同一 JSON 文件的更新在进程间串行化。"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from .memory_cards import SCHEMA_VERSION, SECRET_PATTERN, MemoryValidationError, apply_proposal, effective_cards
from .storage import write_json_atomic


def _empty_document():
    return {"schema_version": SCHEMA_VERSION, "revision": 0, "cards": []}


@contextmanager
def _file_lock(path, timeout=3.0):
    """OS 文件锁在进程崩溃时自动释放，避免遗留锁阻塞未来写入。"""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("memory_store_lock_timeout") from None
                time.sleep(0.025)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class MemoryStore:
    def __init__(self, project_root, global_root=None):
        self.project_root = Path(project_root).resolve()
        self.project_path = self.project_root / ".cagent" / "memory" / "cards.json"
        if global_root is not None:
            self.global_path = Path(global_root) / "cards.json"
        else:
            try:
                self.global_path = Path.home() / ".cagent" / "memory" / "cards.json"
            except RuntimeError:
                # 测试/服务进程可清空 HOME/USERPROFILE；项目任务仍应可运行。
                self.global_path = None
        self.legacy_root = self.project_root / ".cagent" / "memory"

    def path_for(self, scope):
        if scope == "project":
            return self.project_path
        if scope == "global":
            if self.global_path is None:
                raise MemoryValidationError("global_home_unavailable")
            return self.global_path
        raise MemoryValidationError("invalid_scope")

    def _read_path(self, path):
        if not path.exists():
            return _empty_document()
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            raise MemoryValidationError("memory_store_schema_mismatch")
        if not isinstance(data.get("revision"), int) or not isinstance(data.get("cards"), list):
            raise MemoryValidationError("memory_store_corrupt")
        return data

    def read(self, scope):
        if scope == "global" and self.global_path is None:
            return _empty_document()
        return self._read_path(self.path_for(scope))

    def active(self, scope):
        return [card for card in self.read(scope)["cards"] if card.get("status") == "active"]

    def effective(self):
        return effective_cards(self.active("project"), self.active("global"))

    def get(self, card_id):
        for scope in ("project", "global"):
            for card in self.read(scope)["cards"]:
                if card.get("id") == card_id:
                    return card
        return None

    def commit(self, proposal, expected_revision=None):
        path = self.path_for(proposal["scope"])
        with _file_lock(path):
            document = self._read_path(path)
            if expected_revision is not None and document["revision"] != expected_revision:
                raise MemoryValidationError("store_revision_changed")
            action, card = apply_proposal(document, proposal)
            if action != "no_change":
                document["revision"] += 1
                write_json_atomic(path, document)
            return action, card

    def forget(self, card_id, hard=False):
        for scope in ("project", "global"):
            if scope == "global" and self.global_path is None:
                continue
            path = self.path_for(scope)
            with _file_lock(path):
                document = self._read_path(path)
                card = next((card for card in document["cards"] if card.get("id") == card_id), None)
                if not card:
                    continue
                if hard:
                    document["cards"].remove(card)
                else:
                    if card.get("status") == "forgotten":
                        return card
                    card["status"] = "forgotten"
                    from .memory_cards import utc_now
                    card["updated_at"] = utc_now()
                document["revision"] += 1
                write_json_atomic(path, document)
                return card
        raise MemoryValidationError("card_not_found")

    def legacy_present(self, session=None):
        return bool(
            (session or {}).get("memory")
            or (self.legacy_root / "MEMORY.md").exists()
            or any((self.legacy_root / "topics").glob("*.md"))
        )

    def migration_preview(self):
        """只从旧 topic 的 Notes 行读取候选；不碰 session file_summaries。"""
        candidates = []
        active = {(card["scope"], card["key"]): card for card in self.effective()}
        for path in sorted((self.legacy_root / "topics").glob("*.md")):
            in_notes = False
            for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                line = raw.strip()
                if line == "## Notes":
                    in_notes = True
                elif in_notes and line.startswith("- "):
                    text = line[2:].strip()
                    if not text:
                        continue
                    key = f"legacy.{path.stem}.{line_number}"
                    candidates.append({
                        "id": f"{path.stem}:{line_number}", "text": text, "suggested_key": key,
                        "suggested_scope": "project", "conflict": active.get(("project", key), {}).get("current_value"),
                    })
        return candidates

    def import_legacy(self, candidate_ids):
        """显式选中的旧条目作为人工确认的 explicit_note 导入，不推断用户授权。"""
        preview = {candidate["id"]: candidate for candidate in self.migration_preview()}
        selected = []
        seen = set()
        for candidate_id in candidate_ids:
            if candidate_id in seen:
                continue
            seen.add(candidate_id)
            candidate = preview.get(candidate_id)
            if candidate is None:
                raise MemoryValidationError("unknown_migration_candidate")
            if len(candidate["text"]) > 160 or SECRET_PATTERN.search(candidate["text"]):
                raise MemoryValidationError("legacy_candidate_unsafe_or_too_long")
            if candidate["conflict"] is not None:
                raise MemoryValidationError("legacy_candidate_conflicts_with_active_card")
            selected.append(candidate)
        imported = []
        for candidate in selected:
            candidate_id = candidate["id"]
            proposal = {
                "op": "add", "target_card_id": "", "key": candidate["suggested_key"],
                "kind": "explicit_note", "scope": "project", "tags": ["legacy"],
                "current_value": candidate["text"], "display_text": candidate["text"],
                "change_note": "用户手动从旧记忆导入；原始来源见旧 topic 文件。",
                "source": {"authorization_turn_id": "migration", "content_turn_ids": [candidate_id], "user_quote": "explicit CLI import"},
            }
            action, card = self.commit(proposal)
            imported.append({"action": action, "id": card["id"]})
        return imported
