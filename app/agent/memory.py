"""Context-aware memory system supporting separate memory tiers: Task, Preference, Knowledge, and Run History."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from ..config import get_settings

logger = logging.getLogger(__name__)

def sanitize_sensitive(text: str) -> str:
    """Sanitize passwords, 2FA codes, and API tokens from text before saving into memory."""
    if not text:
        return text
    clean = text
    # 1. key: value or key=value
    clean = re.sub(
        r"\b(password|passwd|pwd|secret|api_key|token|auth_token|bearer)\s*[:=]\s*['\"]?([^'\"\s]+)['\"]?",
        r"\1: [REDACTED]",
        clean,
        flags=re.I,
    )
    # 2. 6-digit 2FA / OTP codes
    clean = re.sub(r"\b\d{6}\b", "[REDACTED_2FA]", clean)
    # 3. Bearer tokens: "bearer <token>" -> "bearer [REDACTED]"
    clean = re.sub(r"\b(bearer)\s+[a-zA-Z0-9_\-\.]{10,}\b", r"\1 [REDACTED]", clean, flags=re.I)
    return clean


class MemoryStore:
    """Unified memory manager with explicit separation of Task, Preference, Knowledge, and Run History."""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        if base_dir:
            self.base_dir = Path(base_dir)
        else:
            self.base_dir = Path(get_settings().log_dir).parent / "memory"
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self._pref_path = self.base_dir / "preferences.json"
        self._history_path = self.base_dir / "history.json"
        self._db_path = self.base_dir / "knowledge.db"
        self._lock = threading.RLock()

        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS knowledge_sources (
                        path TEXT PRIMARY KEY,
                        indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        tags TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_docs USING fts5(
                        path,
                        filename,
                        content,
                        tags
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

    # -----------------------------------------------------------------------
    # 1. Preference Memory (user-approved defaults)
    # -----------------------------------------------------------------------
    def get_preferences(self) -> dict[str, Any]:
        """View all stored user preferences."""
        with self._lock:
            if not self._pref_path.exists():
                return {}
            try:
                return json.loads(self._pref_path.read_text(encoding="utf-8"))
            except Exception:
                return {}

    def get_preference(self, key: str, default: Any = None) -> Any:
        return self.get_preferences().get(key, default)

    def set_preference(self, key: str, value: Any) -> None:
        """Set a durable, user-approved default (e.g. 'preferred_save_folder': 'Downloads')."""
        if any(w in key.lower() for w in ("password", "token", "secret", "auth", "credential", "otp")):
            raise ValueError(f"Sensitive key '{key}' cannot be saved in persistent memory.")

        clean_val = sanitize_sensitive(str(value)) if isinstance(value, str) else value
        with self._lock:
            prefs = self.get_preferences()
            prefs[key] = clean_val
            self._pref_path.write_text(json.dumps(prefs, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Saved user preference: %s = %s", key, clean_val)

    def forget_preference(self, key: str) -> bool:
        """Remove a preference from memory."""
        with self._lock:
            prefs = self.get_preferences()
            if key in prefs:
                del prefs[key]
                self._pref_path.write_text(json.dumps(prefs, indent=2, ensure_ascii=False), encoding="utf-8")
                return True
        return False

    def clear_preferences(self) -> None:
        """Clear all stored preferences."""
        with self._lock:
            if self._pref_path.exists():
                self._pref_path.unlink()

    # -----------------------------------------------------------------------
    # 2. Knowledge Index (user-approved documents & passages)
    # -----------------------------------------------------------------------
    def add_to_knowledge_index(self, file_path: str | Path, tags: str = "") -> bool:
        """Index a user-approved file for searchable knowledge retrieval."""
        path = Path(file_path).resolve()
        if not path.exists() or path.is_dir():
            return False

        try:
            if path.suffix.lower() == ".pdf":
                from ..shortcuts.pdf import run as pdf_run
                content = pdf_run({"action": "read", "src": str(path)})
            else:
                content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning("Could not read file for indexing (%s): %s", path, exc)
            return False

        content = sanitize_sensitive(content)

        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO knowledge_sources(path, tags) VALUES(?, ?)",
                    (str(path), tags),
                )
                conn.execute("DELETE FROM knowledge_docs WHERE path = ?", (str(path),))
                conn.execute(
                    "INSERT INTO knowledge_docs(path, filename, content, tags) VALUES(?, ?, ?, ?)",
                    (str(path), path.name, content, tags),
                )
                conn.commit()
                return True
            finally:
                conn.close()

    def search_knowledge(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Search indexed user documents."""
        clean_q = re.sub(r"[^\w\s]", " ", query).strip()
        tokens = [t for t in clean_q.split() if len(t) > 2]
        if not tokens:
            return []

        fts_query = " OR ".join(f'"{t}"' for t in tokens)
        results = []
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT path, filename, snippet(knowledge_docs, 2, '>>>', '<<<', '...', 25), rank, tags
                    FROM knowledge_docs WHERE knowledge_docs MATCH ?
                    ORDER BY rank LIMIT ?
                    """,
                    (fts_query, limit),
                )
                for row in cursor.fetchall():
                    results.append({
                        "path": row[0],
                        "filename": row[1],
                        "snippet": row[2],
                        "score": round(float(row[3]), 3),
                        "tags": row[4],
                    })
            except Exception as exc:
                logger.debug("Knowledge search error: %s", exc)
            finally:
                conn.close()
        return results

    def list_knowledge_sources(self) -> list[dict[str, Any]]:
        """List all indexed document sources."""
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT path, indexed_at, tags FROM knowledge_sources ORDER BY indexed_at DESC")
                return [{"path": r[0], "indexed_at": r[1], "tags": r[2]} for r in cursor.fetchall()]
            finally:
                conn.close()

    def remove_knowledge_source(self, file_path: str) -> bool:
        """Remove a source from the index."""
        path_str = str(Path(file_path).resolve())
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute("DELETE FROM knowledge_sources WHERE path = ?", (path_str,))
                conn.execute("DELETE FROM knowledge_docs WHERE path = ?", (path_str,))
                conn.commit()
                return True
            finally:
                conn.close()

    # -----------------------------------------------------------------------
    # 3. Task Memory (Bridge to recipes)
    # -----------------------------------------------------------------------
    def get_task_memory(self):
        from .recipes import RecipeStore
        return RecipeStore(get_settings().recipes_path)

    # -----------------------------------------------------------------------
    # 4. Run History (Audit logs with sensitive redaction)
    # -----------------------------------------------------------------------
    def record_run(self, run_id: str, goal: str, status: str, summary: str = "", metadata: dict[str, Any] | None = None) -> None:
        """Store clean audit record of run."""
        clean_goal = sanitize_sensitive(goal)
        clean_summary = sanitize_sensitive(summary)
        clean_meta = {}
        if metadata:
            for k, v in metadata.items():
                if isinstance(v, str):
                    clean_meta[k] = sanitize_sensitive(v)
                elif isinstance(v, dict):
                    clean_meta[k] = {ik: sanitize_sensitive(str(iv)) if isinstance(iv, str) else iv for ik, iv in v.items()}
                else:
                    clean_meta[k] = v
        record = {
            "run_id": run_id,
            "goal": clean_goal,
            "status": status,
            "summary": clean_summary,
            "metadata": clean_meta,
        }
        with self._lock:
            entries = []
            if self._history_path.exists():
                try:
                    entries = json.loads(self._history_path.read_text(encoding="utf-8"))
                except Exception:
                    entries = []
            entries.append(record)
            # Keep up to 200 records
            entries = entries[-200:]
            self._history_path.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")

    def get_history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            if not self._history_path.exists():
                return []
            try:
                entries = json.loads(self._history_path.read_text(encoding="utf-8"))
                return entries[-limit:]
            except Exception:
                return []


# Global memory singleton
_memory: MemoryStore | None = None


def get_memory() -> MemoryStore:
    global _memory
    if _memory is None:
        _memory = MemoryStore()
    return _memory


def set_memory(mem: MemoryStore | None) -> None:
    global _memory
    _memory = mem
