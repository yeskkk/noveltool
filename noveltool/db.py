"""One-project SQLite store, explicit transactions, and a lifetime process lock.

A connection is created, used, and closed in the same thread. Async web routes
and the autosave task execute short store operations on the event-loop thread;
there is deliberately no check_same_thread=False or uncoordinated worker pool.
"""

from __future__ import annotations

from collections.abc import Callable
from .migrations import scripts_after, migrate
import os
from pathlib import Path
import sqlite3
from types import TracebackType
from uuid import uuid4

from pydantic import ValidationError

from .domain import APPLICATION_ID, SCHEMA_VERSION, ProjectConfig, ProjectData, ProjectMeta, utc_now

try:
    import fcntl
except ImportError:  # Windows is explicitly outside this first Linux-oriented release.
    fcntl = None  # type: ignore[assignment]


class ProjectError(RuntimeError):
    """An actionable project storage error."""


class ProjectExistsError(ProjectError):
    pass


class ProjectNotFoundError(ProjectError):
    pass


class ProjectLockedError(ProjectError):
    pass


class InvalidProjectError(ProjectError):
    pass


class UnsupportedSchemaError(ProjectError):
    pass


class SaveConflictError(ProjectError):
    pass


class SaveFailedError(ProjectError):
    pass


class ProjectLock:
    """Advisory OS lock held for the lifetime of the in-memory project.

    Do not unlink the lock file on release: that could create two independent
    lock inodes and allow two writers. A leftover file does not mean a held lock.
    """

    def __init__(self, project_path: Path):
        self.path = project_path.with_name(project_path.name + ".lock")
        self.fd: int | None = None

    def acquire(self) -> None:
        if fcntl is None:
            raise ProjectError("此版本需要支持 flock 的系统；请使用 Linux 或 WSL")
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise ProjectLockedError("项目已被另一个 NovelTool 进程打开") from exc
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)  # Closing releases flock, including on process death.
            self.fd = None


CONFIG_COLUMNS = tuple(ProjectConfig.model_fields)
META_COLUMNS = tuple(ProjectMeta.model_fields)


def _database_values(config: ProjectConfig) -> list[object]:
    values = config.model_dump()
    return [int(values[key]) if isinstance(values[key], bool) else values[key]
            for key in CONFIG_COLUMNS]


