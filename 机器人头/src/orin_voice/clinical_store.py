"""SQLite persistence for the intake, task responses and teacher review.

The voice interview still writes its portable ``answers.json`` files.  This
module adds a small relational index around those files so a teacher can
review participant information, task answers and recorded media without
having to scan the session directories.  It has no third party dependency and
is safe to use from the HTTP server's worker threads: every operation opens a
short lived connection and transactions are committed atomically.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


READING_TEXT = (
    "有一回，北风跟太阳在那儿争论谁的本领大。说着说着，来了一个过路的，"
    "身上穿了一件厚袍子。他们俩就商量好了，说谁能先叫这个过路的把他的袍子"
    "脱下来，就算是他的本领大。北风就使劲吹起来，拼命地吹。可是，他吹得"
    "越厉害，那个人就把他的袍子裹得越紧。到末了儿，北风没辙了，只好就算"
    "了。一会儿，太阳出来一晒，那个人马上就把袍子脱了下来。所以，北风不得"
    "不承认，还是太阳比他的本领大。"
)

DEFAULT_TASKS = (
    ("reading", "固定文本阅读", READING_TEXT, 1),
    ("open_1", "开放性问题 1", "请描述一下，你昨天过的怎么样?", 2),
    ("open_2", "开放性问题 2", "请描述一下，现在回想最近一周最开心的记忆?", 3),
    ("open_3", "开放性问题 3", "请描述一下，现在回想最近一周最悲伤的记忆?", 4),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ClinicalStore:
    """Small SQLite repository used by the interview and teacher dashboard."""

    def __init__(self, root: str | os.PathLike[str], db_path: str | os.PathLike[str] | None = None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(db_path or self.root / "clinical.sqlite3").resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self.db_path.chmod(0o600)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=15000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS participants (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    height_cm REAL,
                    weight_kg REAL,
                    gender TEXT,
                    age INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    participant_id TEXT NOT NULL REFERENCES participants(id),
                    session_dir TEXT,
                    mode TEXT NOT NULL DEFAULT 'voice',
                    status TEXT NOT NULL DEFAULT 'in_progress',
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS sessions_started_idx ON sessions(started_at DESC);
                CREATE TABLE IF NOT EXISTS intake_answers (
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    question_key TEXT NOT NULL,
                    answer_text TEXT NOT NULL DEFAULT '',
                    value_num REAL,
                    PRIMARY KEY(session_id, question_key)
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    order_index INTEGER NOT NULL UNIQUE,
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS task_responses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    answer_text TEXT NOT NULL DEFAULT '',
                    started_at TEXT,
                    ended_at TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS task_responses_session_idx ON task_responses(session_id);
                CREATE TABLE IF NOT EXISTS media (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ('audio', 'video')),
                    path TEXT NOT NULL,
                    mime_type TEXT,
                    duration_s REAL,
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS media_session_idx ON media(session_id, kind);
                """
            )
            for kind, title, prompt, order_index in DEFAULT_TASKS:
                task_id = "task_" + kind
                db.execute(
                    "INSERT OR IGNORE INTO tasks(id, kind, title, prompt, order_index) VALUES(?,?,?,?,?)",
                    (task_id, kind, title, prompt, order_index),
                )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def create_participant(self, profile: Mapping[str, Any]) -> dict[str, Any]:
        """Create/update the intake profile, validating values at the boundary."""
        name = str(profile.get("name", "")).strip()
        if not name or len(name) > 120:
            raise ValueError("姓名不能为空且不能超过 120 个字符")
        def number(key: str, minimum: float, maximum: float) -> float | None:
            value = profile.get(key)
            if value in (None, ""):
                return None
            try:
                value = float(value)
            except (TypeError, ValueError) as error:
                raise ValueError(f"{key} 必须是数字") from error
            if not minimum <= value <= maximum:
                raise ValueError(f"{key} 超出有效范围")
            return value
        height = number("height_cm", 30, 250)
        weight = number("weight_kg", 2, 400)
        age = number("age", 1, 130)
        age = int(age) if age is not None else None
        gender = str(profile.get("gender", "")).strip()[:30] or None
        participant_id = str(profile.get("id") or uuid.uuid4().hex)
        now = utc_now()
        with self._connect() as db:
            db.execute(
                """INSERT INTO participants(id,name,height_cm,weight_kg,gender,age,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name,height_cm=excluded.height_cm,
                   weight_kg=excluded.weight_kg,gender=excluded.gender,age=excluded.age,updated_at=excluded.updated_at""",
                (participant_id, name, height, weight, gender, age, now, now),
            )
            return self._row(db.execute("SELECT * FROM participants WHERE id=?", (participant_id,)).fetchone()) or {}

    def create_session(self, participant_id: str, session_id: str | None = None,
                       session_dir: str | None = None, mode: str = "voice",
                       metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not session_id:
            session_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        if not participant_id:
            raise ValueError("participant_id 不能为空")
        with self._connect() as db:
            if not db.execute("SELECT 1 FROM participants WHERE id=?", (participant_id,)).fetchone():
                raise ValueError("participant 不存在")
            db.execute(
                """INSERT INTO sessions(id,participant_id,session_dir,mode,started_at,metadata_json)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                   participant_id=excluded.participant_id,session_dir=excluded.session_dir,
                   mode=excluded.mode,metadata_json=excluded.metadata_json""",
                (session_id, participant_id, session_dir, mode, utc_now(), json.dumps(metadata or {}, ensure_ascii=False)),
            )
            return self._row(db.execute("""SELECT s.*,p.name,p.height_cm,p.weight_kg,p.gender,p.age
                FROM sessions s JOIN participants p ON p.id=s.participant_id WHERE s.id=?""", (session_id,)).fetchone()) or {}

    def save_intake(self, session_id: str, answers: Mapping[str, Any]) -> None:
        with self._connect() as db:
            if not db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
                raise ValueError("session 不存在")
            for key, value in answers.items():
                if not isinstance(key, str) or not key or len(key) > 80:
                    raise ValueError("无效的 intake 字段")
                text = "" if value is None else str(value)[:2000]
                numeric = None
                try:
                    numeric = float(value) if value not in (None, "") else None
                except (TypeError, ValueError):
                    pass
                db.execute(
                    """INSERT INTO intake_answers(session_id,question_key,answer_text,value_num) VALUES(?,?,?,?)
                       ON CONFLICT(session_id,question_key) DO UPDATE SET answer_text=excluded.answer_text,value_num=excluded.value_num""",
                    (session_id, key, text, numeric),
                )

    def finish_session(self, session_id: str, status: str = "completed") -> None:
        if status not in {"completed", "stopped", "in_progress"}:
            raise ValueError("无效的会话状态")
        with self._connect() as db:
            db.execute("UPDATE sessions SET status=?,ended_at=COALESCE(ended_at,?) WHERE id=?", (status, utc_now(), session_id))

    def save_task_response(self, session_id: str, task_id: str, answer_text: str,
                           started_at: str | None = None, ended_at: str | None = None,
                           metadata: Mapping[str, Any] | None = None) -> int:
        answer_text = str(answer_text or "")[:10000]
        with self._connect() as db:
            existing = db.execute("SELECT id FROM task_responses WHERE session_id=? AND task_id=? ORDER BY id DESC LIMIT 1",
                                  (session_id, task_id)).fetchone()
            if existing:
                db.execute("""UPDATE task_responses SET answer_text=?,started_at=COALESCE(?,started_at),
                              ended_at=COALESCE(?,ended_at),metadata_json=? WHERE id=?""",
                           (answer_text, started_at, ended_at,
                            json.dumps(metadata or {}, ensure_ascii=False), int(existing["id"])))
                return int(existing["id"])
            cursor = db.execute(
                """INSERT INTO task_responses(session_id,task_id,answer_text,started_at,ended_at,metadata_json)
                   VALUES(?,?,?,?,?,?)""",
                (session_id, task_id, answer_text, started_at, ended_at,
                 json.dumps(metadata or {}, ensure_ascii=False)),
            )
            return int(cursor.lastrowid)

    def add_media(self, session_id: str, kind: str, path: str, mime_type: str | None = None,
                  duration_s: float | None = None, metadata: Mapping[str, Any] | None = None) -> int:
        if kind not in {"audio", "video"}:
            raise ValueError("媒体类型必须是 audio 或 video")
        media_path = Path(path)
        # Store a path relative to the project root; reject accidental absolute
        # paths outside it because the teacher UI later serves these files.
        try:
            relative = media_path.resolve().relative_to(self.root)
        except ValueError as error:
            raise ValueError("媒体文件必须位于项目数据目录内") from error
        with self._connect() as db:
            existing = db.execute("SELECT id FROM media WHERE session_id=? AND kind=? AND path=?",
                                  (session_id, kind, relative.as_posix())).fetchone()
            if existing:
                return int(existing["id"])
            cursor = db.execute(
                """INSERT INTO media(session_id,kind,path,mime_type,duration_s,created_at,metadata_json)
                   VALUES(?,?,?,?,?,?,?)""",
                (session_id, kind, relative.as_posix(), mime_type, duration_s, utc_now(),
                 json.dumps(metadata or {}, ensure_ascii=False)),
            )
            return int(cursor.lastrowid)

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM tasks WHERE active=1 ORDER BY order_index")]

    def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as db:
            return [dict(row) for row in db.execute("""SELECT s.*,p.name,p.height_cm,p.weight_kg,p.gender,p.age,
                 (SELECT COUNT(*) FROM media m WHERE m.session_id=s.id AND m.kind='audio') audio_count,
                 (SELECT COUNT(*) FROM media m WHERE m.session_id=s.id AND m.kind='video') video_count
                 FROM sessions s JOIN participants p ON p.id=s.participant_id
                 ORDER BY s.started_at DESC LIMIT ?""", (limit,))]

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._connect() as db:
            session = self._row(db.execute("""SELECT s.*,p.name,p.height_cm,p.weight_kg,p.gender,p.age
                FROM sessions s JOIN participants p ON p.id=s.participant_id WHERE s.id=?""", (session_id,)).fetchone())
            if not session:
                raise FileNotFoundError("会话不存在")
            session["metadata"] = json.loads(session.pop("metadata_json") or "{}")
            session["intake"] = [dict(r) for r in db.execute("SELECT * FROM intake_answers WHERE session_id=? ORDER BY question_key", (session_id,))]
            session["tasks"] = [dict(r) for r in db.execute("""SELECT tr.*,t.kind,t.title,t.prompt,t.order_index
                FROM task_responses tr JOIN tasks t ON t.id=tr.task_id WHERE tr.session_id=? ORDER BY t.order_index,tr.id""", (session_id,))]
            session["media"] = [dict(r) for r in db.execute("SELECT * FROM media WHERE session_id=? ORDER BY created_at,id", (session_id,))]
            for key in ("metadata_json",):
                session.pop(key, None)
            for item in session["tasks"] + session["media"]:
                if "metadata_json" in item:
                    item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            return self._attach_session_files(session)

    def _attach_session_files(self, session: dict[str, Any]) -> dict[str, Any]:
        """Add portable interview records without reading outside data root."""
        session.update(answers=None, events=[], summary=None,
                       recording={"video": None, "full_audio": None},
                       artifacts={}, review_warnings=[])
        stored_dir = session.get("session_dir")
        if not stored_dir:
            return session
        directory = Path(stored_dir).resolve()
        if not directory.is_relative_to(self.root):
            session["review_warnings"].append("会话文件目录超出数据目录，未读取原始记录")
            return session

        def load_json(relative: str):
            file = (directory / relative).resolve()
            if not file.is_relative_to(directory) or not file.is_file():
                return None
            try:
                return json.loads(file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                session["review_warnings"].append(f"无法读取 {relative}：{error}")
                return None

        answers = load_json("answers.json")
        if isinstance(answers, dict):
            session["answers"] = answers
            session["summary"] = answers.get("overall_summary")
        for kind, relative in (("video", "video/manifest.json"), ("full_audio", "full_audio/manifest.json")):
            manifest = load_json(relative)
            if isinstance(manifest, dict):
                session["recording"][kind] = manifest

        # Replay the append-only journal to include the latest playback/error
        # updates, including an in-progress conversation not yet exported.
        journal = (directory / "events.jsonl").resolve()
        if journal.is_relative_to(directory) and journal.is_file():
            events: dict[str, Any] = {}
            order: list[str] = []
            try:
                for line in journal.read_text(encoding="utf-8").splitlines():
                    try:
                        entry = json.loads(line)
                        if entry.get("operation") == "create" and isinstance(entry.get("event"), dict):
                            event = entry["event"]
                            event_id = event.get("id")
                            if isinstance(event_id, str):
                                if event_id not in events:
                                    order.append(event_id)
                                events[event_id] = event
                        elif entry.get("operation") == "update" and entry.get("id") in events:
                            fields = entry.get("fields")
                            if isinstance(fields, dict):
                                events[entry["id"]].update(fields)
                    except (ValueError, TypeError):
                        continue
                session["events"] = [events[event_id] for event_id in order]
            except OSError as error:
                session["review_warnings"].append(f"无法读取对话事件：{error}")
        if not session["events"]:
            transcript = load_json("transcript.json")
            if isinstance(transcript, dict) and isinstance(transcript.get("events"), list):
                session["events"] = transcript["events"]
        for name in ("answers.json", "summary.txt", "transcript.json", "session_package.json", "session_package.zip"):
            file = (directory / name).resolve()
            session["artifacts"][name] = bool(file.is_relative_to(directory) and file.is_file())
        if not session["summary"]:
            summary_file = (directory / "summary.txt").resolve()
            if summary_file.is_relative_to(directory) and summary_file.is_file():
                try:
                    session["summary"] = summary_file.read_text(encoding="utf-8")
                except OSError:
                    pass
        return session

    def get_media(self, media_id: int | str) -> dict[str, Any]:
        """Return one media row and its absolute path for a local HTTP route."""
        try:
            media_id = int(media_id)
        except (TypeError, ValueError) as error:
            raise FileNotFoundError("媒体不存在") from error
        with self._connect() as db:
            row = self._row(db.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone())
        if not row:
            raise FileNotFoundError("媒体不存在")
        relative = Path(row["path"])
        absolute = (self.root / relative).resolve()
        if not absolute.is_relative_to(self.root) or not absolute.is_file():
            raise FileNotFoundError("媒体文件不存在")
        row["absolute_path"] = absolute
        return row

    def sync_answers_json(self, session_dir: str | os.PathLike[str]) -> dict[str, Any]:
        """Index an existing Interview directory and return the stored session.

        This is intentionally idempotent, making it suitable for a post-save
        hook in the current interview service while old sessions remain usable.
        """
        directory = Path(session_dir).resolve()
        data = json.loads((directory / "answers.json").read_text(encoding="utf-8"))
        session_id = data.get("session_id") or directory.name
        profile = data.get("participant") or data.get("profile") or {}
        # Interview writes a participant profile without its database id. Find
        # an existing session first so repeated dashboard refreshes do not
        # create a new orphan participant on every sync.
        existing_participant_id = None
        with self._connect() as db:
            row = db.execute("SELECT participant_id FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row:
                existing_participant_id = row["participant_id"]
        profile_data = dict(profile) if isinstance(profile, Mapping) else {}
        if not str(profile_data.get("name", "")).strip():
            profile_data["name"] = "未填写"
        if existing_participant_id:
            profile_data["id"] = existing_participant_id
        participant = self.create_participant(profile_data)
        session = self.create_session(participant["id"], session_id,
                                      directory.as_posix(), data.get("mode", "voice"), {"source": "answers.json"})
        answers = data.get("intake") or data.get("profile") or data.get("participant") or {}
        if isinstance(answers, Mapping):
            self.save_intake(session["id"], answers)
        for question in data.get("intake_questions", []) + data.get("questions", []):
            for segment in question.get("segments", []):
                if segment.get("audio_path"):
                    try:
                        self.add_media(session["id"], "audio", directory / segment["audio_path"], "audio/wav", segment.get("duration_s"))
                    except ValueError:
                        pass
        for task in data.get("tasks", []):
            task_id = task.get("id")
            if task_id:
                self.save_task_response(session["id"], task_id, task.get("answer_text", ""),
                                        metadata={"kind": task.get("kind"), "title": task.get("title")})
            for segment in task.get("segments", []):
                if segment.get("audio_path"):
                    try:
                        self.add_media(session["id"], "audio", directory / segment["audio_path"],
                                       "audio/wav", segment.get("duration_s"),
                                       {"task_id": task_id})
                    except ValueError:
                        pass
        for folder, kind, mime in (("audio", "audio", "audio/wav"),
                                   ("video", "video", "video/x-msvideo"),
                                   ("full_audio", "audio", "audio/wav")):
            media_folder = directory / folder
            if not media_folder.is_dir():
                continue
            for media_file in media_folder.iterdir():
                if (not media_file.is_file() or media_file.name == "manifest.json" or
                        (kind == "audio" and media_file.suffix.lower() != ".wav") or
                        (kind == "video" and media_file.suffix.lower() not in {".avi", ".mp4", ".webm"})):
                    continue
                try:
                    media_mime = mime
                    if kind == "video":
                        media_mime = {".avi": "video/x-msvideo", ".mp4": "video/mp4", ".webm": "video/webm"}.get(media_file.suffix.lower(), mime)
                    self.add_media(session["id"], kind, media_file, media_mime)
                except ValueError:
                    pass
        if data.get("status") in {"completed", "stopped"}:
            self.finish_session(session["id"], data["status"])
        return self.get_session(session["id"])

