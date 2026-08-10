from __future__ import annotations

import argparse
import os
import queue
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .config import CONFIG_VERSION, IDENTITY_RE, ClientConfig, ConfigStore, ProfileConfig
from .database import StateDatabase
from .manager import ArchiveManager
from .service import ArchiveService
from .sources import RcloneSftpSource


ACTIVE_JOB_STATES = {"queued", "running", "cancelling"}
STATUS_LABELS = {
    "verified": "验证通过",
    "downloading": "下载中",
    "verifying": "校验中",
    "ready": "可下载",
    "remote_running": "远端归档中",
    "waiting_manifest": "等待发布",
    "remote_failed": "远端失败",
    "manifest_changed": "清单已变化",
    "error": "处理失败",
    "cancelled": "已取消",
    "interrupted": "等待恢复",
    "unknown": "未知",
}


def bytes_text(value: Any) -> str:
    number = float(value or 0)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    index = 0
    while number >= 1024 and index < len(units) - 1:
        number /= 1024
        index += 1
    precision = 0 if number >= 10 or index == 0 else 1
    return f"{number:.{precision}f} {units[index]}"


def duration_text(value: Any) -> str:
    seconds = max(0, int(float(value or 0)))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {seconds} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def centered_geometry(
    screen_width: int,
    screen_height: int,
    *,
    width: int = 1040,
    height: int = 680,
) -> str:
    left = max(0, (int(screen_width) - width) // 2)
    top = max(0, (int(screen_height) - height) // 2)
    return f"{width}x{height}+{left}+{top}"


def cached_directory_result(
    browse_index: list[dict[str, Any]],
    meta: dict[str, Any],
    path: str,
) -> dict[str, Any]:
    entries: dict[str, dict[str, Any]] = {}
    for source_item in browse_index:
        relative = str(source_item.get("path") or "")
        if path and not relative.startswith(path + "/"):
            continue
        remainder = relative[len(path) + 1 :] if path else relative
        if not remainder:
            continue
        first, separator, _rest = remainder.partition("/")
        child_path = f"{path}/{first}" if path else first
        if separator:
            entry = entries.setdefault(
                child_path,
                {
                    "type": "directory",
                    "path": child_path,
                    "name": first,
                    "entry_count": 0,
                    "locations": [],
                },
            )
            entry["entry_count"] += 1
            for location in source_item.get("locations") or [source_item.get("location")]:
                if location and location not in entry["locations"]:
                    entry["locations"].append(location)
        else:
            entries[child_path] = {**source_item, "path": child_path, "name": first}
    ordered = sorted(
        entries.values(),
        key=lambda item: (item.get("type") != "directory", str(item.get("name") or "").casefold()),
    )
    files = [item for item in ordered if item.get("type") == "file"]
    return {
        **meta,
        "path": path,
        "parent_path": "/".join(path.split("/")[:-1]) if path else "",
        "entry_count": len(ordered),
        "object_count": len(files),
        "bytes_total": sum(int(item.get("size_bytes") or 0) for item in files),
        "row_count": sum(int(item.get("row_count") or 0) for item in files),
        "entries": ordered,
    }


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        if self.handle.read(1) == b"":
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            self.handle.close()
            self.handle = None
            return False
        return True

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


class DesktopConfigStore(ConfigStore):
    """Expose only Ubuntu SFTP profiles to the Windows desktop process."""

    def load(self) -> ClientConfig:
        config = super().load()
        config.profiles = self._normalize_profiles(config.profiles)
        return config

    def save(self, config: ClientConfig) -> None:
        config.profiles = self._normalize_profiles(config.profiles)
        super().save(config)

    @staticmethod
    def _normalize_profiles(profiles: list[ProfileConfig]) -> list[ProfileConfig]:
        connection = next(
            (profile for profile in profiles if profile.source_type == "ubuntu_sftp"),
            None,
        )
        if connection is None:
            return []
        return [
            replace(
                connection,
                profile_id="ubuntu",
                display_name="Ubuntu 归档",
                collector_id="all",
                sftp_auto_discover=True,
            )
        ]

    def runtime_config(self, config: ClientConfig) -> ClientConfig:
        connection = next(
            (profile for profile in config.profiles if profile.sftp_auto_discover),
            None,
        )
        if connection is None:
            return replace(config, profiles=[])
        source = RcloneSftpSource(config, connection)
        profiles = [
            replace(
                connection,
                profile_id=collector_id,
                display_name=collector_id,
                collector_id=collector_id,
                sftp_auto_discover=False,
            )
            for collector_id in sorted(source.list_collectors())
        ]
        return replace(config, profiles=profiles)

    def local_profiles(self, config: ClientConfig) -> list[ProfileConfig]:
        connection = next(
            (profile for profile in config.profiles if profile.sftp_auto_discover),
            None,
        )
        if connection is None:
            return []
        profiles: list[ProfileConfig] = []
        for path in sorted(config.archive_root.glob("collector=*")):
            if not path.is_dir():
                continue
            collector_id = path.name.removeprefix("collector=")
            if not IDENTITY_RE.fullmatch(collector_id):
                continue
            profiles.append(
                replace(
                    connection,
                    profile_id=collector_id,
                    display_name=collector_id,
                    collector_id=collector_id,
                    sftp_auto_discover=False,
                )
            )
        return profiles


class ArchiveDesktopApp:
    COLORS = {
        "bg": "#f5f5f5",
        "surface": "#ffffff",
        "surface2": "#fafafa",
        "line": "#e5e5e5",
        "text": "#1f1f1f",
        "muted": "#686868",
        "brand": "#0f766e",
        "brand_dark": "#0b5f59",
        "brand_soft": "#dff3ef",
        "sidebar": "#f8f8f8",
        "sidebar_text": "#202020",
        "sidebar_muted": "#666666",
        "warn": "#9a6500",
        "bad": "#b52e38",
    }

    def __init__(self, root: tk.Tk, store: ConfigStore) -> None:
        self.root = root
        self.store = store
        self.database = StateDatabase(store.root / "state.sqlite3")
        self.service = ArchiveService(store, self.database)
        self.service.start()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="desktop-worker")
        self.results: queue.Queue[
            tuple[str, int, str, Any, Callable[[Any], None] | None]
        ] = queue.Queue()
        self.async_tokens: dict[str, int] = {}
        self.updating_directory_tree = False
        self.closing = False
        self.active_page = "files"
        self.active_scope = "remote"
        self.browser: dict[str, dict[str, Any]] = {
            "remote": self._new_browser_state(),
            "local": self._new_browser_state(),
        }
        self.tree_actions: dict[str, tuple[str, str]] = {}
        self.list_entries: dict[str, dict[str, Any]] = {}
        self.remote_profiles: list[ProfileConfig] = []
        self.local_profiles_cache: list[ProfileConfig] = []
        self.ubuntu_connection_vars: dict[str, tk.Variable] = {}
        self.nav_buttons: dict[str, tk.Button] = {}
        self.pages: dict[str, tk.Frame] = {}
        self.setting_vars: dict[str, tk.Variable] = {}
        self._configure_window()
        self._configure_styles()
        self._build_shell()
        self._load_settings()
        self._refresh_profile_choices()
        self.show_page("files")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self._drain_results)
        self.root.after(250, self._refresh_runtime)
        self.root.after(350, self._discover_collectors)

    @staticmethod
    def _new_browser_state() -> dict[str, Any]:
        return {
            "profile_id": "",
            "dates": [],
            "archive_date": "",
            "path": "",
            "browse_index": [],
            "meta": {},
            "result": {},
            "query": "",
        }

    def _configure_window(self) -> None:
        self.root.title(f"SMSI 归档备份 · {__version__}")
        width, height = 1040, 680
        self.root.geometry(
            centered_geometry(
                self.root.winfo_screenwidth(),
                self.root.winfo_screenheight(),
                width=width,
                height=height,
            )
        )
        self.root.minsize(width, height)
        self.root.configure(bg=self.COLORS["bg"])
        self.app_icon = self._create_app_icon()
        self.root.iconphoto(True, self.app_icon)

    def _create_app_icon(self) -> tk.PhotoImage:
        icon = tk.PhotoImage(width=32, height=32)
        icon.put(self.COLORS["brand"], to=(0, 0, 32, 32))
        icon.put("#ffffff", to=(7, 7, 25, 10))
        icon.put("#ffffff", to=(7, 13, 25, 16))
        icon.put("#ffffff", to=(7, 19, 25, 22))
        icon.put(self.COLORS["brand"], to=(14, 5, 18, 19))
        icon.put("#ffffff", to=(12, 16, 20, 19))
        icon.put("#ffffff", to=(14, 19, 18, 25))
        return icon

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TFrame", background=self.COLORS["bg"])
        style.configure("Surface.TFrame", background=self.COLORS["surface"])
        style.configure("Toolbar.TFrame", background=self.COLORS["surface2"])
        style.configure("TLabel", background=self.COLORS["bg"], foreground=self.COLORS["text"])
        style.configure("Surface.TLabel", background=self.COLORS["surface"], foreground=self.COLORS["text"])
        style.configure("Muted.TLabel", background=self.COLORS["surface"], foreground=self.COLORS["muted"], font=("Segoe UI", 9))
        style.configure("Title.TLabel", background=self.COLORS["bg"], foreground=self.COLORS["text"], font=("Segoe UI Semibold", 18))
        style.configure("Section.TLabel", background=self.COLORS["surface"], foreground=self.COLORS["text"], font=("Segoe UI Semibold", 10))
        style.configure("TButton", font=("Segoe UI", 9), padding=(10, 7))
        style.configure("Primary.TButton", background=self.COLORS["brand"], foreground="#ffffff")
        style.map("Primary.TButton", background=[("active", self.COLORS["brand_dark"]), ("disabled", "#93aaa7")])
        style.configure("Danger.TButton", foreground=self.COLORS["bad"])
        style.configure("Treeview", rowheight=36, font=("Segoe UI", 9), background=self.COLORS["surface"], fieldbackground=self.COLORS["surface"], borderwidth=0)
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9), padding=(8, 7), background=self.COLORS["surface2"])
        style.map("Treeview", background=[("selected", self.COLORS["brand_soft"])], foreground=[("selected", self.COLORS["brand_dark"])])
        style.configure("Horizontal.TProgressbar", troughcolor="#edf1f3", background=self.COLORS["brand"], thickness=7)

    def _build_shell(self) -> None:
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(1, weight=1)
        sidebar = tk.Frame(self.root, width=176, bg=self.COLORS["sidebar"])
        sidebar.grid(row=0, column=0, rowspan=2, sticky="nsew")
        sidebar.grid_propagate(False)
        main = tk.Frame(self.root, bg=self.COLORS["bg"])
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_rowconfigure(0, weight=1)
        main.grid_columnconfigure(0, weight=1)
        self.page_host = tk.Frame(main, bg=self.COLORS["bg"])
        self.page_host.grid(row=0, column=0, sticky="nsew", padx=18, pady=(16, 12))
        self.page_host.grid_rowconfigure(0, weight=1)
        self.page_host.grid_columnconfigure(0, weight=1)
        self._build_sidebar(sidebar)
        self._build_files_page()
        self._build_jobs_page()
        self._build_settings_page()
        self._build_transfer_bar()

    def _build_sidebar(self, parent: tk.Frame) -> None:
        brand = tk.Frame(parent, bg=self.COLORS["sidebar"])
        brand.pack(fill="x", padx=18, pady=(20, 24))
        mark = tk.Label(
            brand,
            image=self.app_icon,
            width=32,
            height=32,
            bg=self.COLORS["sidebar"],
        )
        mark.pack(side="left")
        name = tk.Frame(brand, bg=self.COLORS["sidebar"])
        name.pack(side="left", padx=(10, 0))
        tk.Label(name, text="SMSI", bg=self.COLORS["sidebar"], fg=self.COLORS["sidebar_text"], font=("Segoe UI Semibold", 11)).pack(anchor="w")
        tk.Label(name, text=f"归档备份 · {__version__}", bg=self.COLORS["sidebar"], fg=self.COLORS["sidebar_muted"], font=("Segoe UI", 8)).pack(anchor="w")
        tk.Label(parent, text="工作区", bg=self.COLORS["sidebar"], fg=self.COLORS["sidebar_muted"], font=("Segoe UI Semibold", 8)).pack(anchor="w", padx=18, pady=(0, 5))
        for key, label in (("files", "归档同步"), ("local", "本地文件"), ("jobs", "任务记录"), ("settings", "设置")):
            button = tk.Button(
                parent,
                text=label,
                anchor="w",
                relief="flat",
                bd=0,
                padx=18,
                pady=11,
                bg=self.COLORS["sidebar"],
                fg=self.COLORS["sidebar_muted"],
                activebackground=self.COLORS["brand_soft"],
                activeforeground=self.COLORS["sidebar_text"],
                font=("Segoe UI Semibold", 10),
                command=lambda page=key: self.show_page(page),
            )
            button.pack(fill="x", padx=8, pady=2)
            self.nav_buttons[key] = button
        footer = tk.Frame(parent, bg=self.COLORS["sidebar"])
        footer.pack(side="bottom", fill="x", padx=18, pady=16)
        self.sidebar_disk = tk.Label(footer, text="本地磁盘 --", bg=self.COLORS["sidebar"], fg=self.COLORS["sidebar_muted"], font=("Segoe UI", 8))
        self.sidebar_disk.pack(anchor="w")
        self.sidebar_state = tk.Label(footer, text="正在启动", bg=self.COLORS["sidebar"], fg=self.COLORS["brand_dark"], font=("Segoe UI", 9))
        self.sidebar_state.pack(anchor="w", pady=(8, 0))

    def _new_page(self, key: str) -> tk.Frame:
        page = tk.Frame(self.page_host, bg=self.COLORS["bg"])
        page.grid(row=0, column=0, sticky="nsew")
        self.pages[key] = page
        return page

    def _page_header(self, page: tk.Frame, title: str, subtitle: str = "") -> tk.Frame:
        header = tk.Frame(page, bg=self.COLORS["bg"])
        header.pack(fill="x", pady=(0, 12))
        title_box = tk.Frame(header, bg=self.COLORS["bg"])
        title_box.pack(side="left")
        ttk.Label(title_box, text=title, style="Title.TLabel").pack(anchor="w")
        if subtitle:
            tk.Label(title_box, text=subtitle, bg=self.COLORS["bg"], fg=self.COLORS["muted"], font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 0))
        return header

    def _build_files_page(self) -> None:
        page = self._new_page("files")
        header = tk.Frame(page, bg=self.COLORS["bg"])
        header.pack(fill="x", pady=(0, 12))
        title_box = tk.Frame(header, bg=self.COLORS["bg"])
        title_box.pack(side="left")
        self.files_title_label = ttk.Label(title_box, text="归档同步", style="Title.TLabel")
        self.files_title_label.pack(anchor="w")
        self.files_subtitle_label = tk.Label(title_box, text="Ubuntu 归档目录", bg=self.COLORS["bg"], fg=self.COLORS["muted"], font=("Segoe UI", 9))
        self.files_subtitle_label.pack(anchor="w", pady=(2, 0))
        header_actions = tk.Frame(header, bg=self.COLORS["bg"])
        header_actions.pack(side="right", pady=3)
        self.refresh_collectors_button = ttk.Button(header_actions, text="刷新", command=self.refresh_source)
        self.refresh_collectors_button.pack(side="left", padx=(0, 8))
        self.sync_all_button = ttk.Button(header_actions, text="同步缺失归档", style="Primary.TButton", command=lambda: self.request_scan(True))
        self.sync_all_button.pack(side="left")

        connection = tk.Frame(page, bg=self.COLORS["surface"], highlightbackground=self.COLORS["line"], highlightthickness=1)
        connection.pack(fill="x", pady=(0, 10))
        connection.grid_columnconfigure(1, weight=1)
        self.connection_title_label = tk.Label(connection, text="Ubuntu 归档源", bg=self.COLORS["surface"], fg=self.COLORS["text"], font=("Segoe UI Semibold", 10))
        self.connection_title_label.grid(row=0, column=0, sticky="w", padx=(14, 8), pady=(11, 3))
        self.connection_status_label = tk.Label(connection, text="正在连接…", bg=self.COLORS["surface"], fg=self.COLORS["warn"], font=("Segoe UI Semibold", 9))
        self.connection_status_label.grid(row=0, column=1, sticky="w", pady=(11, 3))
        self.collector_status_label = tk.Label(connection, text="归档目录自动发现中", bg=self.COLORS["surface"], fg=self.COLORS["muted"], font=("Segoe UI", 8))
        self.collector_status_label.grid(row=1, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 11))
        browser = tk.Frame(page, bg=self.COLORS["surface"], highlightbackground=self.COLORS["line"], highlightthickness=1)
        browser.pack(fill="both", expand=True)
        toolbar = tk.Frame(browser, bg=self.COLORS["surface2"], height=54)
        toolbar.pack(fill="x")
        toolbar.pack_propagate(False)
        self.scope_buttons: dict[str, tk.Button] = {}
        self.up_button = ttk.Button(toolbar, text="↑", width=3, command=self.go_up)
        self.up_button.pack(side="left", padx=(10, 4), pady=9)
        self.refresh_button = ttk.Button(
            toolbar, text="↻", width=3, command=self.refresh_browser
        )
        self.refresh_button.pack(side="left", padx=(0, 6), pady=9)
        self.path_var = tk.StringVar(value="归档根目录")
        path_entry = ttk.Entry(toolbar, textvariable=self.path_var, state="readonly")
        path_entry.pack(side="left", fill="x", expand=True, pady=9)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._render_current_list())
        tk.Label(
            toolbar,
            text="搜索",
            bg=self.COLORS["surface2"],
            fg=self.COLORS["muted"],
            font=("Segoe UI", 8),
        ).pack(side="left", padx=(8, 4))
        self.search_entry = ttk.Entry(toolbar, textvariable=self.search_var, width=18)
        self.search_entry.pack(side="left", padx=(0, 6), pady=9)
        self.download_button = ttk.Button(
            toolbar,
            text="下载并校验",
            command=self.download_selected_date,
        )
        self.download_button.pack(side="left", padx=(0, 10), pady=9)
        self.file_notice = tk.Label(browser, text="等待 Ubuntu 连接和归档目录", anchor="w", bg=self.COLORS["surface"], fg=self.COLORS["muted"], padx=12, pady=7, font=("Segoe UI", 9))
        self.file_notice.pack(fill="x")
        panes = ttk.Panedwindow(browser, orient="horizontal")
        panes.pack(fill="both", expand=True)
        tree_frame = tk.Frame(panes, bg=self.COLORS["surface2"], width=180)
        list_frame = tk.Frame(panes, bg=self.COLORS["surface"])
        panes.add(tree_frame, weight=0)
        panes.add(list_frame, weight=1)
        tk.Label(tree_frame, text="目录", anchor="w", bg=self.COLORS["surface2"], fg=self.COLORS["muted"], padx=10, pady=9, font=("Segoe UI Semibold", 9)).pack(fill="x")
        self.directory_tree = ttk.Treeview(tree_frame, show="tree", selectmode="browse")
        tree_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.directory_tree.yview)
        self.directory_tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.pack(side="right", fill="y")
        self.directory_tree.pack(fill="both", expand=True)
        self.directory_tree.bind("<<TreeviewSelect>>", self._tree_selected)
        columns = ("type", "size", "status", "local")
        self.file_list = ttk.Treeview(list_frame, columns=columns, show="tree headings", selectmode="browse")
        self.file_list.heading("#0", text="名称")
        self.file_list.heading("type", text="类型")
        self.file_list.heading("size", text="大小")
        self.file_list.heading("status", text="归档状态")
        self.file_list.heading("local", text="本地副本")
        self.file_list.column("#0", width=230, minwidth=170)
        self.file_list.column("type", width=170, minwidth=110)
        self.file_list.column("size", width=85, minwidth=70, anchor="e")
        self.file_list.column("status", width=100, minwidth=80)
        self.file_list.column("local", width=110, minwidth=90)
        list_y = ttk.Scrollbar(list_frame, orient="vertical", command=self.file_list.yview)
        list_x = ttk.Scrollbar(list_frame, orient="horizontal", command=self.file_list.xview)
        self.file_list.configure(yscrollcommand=list_y.set, xscrollcommand=list_x.set)
        list_y.pack(side="right", fill="y")
        list_x.pack(side="bottom", fill="x")
        self.file_summary = tk.Label(list_frame, text="--", anchor="w", bg=self.COLORS["surface2"], fg=self.COLORS["muted"], padx=10, pady=7, font=("Segoe UI", 8))
        self.file_summary.pack(fill="x", side="bottom")
        self.file_list.pack(fill="both", expand=True)
        self.file_list.bind("<Double-1>", self._list_open)
        self.file_list.bind("<Return>", self._list_open)

    def _build_jobs_page(self) -> None:
        page = self._new_page("jobs")
        header = self._page_header(page, "任务记录", "下载、校验与任务恢复记录")
        self.cancel_button = ttk.Button(
            header,
            text="取消当前任务",
            style="Danger.TButton",
            command=self.cancel_task,
            state="disabled",
        )
        self.cancel_button.pack(side="right", pady=4)
        surface = tk.Frame(page, bg=self.COLORS["surface"], highlightbackground=self.COLORS["line"], highlightthickness=1)
        surface.pack(fill="both", expand=True)
        columns = ("location", "state", "progress", "data", "updated")
        self.jobs_tree = ttk.Treeview(surface, columns=columns, show="tree headings")
        self.jobs_tree.heading("#0", text="任务")
        for key, label in (("location", "采集服务器 / 日期"), ("state", "状态"), ("progress", "进度"), ("data", "数据量"), ("updated", "更新时间")):
            self.jobs_tree.heading(key, text=label)
        self.jobs_tree.column("#0", width=180)
        self.jobs_tree.column("location", width=230)
        self.jobs_tree.column("state", width=100)
        self.jobs_tree.column("progress", width=110)
        self.jobs_tree.column("data", width=190)
        self.jobs_tree.column("updated", width=170)
        scroll = ttk.Scrollbar(surface, orient="vertical", command=self.jobs_tree.yview)
        self.jobs_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.jobs_tree.pack(fill="both", expand=True)

    def _build_settings_page(self) -> None:
        page = self._new_page("settings")
        header = self._page_header(page, "设置", "本地存储与 Ubuntu 只读 SFTP")
        ttk.Button(header, text="保存设置", style="Primary.TButton", command=self.save_settings).pack(side="right", pady=4)
        body = tk.Frame(page, bg=self.COLORS["bg"])
        body.pack(fill="both", expand=True)
        general = tk.Frame(body, bg=self.COLORS["surface"], highlightbackground=self.COLORS["line"], highlightthickness=1)
        general.pack(fill="x", pady=(0, 12))
        tk.Label(general, text="同步策略", bg=self.COLORS["surface"], fg=self.COLORS["text"], font=("Segoe UI Semibold", 10)).grid(row=0, column=0, columnspan=2, sticky="w", padx=14, pady=(12, 8))
        self.setting_vars["local_root"] = tk.StringVar()
        tk.Label(general, text="本地归档目录", bg=self.COLORS["surface"], fg=self.COLORS["muted"], font=("Segoe UI", 8)).grid(row=1, column=0, sticky="w", padx=(14, 6), pady=8)
        root_entry = ttk.Entry(general, textvariable=self.setting_vars["local_root"])
        root_entry.grid(row=1, column=1, sticky="ew", padx=(0, 48), pady=8)
        ttk.Button(general, text="…", width=3, command=self.choose_archive_root).grid(row=1, column=1, sticky="e", padx=(0, 14), pady=8)
        for key in ("poll_minutes", "history_days", "download_workers", "bandwidth_limit", "minimum_free_gib"):
            self.setting_vars[key] = tk.StringVar()
        auto = tk.BooleanVar()
        self.setting_vars["auto_download"] = auto
        ttk.Checkbutton(general, text="打开客户端后自动检查并复制缺失归档", variable=auto).grid(row=2, column=0, columnspan=2, sticky="w", padx=14, pady=(2, 4))
        self.advanced_summary = tk.Label(general, text="", anchor="w", bg=self.COLORS["surface"], fg=self.COLORS["muted"], font=("Segoe UI", 8))
        self.advanced_summary.grid(row=3, column=0, sticky="w", padx=14, pady=(2, 12))
        ttk.Button(general, text="调整高级下载设置…", command=self.edit_advanced_settings).grid(row=3, column=1, sticky="e", padx=14, pady=(2, 12))
        general.grid_columnconfigure(1, weight=1)
        connection = tk.Frame(body, bg=self.COLORS["surface"], highlightbackground=self.COLORS["line"], highlightthickness=1)
        connection.pack(fill="x", pady=(0, 12))
        tk.Label(connection, text="Ubuntu 连接", bg=self.COLORS["surface"], fg=self.COLORS["text"], font=("Segoe UI Semibold", 10)).grid(row=0, column=0, columnspan=3, sticky="w", padx=14, pady=(12, 2))
        tk.Label(connection, text="/archive 对应 Ubuntu 的 /data/smsi-archive", bg=self.COLORS["surface"], fg=self.COLORS["muted"], font=("Segoe UI", 8)).grid(row=1, column=0, columnspan=3, sticky="w", padx=14, pady=(0, 10))
        fields = (
            ("sftp_host", "Ubuntu 主机", "192.168.2.240"),
            ("sftp_port", "SSH 端口", "22"),
            ("sftp_user", "只读用户", "smsi-archive-reader"),
            ("sftp_root", "SFTP 根目录", "/archive"),
            ("sftp_key_file", "SSH 私钥", ""),
            ("sftp_known_hosts_file", "known_hosts", ""),
        )
        for row, (key, label, default) in enumerate(fields, start=2):
            tk.Label(connection, text=label, bg=self.COLORS["surface"], fg=self.COLORS["muted"], font=("Segoe UI", 8)).grid(row=row, column=0, sticky="w", padx=(14, 8), pady=5)
            variable = tk.StringVar(value=default)
            self.ubuntu_connection_vars[key] = variable
            field = tk.Frame(connection, bg=self.COLORS["surface"])
            field.grid(row=row, column=1, columnspan=2, sticky="ew", padx=(0, 14), pady=4)
            field.grid_columnconfigure(0, weight=1)
            ttk.Entry(field, textvariable=variable).grid(row=0, column=0, sticky="ew")
            if key in {"sftp_key_file", "sftp_known_hosts_file"}:
                ttk.Button(field, text="选择…", width=8, command=lambda target=variable: self._choose_file(target)).grid(row=0, column=1, padx=(7, 0))
        self.connection_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(connection, text="启用 Ubuntu 归档连接", variable=self.connection_enabled).grid(row=8, column=1, sticky="w", padx=(0, 14), pady=(5, 13))
        connection.grid_columnconfigure(1, weight=1)


    def _build_transfer_bar(self) -> None:
        bar = tk.Frame(self.root, height=52, bg=self.COLORS["surface"], highlightbackground=self.COLORS["line"], highlightthickness=1)
        bar.grid(row=1, column=1, sticky="ew")
        bar.grid_propagate(False)
        self.transfer_title = tk.Label(bar, text="传输空闲", anchor="w", bg=self.COLORS["surface"], fg=self.COLORS["text"], font=("Segoe UI Semibold", 9))
        self.transfer_title.pack(side="left", padx=(14, 10))
        self.transfer_detail = tk.Label(bar, text="等待自动检查", anchor="w", bg=self.COLORS["surface"], fg=self.COLORS["muted"], font=("Segoe UI", 8), width=24)
        self.transfer_detail.pack(side="left")
        self.transfer_progress = ttk.Progressbar(bar, orient="horizontal", mode="determinate", maximum=100, length=170)
        self.transfer_progress.pack(side="left", fill="x", expand=True, padx=12)
        self.transfer_stats = tk.Label(bar, text="速度 -- · 剩余 --", bg=self.COLORS["surface"], fg=self.COLORS["muted"], font=("Segoe UI", 8))
        self.transfer_stats.pack(side="left", padx=10)
        ttk.Button(bar, text="查看任务", command=lambda: self.show_page("jobs")).pack(side="right", padx=(0, 12), pady=7)

    def show_page(self, key: str) -> None:
        self.active_page = key
        page_key = "files" if key == "local" else key
        self.pages[page_key].tkraise()
        for page, button in self.nav_buttons.items():
            active = page == key
            button.configure(
                bg=self.COLORS["brand_soft"] if active else self.COLORS["sidebar"],
                fg=self.COLORS["brand_dark"] if active else self.COLORS["sidebar_muted"],
            )
        if key == "local":
            self.set_scope("local")
        elif key == "files":
            self.set_scope("remote")
        elif key == "jobs":
            self._render_jobs()
        elif key == "settings":
            self._load_settings()

    def set_scope(self, scope: str) -> None:
        self.active_scope = scope
        if scope == "local":
            self.local_profiles_cache = self.store.local_profiles(self.store.load())
            self.files_title_label.configure(text="本地文件")
            self.files_subtitle_label.configure(text=str(self.store.load().archive_root))
            self.connection_title_label.configure(text="本地归档")
            self.connection_status_label.configure(text="已验证副本", fg=self.COLORS["brand_dark"])
            self.sync_all_button.pack_forget()
        else:
            connection = next((item for item in self.store.load().profiles if item.source_type == "ubuntu_sftp"), None)
            self.files_title_label.configure(text="归档同步")
            self.files_subtitle_label.configure(text="Ubuntu 归档目录")
            self.connection_title_label.configure(text="Ubuntu 归档源")
            if connection is not None:
                self.connection_status_label.configure(text=f"{connection.sftp_user}@{connection.sftp_host}:{connection.sftp_port} · {connection.sftp_root}", fg=self.COLORS["brand_dark"])
            self.sync_all_button.pack(side="left")
        for key, button in self.scope_buttons.items():
            active = key == scope
            button.configure(
                bg=self.COLORS["brand"] if active else self.COLORS["surface"],
                fg="#ffffff" if active else self.COLORS["muted"],
            )
        self._refresh_profile_choices()
        self._render_browser_state()
        if not self.browser[scope]["dates"]:
            self.refresh_dates(force=True)

    def refresh_source(self) -> None:
        if self.active_scope == "remote":
            self._discover_collectors()
            return
        self.local_profiles_cache = self.store.local_profiles(self.store.load())
        self.browser["local"] = self._new_browser_state()
        self._refresh_profile_choices()
        if self.local_profiles_cache:
            self.refresh_dates(force=True)
        else:
            self._show_empty_browser("本地还没有已验证归档")

    def refresh_browser(self) -> None:
        if not self.browser[self.active_scope]["profile_id"]:
            self.refresh_source()
            return
        self.refresh_dates(force=True)

    def _profile_choices(self) -> list[ProfileConfig]:
        profiles = self.remote_profiles if self.active_scope == "remote" else self.local_profiles_cache
        return [profile for profile in profiles if profile.enabled]

    def _refresh_profile_choices(self) -> None:
        profiles = self._profile_choices()
        state = self.browser[self.active_scope]
        selected = next((profile for profile in profiles if profile.profile_id == state["profile_id"]), None)
        if selected is None:
            state["profile_id"] = ""
        self.refresh_button.configure(state="normal")
        if hasattr(self, "sync_all_button"):
            self.sync_all_button.configure(state="normal" if profiles and self.active_scope == "remote" else "disabled")
        if hasattr(self, "collector_status_label"):
            if profiles:
                source = "自动发现" if self.active_scope == "remote" else "本地已存在"
                self.collector_status_label.configure(text=f"{source} {len(profiles)} 个归档目录")
            elif self.active_scope == "local":
                self.collector_status_label.configure(text="本地还没有已发布的 collector=* 归档")
            else:
                self.collector_status_label.configure(text="尚未发现 collector=* 归档目录")

    def _selected_profile(self) -> ProfileConfig | None:
        profile_id = self.browser[self.active_scope].get("profile_id")
        return next((item for item in self._profile_choices() if item.profile_id == profile_id), None)

    def _profile_selected(self, profile_id: str) -> None:
        profile = next((item for item in self._profile_choices() if item.profile_id == profile_id), None)
        if profile is None:
            return
        self.browser[self.active_scope] = self._new_browser_state()
        self.browser[self.active_scope]["profile_id"] = profile.profile_id
        self.refresh_dates(force=True)

    def _discover_collectors(self) -> None:
        connection = next((item for item in self.store.load().profiles if item.source_type == "ubuntu_sftp"), None)
        if connection is None or not connection.enabled:
            self.remote_profiles = []
            self.connection_status_label.configure(text="未配置 Ubuntu 连接", fg=self.COLORS["warn"])
            self._refresh_profile_choices()
            self._show_empty_browser("请在设置中填写 Ubuntu 连接")
            return
        self.connection_status_label.configure(
            text=f"{connection.sftp_user}@{connection.sftp_host}:{connection.sftp_port} · {connection.sftp_root}",
            fg=self.COLORS["brand_dark"],
        )
        self.refresh_collectors_button.configure(state="disabled")

        def operation() -> list[ProfileConfig]:
            runtime = self.store.runtime_config(self.store.load())
            return runtime.profiles

        self._run_async(
            "discover",
            "正在读取 Ubuntu 归档目录…",
            operation,
            self._collectors_loaded,
        )

    def _collectors_loaded(self, profiles: list[ProfileConfig]) -> None:
        self.remote_profiles = list(profiles)
        self.refresh_collectors_button.configure(state="normal")
        self._refresh_profile_choices()
        if profiles:
            self.file_notice.configure(text=f"已发现 {len(profiles)} 个归档目录，可以浏览或同步。", fg=self.COLORS["brand_dark"])
            if self.active_scope == "remote" and not self.browser["remote"]["dates"]:
                self.refresh_dates(force=True)
        else:
            self.file_notice.configure(text="Ubuntu 根目录下没有发现 collector=* 归档目录。", fg=self.COLORS["warn"])

    def _run_async(
        self,
        channel: str,
        label: str,
        operation: Callable[[], Any],
        callback: Callable[[Any], None] | None = None,
    ) -> None:
        token = self.async_tokens.get(channel, 0) + 1
        self.async_tokens[channel] = token
        self.file_notice.configure(text=label, fg=self.COLORS["muted"])
        self.refresh_button.configure(state="disabled")

        def worker() -> None:
            try:
                result = operation()
                self.results.put((channel, token, "ok", result, callback))
            except Exception as exc:
                self.results.put((channel, token, "error", exc, callback))

        self.executor.submit(worker)

    def _drain_results(self) -> None:
        if self.closing:
            return
        try:
            while True:
                channel, token, status, result, callback = self.results.get_nowait()
                if token != self.async_tokens.get(channel):
                    continue
                channel_is_visible = channel == f"browser:{self.active_scope}"
                if channel_is_visible:
                    self.refresh_button.configure(state="normal")
                if channel == "discover":
                    self.refresh_collectors_button.configure(state="normal")
                if status == "error":
                    self.file_notice.configure(text=str(result), fg=self.COLORS["bad"])
                    if channel == "discover":
                        self.connection_status_label.configure(text="连接失败", fg=self.COLORS["bad"])
                    messagebox.showerror("操作失败", str(result), parent=self.root)
                elif callback is not None:
                    callback(result)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_results)

    def refresh_dates(self, *, force: bool = False) -> None:
        profile = self._selected_profile()
        if profile is None:
            self.browser[self.active_scope] = self._new_browser_state()
            self._render_browser_state()
            return
        state = self.browser[self.active_scope]
        if not force and state["profile_id"] == profile.profile_id and state["dates"]:
            self._render_browser_state()
            return
        scope = self.active_scope
        state["profile_id"] = profile.profile_id

        def operation() -> dict[str, Any]:
            manager = ArchiveManager(self._browser_config(scope), self.database)
            return manager.browse_dates(profile.profile_id, scope=scope)

        self._run_async(
            f"browser:{scope}",
            "正在读取归档日期…",
            operation,
            lambda result: self._dates_loaded(scope, result),
        )

    def _dates_loaded(self, scope: str, result: dict[str, Any]) -> None:
        state = self.browser[scope]
        state.update({"dates": result.get("dates") or [], "archive_date": "", "path": "", "browse_index": [], "meta": {}, "result": {}})
        if scope == self.active_scope:
            self.file_notice.configure(text=f"{len(state['dates'])} 个归档日期", fg=self.COLORS["muted"])
            self._render_browser_state()

    def open_date(self, archive_date: str) -> None:
        state = self.browser[self.active_scope]
        state["archive_date"] = archive_date
        state["path"] = ""
        self._load_directory("")

    def _load_directory(self, path: str) -> None:
        state = self.browser[self.active_scope]
        if state["browse_index"] and state["meta"].get("archive_date") == state["archive_date"]:
            state["result"] = cached_directory_result(state["browse_index"], state["meta"], path)
            state["path"] = path
            self._render_browser_state()
            return
        scope = self.active_scope
        profile_id = state["profile_id"]
        archive_date = state["archive_date"]

        def operation() -> dict[str, Any]:
            manager = ArchiveManager(self._browser_config(scope), self.database)
            return manager.browse_files(profile_id, archive_date, scope=scope, path=path)

        self._run_async(
            f"browser:{scope}",
            f"正在读取 {archive_date}…",
            operation,
            lambda result: self._directory_loaded(scope, result),
        )

    def _browser_config(self, scope: str) -> ClientConfig:
        config = self.store.load()
        profiles = self.remote_profiles if scope == "remote" else self.local_profiles_cache
        return replace(config, profiles=list(profiles))

    def _directory_loaded(self, scope: str, result: dict[str, Any]) -> None:
        state = self.browser[scope]
        state["browse_index"] = result.get("browse_index") or []
        state["meta"] = {
            key: result.get(key)
            for key in ("scope", "profile_id", "archive_date", "state", "detail", "download_eligible", "download_block_reason", "manifest_sha256")
        }
        state["result"] = result
        state["path"] = str(result.get("path") or "")
        if scope == self.active_scope:
            self._render_browser_state()

    def go_up(self) -> None:
        state = self.browser[self.active_scope]
        if state["path"]:
            self._load_directory("/".join(state["path"].split("/")[:-1]))
            return
        if state["archive_date"]:
            state["archive_date"] = ""
            state["result"] = {}
        elif state["profile_id"]:
            self.browser[self.active_scope] = self._new_browser_state()
        else:
            return
        self._render_browser_state()

    def _render_browser_state(self) -> None:
        state = self.browser[self.active_scope]
        self.search_var.set("")
        profile = self._selected_profile()
        root_label = "Ubuntu 归档" if self.active_scope == "remote" else "本地归档"
        self.path_var.set(
            " / ".join(part for part in (root_label, profile.collector_id if profile else "", state["archive_date"], state["path"]) if part)
        )
        self.up_button.configure(state="normal" if state["profile_id"] else "disabled")
        self._render_directory_tree()
        self._render_current_list()
        self._update_download_action()

    def _render_directory_tree(self) -> None:
        tree = self.directory_tree
        self.updating_directory_tree = True
        try:
            tree.delete(*tree.get_children())
            self.tree_actions.clear()
            state = self.browser[self.active_scope]
            root_text = "Ubuntu 归档" if self.active_scope == "remote" else "本地归档"
            root_id = tree.insert("", "end", text=root_text, open=True)
            self.tree_actions[root_id] = ("root", "")
            profiles = self._profile_choices()
            if not state["profile_id"]:
                for profile in profiles:
                    item_id = tree.insert(root_id, "end", text=f"▸  collector={profile.collector_id}")
                    self.tree_actions[item_id] = ("profile", profile.profile_id)
                return
            profile = self._selected_profile()
            if profile is None:
                return
            profile_id = tree.insert(root_id, "end", text=f"▸  collector={profile.collector_id}", open=True)
            self.tree_actions[profile_id] = ("profile", profile.profile_id)
            if not state["archive_date"]:
                for item in state["dates"]:
                    archive_date = str(item.get("archive_date") or "")
                    item_id = tree.insert(profile_id, "end", text=f"▣  {archive_date}")
                    self.tree_actions[item_id] = ("date", archive_date)
                return
            date_id = tree.insert(profile_id, "end", text=f"▣  {state['archive_date']}", open=True)
            self.tree_actions[date_id] = ("path", "")
            nodes: dict[str, str] = {"": date_id}
            paths: set[str] = set()
            for item in state["browse_index"]:
                parts = str(item.get("path") or "").split("/")[:-1]
                for index in range(len(parts)):
                    paths.add("/".join(parts[: index + 1]))
            for path in sorted(paths, key=lambda value: (value.count("/"), value.casefold())):
                parent_path = "/".join(path.split("/")[:-1])
                item_id = tree.insert(
                    nodes.get(parent_path, date_id),
                    "end",
                    text=f"▸  {path.split('/')[-1]}",
                    open=path == state["path"] or state["path"].startswith(path + "/"),
                )
                nodes[path] = item_id
                self.tree_actions[item_id] = ("path", path)
            tree.item(root_id, open=True)
            selected = nodes.get(state["path"], date_id)
            tree.selection_set(selected)
            tree.see(selected)
        finally:
            self.updating_directory_tree = False

    def _tree_selected(self, _event: tk.Event[Any]) -> None:
        if self.updating_directory_tree:
            return
        selection = self.directory_tree.selection()
        if not selection:
            return
        action = self.tree_actions.get(selection[0])
        if action is None:
            return
        kind, value = action
        state = self.browser[self.active_scope]
        if kind == "root":
            if not state["profile_id"]:
                return
            self.browser[self.active_scope] = self._new_browser_state()
            self._render_browser_state()
        elif kind == "profile":
            if state["profile_id"] == value and not state["archive_date"]:
                return
            self._profile_selected(value)
        elif kind == "date":
            if state["archive_date"] == value:
                return
            self.open_date(value)
        else:
            if state["path"] == value:
                return
            self._load_directory(value)

    def _visible_entries(self) -> list[dict[str, Any]]:
        state = self.browser[self.active_scope]
        query_text = self.search_var.get().strip().casefold()
        if not state["profile_id"]:
            entries = [
                {
                    "type": "collector",
                    "name": f"collector={profile.collector_id}",
                    "path": f"collector={profile.collector_id}",
                    "profile_id": profile.profile_id,
                }
                for profile in self._profile_choices()
            ]
        elif not state["archive_date"]:
            entries = [
                {
                    "type": "date",
                    "name": item.get("archive_date"),
                    "path": item.get("archive_date"),
                    **item,
                }
                for item in state["dates"]
            ]
        else:
            entries = list(state["result"].get("entries") or [])
        if not query_text:
            return entries
        return [
            item
            for item in entries
            if query_text in " ".join(
                str(item.get(key) or "")
                for key in ("name", "path", "kind", "table_name", "location")
            ).casefold()
        ]

    def _render_current_list(self) -> None:
        if not hasattr(self, "file_list"):
            return
        state = self.browser[self.active_scope]
        self.file_list.delete(*self.file_list.get_children())
        self.list_entries.clear()
        entries = self._visible_entries()
        for index, item in enumerate(entries):
            item_type = str(item.get("type") or "file")
            if item_type == "collector":
                values = ("归档目录", "--", "自动发现", "--")
            elif item_type == "date":
                values = (
                    "归档日期",
                    bytes_text(item.get("bytes_total")),
                    STATUS_LABELS.get(str(item.get("status") or "unknown"), str(item.get("status") or "未知")),
                    "本地存在" if item.get("local") else "未下载",
                )
            elif item_type == "directory":
                values = (
                    f"文件夹 · {int(item.get('entry_count') or 0)} 个对象",
                    "--",
                    "--",
                    "--",
                )
            elif self.active_scope == "remote":
                values = (
                    " · ".join(filter(None, (str(item.get("kind") or ""), str(item.get("table_name") or "")))) or "归档对象",
                    bytes_text(item.get("size_bytes")),
                    "已发布",
                    {"present": "已验证", "staged": "已暂存", "downloading": "下载中", "missing": "待下载", "mismatch": "大小异常"}.get(str(item.get("local_state") or ""), "未知"),
                )
            else:
                values = (
                    "本地文件",
                    bytes_text(item.get("size_bytes")),
                    "已列入清单" if item.get("remote_state") == "listed" else "本地对象",
                    "下载中" if item.get("state") == "downloading" else "已验证" if item.get("location") == "verified" else "暂存目录",
                )
            icon = {"collector": "▸", "date": "▣", "directory": "▸"}.get(item_type, "▤")
            item_id = self.file_list.insert(
                "", "end", text=f"{icon}  {str(item.get('name') or '')}", values=values
            )
            self.list_entries[item_id] = item
        result = state.get("result") or {}
        suffix = f" · 搜索结果 {len(entries)} 项" if self.search_var.get().strip() else ""
        if not state["profile_id"]:
            self.file_summary.configure(text=f"{len(entries)} 个归档目录{suffix}")
            self.file_notice.configure(text=f"{len(entries)} 个归档目录", fg=self.COLORS["muted"])
        else:
            self.file_summary.configure(
                text=f"{len(entries)} 项 · {bytes_text(result.get('bytes_total'))} · {int(result.get('row_count') or 0):,} 行{suffix}"
            )
        if state["profile_id"] and not state["archive_date"]:
            hint = " · 双击日期查看对象" if state["dates"] else ""
            self.file_notice.configure(
                text=f"{len(state['dates'])} 个归档日期{hint}",
                fg=self.COLORS["muted"],
            )
        elif state["archive_date"]:
            detail = str(result.get("detail") or "")
            self.file_notice.configure(text=detail or "目录已读取", fg=self.COLORS["muted"])

    def _list_open(self, _event: tk.Event[Any]) -> None:
        selection = self.file_list.selection()
        if not selection:
            return
        item = self.list_entries.get(selection[0]) or {}
        if item.get("type") == "collector":
            self._profile_selected(str(item.get("profile_id") or ""))
        elif item.get("type") == "date":
            self.open_date(str(item.get("archive_date") or ""))
        elif item.get("type") == "directory":
            self._load_directory(str(item.get("path") or ""))

    def _update_download_action(self) -> None:
        state = self.browser[self.active_scope]
        enabled = False
        reason = "本地归档只读浏览" if self.active_scope == "local" else "选择归档日期后检查下载资格"
        if self.active_scope == "remote" and state["archive_date"] and state["meta"]:
            day = self.database.day(state["profile_id"], state["archive_date"]) or {}
            verified = day.get("status") == "verified"
            blocked = day.get("status") in {"manifest_changed", "remote_failed", "error"}
            busy = self.service.status().get("running") or self.database.active_job()
            enabled = bool(state["meta"].get("download_eligible")) and not verified and not blocked and not busy
            reason = str(state["meta"].get("download_block_reason") or state["meta"].get("detail") or "远端归档已验证，可以下载")
            if verified:
                reason = "本地归档已经完整验证"
            elif blocked:
                reason = str(day.get("error") or day.get("detail") or "当前归档状态异常")
            elif busy:
                reason = "已有任务正在执行或等待执行"
        self.download_button.configure(state="normal" if enabled else "disabled")
        if self.active_scope == "remote" and state["archive_date"]:
            self.file_notice.configure(text=reason, fg=self.COLORS["brand_dark"] if enabled else self.COLORS["warn"])

    def download_selected_date(self) -> None:
        state = self.browser["remote"]
        if not state["profile_id"] or not state["archive_date"]:
            return
        try:
            self.service.request_download(state["profile_id"], state["archive_date"])
            self.file_notice.configure(text="下载任务已加入队列", fg=self.COLORS["brand_dark"])
            self._update_download_action()
        except RuntimeError as exc:
            messagebox.showerror("无法开始下载", str(exc), parent=self.root)

    def request_scan(self, download: bool) -> None:
        try:
            self.service.request_scan(download=download)
            self.show_page("jobs")
        except RuntimeError as exc:
            messagebox.showerror("无法开始任务", str(exc), parent=self.root)

    def cancel_task(self) -> None:
        try:
            self.service.request_cancel()
        except RuntimeError as exc:
            messagebox.showinfo("取消任务", str(exc), parent=self.root)

    def _refresh_runtime(self) -> None:
        if self.closing:
            return
        runtime = self.service.status()
        active = runtime.get("current_job") or self.database.active_job()
        progress = runtime.get("progress") or {}
        total = int(progress.get("bytes_total") or (active or {}).get("bytes_total") or 0)
        done = int(progress.get("bytes_transferred") or progress.get("bytes_done") or (active or {}).get("bytes_done") or 0)
        objects = int(progress.get("object_count") or (active or {}).get("object_count") or 0)
        objects_done = int(progress.get("objects_done") or (active or {}).get("objects_done") or 0)
        percent = done / total * 100 if total else objects_done / objects * 100 if objects else 0
        if active:
            label = {"scan": "检查 Ubuntu 归档", "scan_download": "同步缺失归档", "download": "指定日期下载", "verify": "重新校验"}.get(str(active.get("action") or ""), "后台任务")
            self.transfer_title.configure(text=label)
            self.transfer_detail.configure(text=str(progress.get("current_object") or active.get("detail") or "任务执行中"))
        else:
            self.transfer_title.configure(text="传输空闲")
            self.transfer_detail.configure(text=str(runtime.get("detail") or "等待自动检查"))
        self.transfer_progress["value"] = percent
        speed = int(progress.get("speed_bytes_per_second") or (active or {}).get("speed_bytes_per_second") or 0)
        eta = progress.get("eta_seconds") if progress.get("eta_seconds") is not None else (active or {}).get("eta_seconds")
        self.transfer_stats.configure(text=f"{percent:.1f}% · 速度 {bytes_text(speed)}/秒 · 剩余 {duration_text(eta) if eta is not None else '--'}")
        disk = runtime.get("disk") or {}
        ratio = int(int(disk.get("used") or 0) / int(disk.get("total") or 1) * 100)
        self.sidebar_disk.configure(text=f"本地磁盘 {ratio}% · 可用 {bytes_text(disk.get('free'))}")
        self.sidebar_state.configure(text="任务执行中" if active else "客户端在线")
        if hasattr(self, "cancel_button"):
            self.cancel_button.configure(state="normal" if active else "disabled")
        if hasattr(self, "sync_all_button"):
            self.sync_all_button.configure(
                state="disabled" if active or not self._profile_choices() else "normal"
            )
        if self.active_page == "jobs":
            self._render_jobs()
        if self.active_page in {"files", "local"}:
            self._update_download_action()
        self.root.after(1000, self._refresh_runtime)

    def _render_jobs(self) -> None:
        if not hasattr(self, "jobs_tree"):
            return
        self.jobs_tree.delete(*self.jobs_tree.get_children())
        for job in self.database.jobs(200):
            action = {"scan": "检查 Ubuntu 归档", "scan_download": "同步缺失归档", "download": "指定日期下载", "verify": "重新校验"}.get(str(job.get("action") or ""), str(job.get("action") or "任务"))
            profile_id = str(job.get("profile_id") or "")
            location = " · ".join(filter(None, (f"collector={profile_id}" if profile_id else "全部归档目录", str(job.get("archive_date") or ""))))
            values = (
                location,
                {"queued": "排队中", "running": "执行中", "cancelling": "正在取消", "completed": "已完成", "cancelled": "已取消", "failed": "失败"}.get(str(job.get("status") or ""), str(job.get("status") or "")),
                f"{int(job.get('objects_done') or 0)}/{int(job.get('object_count') or 0)}",
                f"{bytes_text(job.get('bytes_done'))} / {bytes_text(job.get('bytes_total'))}",
                str(job.get("updated_at") or ""),
            )
            self.jobs_tree.insert("", "end", text=action, values=values)

    def _load_settings(self) -> None:
        if not self.setting_vars:
            return
        config = self.store.load()
        values = {
            "local_root": config.local_root,
            "poll_minutes": str(config.poll_minutes),
            "history_days": str(config.history_days),
            "download_workers": str(config.download_workers),
            "bandwidth_limit": config.bandwidth_limit,
            "minimum_free_gib": str(max(1, config.minimum_free_bytes // 1024**3)),
            "auto_download": config.auto_download,
        }
        for key, value in values.items():
            if key in self.setting_vars:
                self.setting_vars[key].set(value)
        connection = next((item for item in config.profiles if item.source_type == "ubuntu_sftp"), None)
        if connection is not None:
            values = {
                "sftp_host": connection.sftp_host,
                "sftp_port": str(connection.sftp_port),
                "sftp_user": connection.sftp_user,
                "sftp_root": connection.sftp_root,
                "sftp_key_file": connection.sftp_key_file,
                "sftp_known_hosts_file": connection.sftp_known_hosts_file,
            }
            for key, value in values.items():
                self.ubuntu_connection_vars[key].set(value)
            self.connection_enabled.set(connection.enabled)
        self._update_advanced_summary()

    def _update_advanced_summary(self) -> None:
        if not hasattr(self, "advanced_summary"):
            return
        self.advanced_summary.configure(
            text=(
                f"高级设置：每 {self.setting_vars['poll_minutes'].get()} 分钟检查 · "
                f"并发 {self.setting_vars['download_workers'].get()} · "
                f"带宽 {self.setting_vars['bandwidth_limit'].get()} · "
                f"保留空间 {self.setting_vars['minimum_free_gib'].get()} GiB"
            )
        )

    def choose_archive_root(self) -> None:
        selected = filedialog.askdirectory(parent=self.root, initialdir=self.setting_vars["local_root"].get() or str(Path.home()))
        if selected:
            self.setting_vars["local_root"].set(selected)

    def _choose_file(self, variable: tk.Variable) -> None:
        selected = filedialog.askopenfilename(parent=self.root)
        if selected:
            variable.set(selected)

    def edit_advanced_settings(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("高级下载设置")
        dialog.geometry("460x360")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        container = tk.Frame(dialog, bg=self.COLORS["surface"], padx=18, pady=16)
        container.pack(fill="both", expand=True, padx=12, pady=12)
        fields = (
            ("poll_minutes", "自动检查间隔（分钟）"),
            ("history_days", "扫描历史范围（天）"),
            ("download_workers", "下载并发"),
            ("bandwidth_limit", "带宽限制"),
            ("minimum_free_gib", "最低可用空间（GiB）"),
        )
        variables: dict[str, tk.StringVar] = {}
        for row, (key, label) in enumerate(fields):
            tk.Label(container, text=label, bg=self.COLORS["surface"], fg=self.COLORS["muted"], font=("Segoe UI", 9)).grid(row=row, column=0, sticky="w", pady=7)
            variable = tk.StringVar(value=str(self.setting_vars[key].get()))
            variables[key] = variable
            ttk.Entry(container, textvariable=variable, width=24).grid(row=row, column=1, sticky="ew", pady=7)
        error = tk.StringVar()
        tk.Label(container, textvariable=error, bg=self.COLORS["surface"], fg=self.COLORS["bad"], wraplength=400, justify="left").grid(row=len(fields), column=0, columnspan=2, sticky="w", pady=(4, 8))
        buttons = tk.Frame(container, bg=self.COLORS["surface"])
        buttons.grid(row=len(fields) + 1, column=0, columnspan=2, sticky="e")

        def apply() -> None:
            try:
                poll = int(variables["poll_minutes"].get())
                history = int(variables["history_days"].get())
                workers = int(variables["download_workers"].get())
                minimum = float(variables["minimum_free_gib"].get())
                if not 1 <= poll <= 1440:
                    raise ValueError("自动检查间隔必须为 1 至 1440 分钟")
                if not 1 <= history <= 3650:
                    raise ValueError("扫描历史范围必须为 1 至 3650 天")
                if not 1 <= workers <= 16:
                    raise ValueError("下载并发必须为 1 至 16")
                if minimum < 1:
                    raise ValueError("最低可用空间必须至少为 1 GiB")
                if not variables["bandwidth_limit"].get().strip():
                    raise ValueError("带宽限制不能为空")
                for key, variable in variables.items():
                    self.setting_vars[key].set(variable.get().strip())
                self._update_advanced_summary()
                dialog.destroy()
            except (TypeError, ValueError) as exc:
                error.set(str(exc))

        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right", padx=4)
        ttk.Button(buttons, text="确定", style="Primary.TButton", command=apply).pack(side="right", padx=4)
        container.grid_columnconfigure(1, weight=1)

    def save_settings(self) -> None:
        current = self.store.load()
        try:
            profiles: list[ProfileConfig] = []
            if self.connection_enabled.get():
                values = {key: variable.get() for key, variable in self.ubuntu_connection_vars.items()}
                profiles = [ProfileConfig.from_mapping({
                    "profile_id": "ubuntu",
                    "display_name": "Ubuntu 归档",
                    "collector_id": "all",
                    "source_type": "ubuntu_sftp",
                    "enabled": True,
                    **values,
                })]
            config = ClientConfig(
                config_version=CONFIG_VERSION,
                local_root=str(self.setting_vars["local_root"].get()).strip(),
                rclone_binary=current.rclone_binary,
                poll_minutes=int(str(self.setting_vars["poll_minutes"].get())),
                history_days=int(str(self.setting_vars["history_days"].get())),
                download_workers=int(str(self.setting_vars["download_workers"].get())),
                bandwidth_limit=str(self.setting_vars["bandwidth_limit"].get()).strip(),
                minimum_free_bytes=int(float(str(self.setting_vars["minimum_free_gib"].get())) * 1024**3),
                auto_download=bool(self.setting_vars["auto_download"].get()),
                web_host=current.web_host,
                web_port=current.web_port,
                password_hash=current.password_hash,
                session_secret=current.session_secret,
                profiles=profiles,
            )
            self.store.save(config)
            self.service.wake()
            self.browser = {"remote": self._new_browser_state(), "local": self._new_browser_state()}
            self.remote_profiles = []
            self.local_profiles_cache = []
            self._refresh_profile_choices()
            self._update_advanced_summary()
            self._discover_collectors()
            messagebox.showinfo("设置", "设置已保存。", parent=self.root)
        except (ValueError, TypeError) as exc:
            messagebox.showerror("设置无效", str(exc), parent=self.root)

    def _show_empty_browser(self, message: str) -> None:
        self.file_notice.configure(text=message, fg=self.COLORS["warn"])
        self.directory_tree.delete(*self.directory_tree.get_children())
        self.file_list.delete(*self.file_list.get_children())
        self.file_summary.configure(text=message)
        self.download_button.configure(state="disabled")

    def close(self) -> None:
        if self.closing:
            return
        active = self.database.active_job()
        if active and active.get("status") in ACTIVE_JOB_STATES:
            if not messagebox.askyesno(
                "退出客户端",
                "当前有任务正在执行。退出会中止当前进程，未完成对象会在下次启动时恢复。确定退出？",
                parent=self.root,
            ):
                return
        self.closing = True
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.service.stop()
        self.root.destroy()


def preflight(store: ConfigStore | None = None) -> dict[str, Any]:
    active_store = store or ConfigStore()
    config = active_store.load()
    StateDatabase(active_store.root / "state.sqlite3")
    return {
        "version": __version__,
        "state_dir": str(active_store.root),
        "archive_root": str(config.archive_root),
        "profiles": len(config.profiles),
        "tk": str(tk.TkVersion),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="SMSI Windows 原生归档备份客户端")
    parser.add_argument("--check", action="store_true", help="只检查桌面客户端运行环境")
    arguments = parser.parse_args()
    store = DesktopConfigStore()
    if arguments.check:
        result = preflight(store)
        print("desktop_ready " + " ".join(f"{key}={value}" for key, value in result.items()))
        return
    lock = InstanceLock(store.root / "desktop.lock")
    if not lock.acquire():
        check_root = tk.Tk()
        check_root.withdraw()
        messagebox.showinfo("SMSI 归档备份", "Windows 客户端已经在运行。", parent=check_root)
        check_root.destroy()
        return
    root = tk.Tk()
    try:
        ArchiveDesktopApp(root, store)
        root.mainloop()
    except Exception:
        error_path = store.root / "desktop-error.log"
        error_path.write_text(traceback.format_exc(), encoding="utf-8")
        try:
            messagebox.showerror(
                "SMSI 归档备份",
                f"桌面客户端启动失败，详细信息已写入：\n{error_path}",
                parent=root,
            )
        except tk.TclError:
            pass
        raise
    finally:
        lock.release()


if __name__ == "__main__":
    main()