class ProjectStore:
    def __init__(self, path: Path, connection: sqlite3.Connection, lock: ProjectLock):
        self.path = path
        self.connection = connection
        self._lock = lock
        self._closed = False
        self._project_id: str | None = None
        self.migration_backup: Path | None = None

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        # mode=rw is essential: opening a misspelled filename must not create a DB.
        conn = sqlite3.connect(
            path.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=5.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @staticmethod
    def _configure_owned_database(conn: sqlite3.Connection) -> None:
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise ProjectError("项目所在文件系统无法启用 SQLite WAL")
        # With minute-batched writes, choose stronger durability over NORMAL.
        conn.execute("PRAGMA synchronous = FULL")

    @classmethod
    def create(cls, path: Path, title: str, config: ProjectConfig | None = None) -> ProjectStore:
        path = Path(path).expanduser().resolve()
        now = utc_now()
        data = ProjectData(
            ProjectMeta(id=uuid4().hex, title=title, created_at=now, updated_at=now, saved_at=now),
            config if config is not None else ProjectConfig(),
        )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = ProjectLock(path)
        lock.acquire()
        created = False
        conn: sqlite3.Connection | None = None
        try:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as exc:
                raise ProjectExistsError("项目文件已存在；打开它时不要使用 --create") from exc
            os.close(fd)
            created = True
            conn = cls._connect(path)
            cls._configure_owned_database(conn)
            schema = "\n".join(scripts_after(0))
            # executescript is only used for this static, bundled schema. The
            # explicit BEGIN remains active for the parameterized inserts below.
            conn.executescript("BEGIN IMMEDIATE;\n" + schema)
            conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute(
                f"INSERT INTO project_meta(singleton, {', '.join(META_COLUMNS)}) "
                f"VALUES(1, {', '.join('?' for _ in META_COLUMNS)})",
                [getattr(data.meta, key) for key in META_COLUMNS],
            )
            conn.execute(
                f"INSERT INTO project_config(project_id, {', '.join(CONFIG_COLUMNS)}) "
                f"VALUES({', '.join('?' for _ in range(len(CONFIG_COLUMNS) + 1))})",
                [data.meta.id, *_database_values(data.config)],
            )
            conn.execute("COMMIT")
            store = cls(path, conn, lock)
            store._project_id = data.meta.id
            return store
        except BaseException as exc:
            try:
                if conn is not None:
                    try:
                        if conn.in_transaction:
                            conn.execute("ROLLBACK")
                    finally:
                        conn.close()
            finally:
                try:
                    if created:
                        for suffix in ("", "-wal", "-shm"):
                            Path(str(path) + suffix).unlink(missing_ok=True)
                finally:
                    lock.release()
            if isinstance(exc, sqlite3.Error):
                raise ProjectError(f"项目初始化失败：{exc}") from exc
            raise

    @classmethod
    def open(cls, path: Path) -> ProjectStore:
        path = Path(path).expanduser().resolve()
        if not path.is_file():
            raise ProjectNotFoundError("项目文件不存在；首次创建请使用 --create")
        lock = ProjectLock(path)
        lock.acquire()
        conn: sqlite3.Connection | None = None
        try:
            conn = cls._connect(path)
            if conn.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                raise InvalidProjectError("这不是 NovelTool 项目数据库；原文件不会被初始化或覆盖")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if not 1 <= version <= SCHEMA_VERSION:
                raise UnsupportedSchemaError(
                    f"数据库 schema={version}，本程序只支持 schema={SCHEMA_VERSION}；拒绝自动改写"
                )
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise InvalidProjectError("SQLite 快速完整性检查失败，请保留原文件并从备份恢复")
            store = cls(path, conn, lock)
            store.load(expected_schema=version)  # Validate old data before any migration.
            if version < SCHEMA_VERSION:
                store.migration_backup = migrate(conn, path, version)
                store.load()
            cls._configure_owned_database(conn)
            return store
        except BaseException as exc:
            try:
                if conn is not None:
                    conn.close()
            finally:
                lock.release()
            if isinstance(exc, sqlite3.Error):
                raise InvalidProjectError(f"数据库无法读取：{exc}") from exc
            raise

    def load(self, *, expected_schema: int = SCHEMA_VERSION) -> ProjectData:
        try:
            rows = self.connection.execute("SELECT * FROM project_meta").fetchall()
            if len(rows) != 1:
                raise InvalidProjectError("项目元数据必须恰好有一条记录")
            meta_values = dict(rows[0])
            if meta_values.pop("singleton") != 1:
                raise InvalidProjectError("无效的项目单例标识")
            meta = ProjectMeta.model_validate(meta_values)
            if meta.schema_version != expected_schema:
                raise UnsupportedSchemaError("项目记录与受支持的 schema 版本不匹配")
            config_rows = self.connection.execute("SELECT * FROM project_config").fetchall()
            if len(config_rows) != 1:
                raise InvalidProjectError("项目配置必须恰好有一条记录")
            values = dict(config_rows[0])
            if values.pop("project_id") != meta.id:
                raise InvalidProjectError("配置不属于当前项目")
            if type(values["retain_llm_logs"]) is not int or values["retain_llm_logs"] not in (0, 1):
                raise InvalidProjectError("retain_llm_logs 必须为 0 或 1")
            values["retain_llm_logs"] = bool(values["retain_llm_logs"])
            config = ProjectConfig.model_validate(values)
            if self.connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise InvalidProjectError("项目外键检查失败")
            self._project_id = meta.id
            return ProjectData(meta, config)
        except (ValidationError, KeyError, sqlite3.Error) as exc:
            raise InvalidProjectError(f"项目数据校验失败：{exc}") from exc

    def flush(self, data: ProjectData, *, expected_version: int, dirty: set[str],
              apply: Callable[[sqlite3.Connection], None] | None = None) -> ProjectData:
        """Commit a complete dirty batch, or leave disk unchanged on failure.

        The caller holds its in-memory project lock. Runtime dirty flags are
        cleared by the caller only AFTER this method returns successfully.
        """
        if not dirty:
            return data
        if not dirty <= {"meta", "config", "llm_runs"}:
            raise SaveFailedError("发现未知的 dirty 分类")
        if data.meta.id != self._project_id or data.meta.data_version <= expected_version:
            raise SaveFailedError("项目 ID 或数据版本不合法")
        saved_meta = ProjectMeta.model_validate({**data.meta.model_dump(), "saved_at": utc_now()})
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            cursor = self.connection.execute(
                "UPDATE project_meta SET title=?, data_version=?, updated_at=?, saved_at=? "
                "WHERE singleton=1 AND id=? AND data_version=?",
                (saved_meta.title, saved_meta.data_version, saved_meta.updated_at,
                 saved_meta.saved_at, saved_meta.id, expected_version),
            )
            if cursor.rowcount != 1:
                raise SaveConflictError("磁盘项目已发生外部变化；未覆盖磁盘，也未清除内存修改")
            if "config" in dirty:
                cursor = self.connection.execute(
                    f"UPDATE project_config SET {', '.join(key + '=?' for key in CONFIG_COLUMNS)} "
                    "WHERE project_id=?",
                    [*_database_values(data.config), saved_meta.id],
                )
                if cursor.rowcount != 1:
                    raise SaveFailedError("项目配置行丢失，保存已回滚")
            if apply is not None:
                apply(self.connection)
            self.connection.execute("COMMIT")
        except BaseException as exc:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            if isinstance(exc, (sqlite3.Error, OSError)):
                raise SaveFailedError(f"SQLite 保存失败：{exc}") from exc
            raise
        return ProjectData(saved_meta, data.config)

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.connection.close()
        finally:
            self._lock.release()
            self._closed = True

    def __enter__(self) -> ProjectStore:
        return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc: BaseException | None, traceback: TracebackType | None) -> None:
        # The store itself never implicitly saves. ProjectSession owns that policy.
        self.close()
