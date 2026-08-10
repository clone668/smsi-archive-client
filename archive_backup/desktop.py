from __future__ import annotations

import argparse
import os
import queue
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .config import CONFIG_VERSION, ClientConfig, ConfigStore, ProfileConfig
from .database import StateDatabase
from .manager import ArchiveManager
from .service import ArchiveService


ACTIVE_JOB_STATES = {"queued", "running", "cancelling"}
SOURCE_LABELS = {
    "google_drive": "Google Drive",
    "ubuntu_sftp": "Ubuntu 内网",
    "verified_directory": "已验证目录",
}
SOURCE_VALUES = {value: key for key, value in SOURCE_LABELS.items()}
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


class ArchiveDesktopApp:
    COLORS = {
        "bg": "#f3f5f7",
        "surface": "#ffffff",
        "surface2": "#f7f9fa",
        "line": "#dce2e6",
        "text": "#182126",
        "muted": "#68767e",
        "brand": "#126a63",
        "brand_dark": "#0b554f",
        "brand_soft": "#e3f2ef",
        "sidebar": "#1b2422",
        "sidebar_text": "#e9efed",
        "sidebar_muted": "#93a19e",
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
        self.profile_dialog: tk.Toplevel | None = None
        self.profile_drafts: list[ProfileConfig] = []
        self.nav_buttons: dict[str, tk.Button] = {}
        self.pages: dict[str, tk.Frame] = {}
        self.setting_vars: dict[str, tk.Variable] = {}
        self.detail_vars: dict[str, tk.StringVar] = {}
        self._configure_window()
        self._configure_styles()
        self._build_shell()
        self._load_settings()
        self._refresh_profile_choices()
        self.show_page("files")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self._drain_results)
        self.root.after(250, self._refresh_runtime)
        self.root.after(350, lambda: self.refresh_dates(force=True))

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
        self.root.geometry("1380x840")
        self.root.minsize(1040, 680)
        self.root.configure(bg=self.COLORS["bg"])
        try:
            self.root.state("zoomed")
        except tk.TclError:
            pass

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
        sidebar = tk.Frame(self.root, width=214, bg=self.COLORS["sidebar"])
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
        self._build_status_page()
        self._build_settings_page()
        self._build_transfer_bar()

    def _build_sidebar(self, parent: tk.Frame) -> None:
        brand = tk.Frame(parent, bg=self.COLORS["sidebar"])
        brand.pack(fill="x", padx=18, pady=(20, 24))
        mark = tk.Label(brand, text="S", width=2, height=1, bg=self.COLORS["brand"], fg="white", font=("Segoe UI Semibold", 16))
        mark.pack(side="left")
        name = tk.Frame(brand, bg=self.COLORS["sidebar"])
        name.pack(side="left", padx=(10, 0))
        tk.Label(name, text="SMSI", bg=self.COLORS["sidebar"], fg=self.COLORS["sidebar_text"], font=("Segoe UI Semibold", 11)).pack(anchor="w")
        tk.Label(name, text=f"归档备份 · {__version__}", bg=self.COLORS["sidebar"], fg=self.COLORS["sidebar_muted"], font=("Segoe UI", 8)).pack(anchor="w")
        tk.Label(parent, text="工作区", bg=self.COLORS["sidebar"], fg=self.COLORS["sidebar_muted"], font=("Segoe UI Semibold", 8)).pack(anchor="w", padx=18, pady=(0, 5))
        for key, label in (("files", "文件浏览"), ("jobs", "传输任务"), ("status", "运行状态"), ("settings", "设置")):
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
                activebackground="#26312f",
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
        self.sidebar_state = tk.Label(footer, text="正在启动", bg=self.COLORS["sidebar"], fg="#76cdb0", font=("Segoe UI", 9))
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
        header = self._page_header(page, "归档文件", "从 Google Drive、Ubuntu 内网或本地验证目录读取")
        scope_box = tk.Frame(header, bg=self.COLORS["bg"])
        scope_box.pack(side="right", pady=3)
        self.scope_buttons: dict[str, tk.Button] = {}
        for scope, label in (("remote", "云端来源"), ("local", "本地归档")):
            button = tk.Button(scope_box, text=label, bd=1, relief="solid", padx=14, pady=6, font=("Segoe UI Semibold", 9), command=lambda value=scope: self.set_scope(value))
            button.pack(side="left", padx=(0, 4))
            self.scope_buttons[scope] = button
        browser = tk.Frame(page, bg=self.COLORS["surface"], highlightbackground=self.COLORS["line"], highlightthickness=1)
        browser.pack(fill="both", expand=True)
        toolbar = tk.Frame(browser, bg=self.COLORS["surface2"], height=54)
        toolbar.pack(fill="x")
        toolbar.pack_propagate(False)
        self.up_button = ttk.Button(toolbar, text="↑", width=3, command=self.go_up)
        self.up_button.pack(side="left", padx=(10, 4), pady=9)
        self.refresh_button = ttk.Button(toolbar, text="刷新", command=lambda: self.refresh_dates(force=True))
        self.refresh_button.pack(side="left", padx=(0, 8), pady=9)
        self.path_var = tk.StringVar(value="归档根目录")
        path_entry = ttk.Entry(toolbar, textvariable=self.path_var, state="readonly")
        path_entry.pack(side="left", fill="x", expand=True, pady=9)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._render_current_list())
        self.search_entry = ttk.Entry(toolbar, textvariable=self.search_var, width=24)
        self.search_entry.pack(side="left", padx=8, pady=9)
        self.profile_var = tk.StringVar()
        self.profile_combo = ttk.Combobox(toolbar, textvariable=self.profile_var, state="readonly", width=22)
        self.profile_combo.pack(side="left", padx=(0, 8), pady=9)
        self.profile_combo.bind("<<ComboboxSelected>>", lambda _event: self._profile_changed())
        self.download_button = ttk.Button(toolbar, text="下载该日期", style="Primary.TButton", command=self.download_selected_date)
        self.download_button.pack(side="left", padx=(0, 10), pady=9)
        self.file_notice = tk.Label(browser, text="选择采集服务器后读取归档日期", anchor="w", bg=self.COLORS["surface"], fg=self.COLORS["muted"], padx=12, pady=8, font=("Segoe UI", 9))
        self.file_notice.pack(fill="x")
        panes = ttk.Panedwindow(browser, orient="horizontal")
        panes.pack(fill="both", expand=True)
        tree_frame = tk.Frame(panes, bg=self.COLORS["surface2"], width=220)
        list_frame = tk.Frame(panes, bg=self.COLORS["surface"])
        detail_frame = tk.Frame(panes, bg=self.COLORS["surface"], width=270)
        panes.add(tree_frame, weight=0)
        panes.add(list_frame, weight=1)
        panes.add(detail_frame, weight=0)
        tk.Label(tree_frame, text="目录", anchor="w", bg=self.COLORS["surface2"], fg=self.COLORS["muted"], padx=10, pady=9, font=("Segoe UI Semibold", 9)).pack(fill="x")
        self.directory_tree = ttk.Treeview(tree_frame, show="tree", selectmode="browse")
        tree_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.directory_tree.yview)
        self.directory_tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.pack(side="right", fill="y")
        self.directory_tree.pack(fill="both", expand=True)
        self.directory_tree.bind("<<TreeviewSelect>>", self._tree_selected)
        columns = ("type", "size", "rows", "status", "summary")
        self.file_list = ttk.Treeview(list_frame, columns=columns, show="tree headings", selectmode="browse")
        self.file_list.heading("#0", text="名称")
        self.file_list.heading("type", text="类型 / 数据表")
        self.file_list.heading("size", text="大小")
        self.file_list.heading("rows", text="行数")
        self.file_list.heading("status", text="本地状态")
        self.file_list.heading("summary", text="摘要")
        self.file_list.column("#0", width=310, minwidth=190)
        self.file_list.column("type", width=180, minwidth=120)
        self.file_list.column("size", width=90, minwidth=70, anchor="e")
        self.file_list.column("rows", width=100, minwidth=70, anchor="e")
        self.file_list.column("status", width=105, minwidth=80)
        self.file_list.column("summary", width=150, minwidth=100)
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
        self.file_list.bind("<<TreeviewSelect>>", self._list_selected)
        tk.Label(detail_frame, text="详细信息", anchor="w", bg=self.COLORS["surface"], fg=self.COLORS["muted"], padx=12, pady=9, font=("Segoe UI Semibold", 9)).pack(fill="x")
        detail_body = tk.Frame(detail_frame, bg=self.COLORS["surface"])
        detail_body.pack(fill="both", expand=True, padx=13, pady=8)
        for key, label in (("name", "名称"), ("type", "类型"), ("path", "路径"), ("size", "大小"), ("rows", "记录数"), ("status", "状态"), ("sha256", "SHA-256")):
            tk.Label(detail_body, text=label, anchor="w", bg=self.COLORS["surface"], fg=self.COLORS["muted"], font=("Segoe UI", 8)).pack(fill="x", pady=(7, 1))
            variable = tk.StringVar(value="--")
            self.detail_vars[key] = variable
            tk.Label(detail_body, textvariable=variable, anchor="w", justify="left", wraplength=235, bg=self.COLORS["surface"], fg=self.COLORS["text"], font=("Consolas", 8) if key == "sha256" else ("Segoe UI", 9)).pack(fill="x")

    def _build_jobs_page(self) -> None:
        page = self._new_page("jobs")
        header = self._page_header(page, "传输任务", "下载、校验与任务恢复记录")
        ttk.Button(header, text="取消当前任务", style="Danger.TButton", command=self.cancel_task).pack(side="right", pady=4)
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

    def _build_status_page(self) -> None:
        page = self._new_page("status")
        header = self._page_header(page, "运行状态", "自动检查、本地副本和最近操作")
        actions = tk.Frame(header, bg=self.COLORS["bg"])
        actions.pack(side="right")
        ttk.Button(actions, text="检查网盘", command=lambda: self.request_scan(False)).pack(side="left", padx=4)
        ttk.Button(actions, text="立即同步", style="Primary.TButton", command=lambda: self.request_scan(True)).pack(side="left", padx=4)
        cards = tk.Frame(page, bg=self.COLORS["bg"])
        cards.pack(fill="x", pady=(0, 12))
        self.status_cards: dict[str, tk.StringVar] = {}
        for key, label in (("runtime", "运行状态"), ("verified", "已验证日期"), ("pending", "待处理"), ("disk", "本地磁盘")):
            card = tk.Frame(cards, bg=self.COLORS["surface"], highlightbackground=self.COLORS["line"], highlightthickness=1)
            card.pack(side="left", fill="x", expand=True, padx=(0, 8))
            tk.Label(card, text=label, bg=self.COLORS["surface"], fg=self.COLORS["muted"], font=("Segoe UI", 9)).pack(anchor="w", padx=14, pady=(12, 2))
            variable = tk.StringVar(value="--")
            self.status_cards[key] = variable
            tk.Label(card, textvariable=variable, bg=self.COLORS["surface"], fg=self.COLORS["text"], font=("Segoe UI Semibold", 18)).pack(anchor="w", padx=14, pady=(0, 12))
        surface = tk.Frame(page, bg=self.COLORS["surface"], highlightbackground=self.COLORS["line"], highlightthickness=1)
        surface.pack(fill="both", expand=True)
        columns = ("profile", "status", "objects", "data", "updated")
        self.days_tree = ttk.Treeview(surface, columns=columns, show="tree headings")
        self.days_tree.heading("#0", text="归档日期")
        for key, label in (("profile", "采集服务器"), ("status", "状态"), ("objects", "对象"), ("data", "数据量"), ("updated", "更新时间")):
            self.days_tree.heading(key, text=label)
        self.days_tree.column("#0", width=130)
        self.days_tree.column("profile", width=180)
        self.days_tree.column("status", width=120)
        self.days_tree.column("objects", width=100)
        self.days_tree.column("data", width=180)
        self.days_tree.column("updated", width=170)
        scroll = ttk.Scrollbar(surface, orient="vertical", command=self.days_tree.yview)
        self.days_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.days_tree.pack(fill="both", expand=True)

    def _build_settings_page(self) -> None:
        page = self._new_page("settings")
        header = self._page_header(page, "设置", "本地存储、资源限制和采集服务器来源")
        ttk.Button(header, text="保存设置", style="Primary.TButton", command=self.save_settings).pack(side="right", pady=4)
        body = tk.Frame(page, bg=self.COLORS["bg"])
        body.pack(fill="both", expand=True)
        general = tk.Frame(body, bg=self.COLORS["surface"], highlightbackground=self.COLORS["line"], highlightthickness=1)
        general.pack(fill="x", pady=(0, 12))
        tk.Label(general, text="运行策略", bg=self.COLORS["surface"], fg=self.COLORS["text"], font=("Segoe UI Semibold", 10)).grid(row=0, column=0, columnspan=4, sticky="w", padx=14, pady=(12, 8))
        fields = (
            ("local_root", "本地归档目录"),
            ("poll_minutes", "检查间隔（分钟）"),
            ("history_days", "扫描历史（天）"),
            ("download_workers", "下载并发"),
            ("bandwidth_limit", "全局带宽限制"),
            ("minimum_free_gib", "磁盘保留（GiB）"),
            ("rclone_binary", "rclone 路径"),
        )
        for index, (key, label) in enumerate(fields):
            row = 1 + index // 2
            column = (index % 2) * 2
            tk.Label(general, text=label, bg=self.COLORS["surface"], fg=self.COLORS["muted"], font=("Segoe UI", 8)).grid(row=row, column=column, sticky="w", padx=(14, 6), pady=8)
            variable = tk.StringVar()
            self.setting_vars[key] = variable
            entry = ttk.Entry(general, textvariable=variable, width=35)
            entry.grid(row=row, column=column + 1, sticky="ew", padx=(0, 14), pady=8)
            if key == "local_root":
                entry.grid_configure(padx=(0, 48))
                ttk.Button(general, text="…", width=3, command=self.choose_archive_root).grid(row=row, column=column + 1, sticky="e", padx=(0, 14), pady=8)
        for column in (1, 3):
            general.grid_columnconfigure(column, weight=1)
        auto = tk.BooleanVar()
        self.setting_vars["auto_download"] = auto
        ttk.Checkbutton(general, text="自动检查并下载", variable=auto).grid(row=5, column=0, columnspan=2, sticky="w", padx=14, pady=(8, 14))
        profiles = tk.Frame(body, bg=self.COLORS["surface"], highlightbackground=self.COLORS["line"], highlightthickness=1)
        profiles.pack(fill="both", expand=True)
        profile_header = tk.Frame(profiles, bg=self.COLORS["surface"])
        profile_header.pack(fill="x", padx=12, pady=10)
        tk.Label(profile_header, text="采集服务器", bg=self.COLORS["surface"], fg=self.COLORS["text"], font=("Segoe UI Semibold", 10)).pack(side="left")
        ttk.Button(profile_header, text="添加", command=lambda: self.edit_profile(None)).pack(side="right", padx=3)
        ttk.Button(profile_header, text="编辑", command=self.edit_selected_profile).pack(side="right", padx=3)
        ttk.Button(profile_header, text="删除", style="Danger.TButton", command=self.delete_selected_profile).pack(side="right", padx=3)
        columns = ("collector", "source", "enabled")
        self.profiles_tree = ttk.Treeview(profiles, columns=columns, show="tree headings", selectmode="browse")
        self.profiles_tree.heading("#0", text="名称 / 配置 ID")
        self.profiles_tree.heading("collector", text="Collector ID")
        self.profiles_tree.heading("source", text="来源")
        self.profiles_tree.heading("enabled", text="状态")
        self.profiles_tree.column("#0", width=260)
        self.profiles_tree.column("collector", width=220)
        self.profiles_tree.column("source", width=180)
        self.profiles_tree.column("enabled", width=100)
        self.profiles_tree.pack(fill="both", expand=True)
        self.profiles_tree.bind("<Double-1>", lambda _event: self.edit_selected_profile())

    def _build_transfer_bar(self) -> None:
        bar = tk.Frame(self.root, height=52, bg=self.COLORS["surface"], highlightbackground=self.COLORS["line"], highlightthickness=1)
        bar.grid(row=1, column=1, sticky="ew")
        bar.grid_propagate(False)
        self.transfer_title = tk.Label(bar, text="传输空闲", anchor="w", bg=self.COLORS["surface"], fg=self.COLORS["text"], font=("Segoe UI Semibold", 9))
        self.transfer_title.pack(side="left", padx=(14, 10))
        self.transfer_detail = tk.Label(bar, text="等待自动检查", anchor="w", bg=self.COLORS["surface"], fg=self.COLORS["muted"], font=("Segoe UI", 8), width=34)
        self.transfer_detail.pack(side="left")
        self.transfer_progress = ttk.Progressbar(bar, orient="horizontal", mode="determinate", maximum=100, length=280)
        self.transfer_progress.pack(side="left", fill="x", expand=True, padx=12)
        self.transfer_stats = tk.Label(bar, text="速度 -- · 剩余 --", bg=self.COLORS["surface"], fg=self.COLORS["muted"], font=("Segoe UI", 8))
        self.transfer_stats.pack(side="left", padx=10)
        ttk.Button(bar, text="任务", command=lambda: self.show_page("jobs")).pack(side="right", padx=(0, 12), pady=7)

    def show_page(self, key: str) -> None:
        self.active_page = key
        self.pages[key].tkraise()
        for page, button in self.nav_buttons.items():
            active = page == key
            button.configure(
                bg=self.COLORS["brand_soft"] if active else self.COLORS["sidebar"],
                fg=self.COLORS["brand_dark"] if active else self.COLORS["sidebar_muted"],
            )
        if key == "jobs":
            self._render_jobs()
        elif key == "status":
            self._render_days()
        elif key == "settings":
            self._load_settings()

    def set_scope(self, scope: str) -> None:
        self.active_scope = scope
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

    def _profile_choices(self) -> list[ProfileConfig]:
        return [profile for profile in self.store.load().profiles if profile.enabled]

    def _refresh_profile_choices(self) -> None:
        profiles = self._profile_choices()
        labels = [profile.display_name for profile in profiles]
        self.profile_combo["values"] = labels
        state = self.browser[self.active_scope]
        selected = next((profile for profile in profiles if profile.profile_id == state["profile_id"]), None)
        if selected is None and profiles:
            selected = profiles[0]
            state["profile_id"] = selected.profile_id
        self.profile_var.set(selected.display_name if selected else "")
        self.profile_combo.configure(state="readonly" if profiles else "disabled")
        self.refresh_button.configure(state="normal" if profiles else "disabled")

    def _selected_profile(self) -> ProfileConfig | None:
        name = self.profile_var.get()
        return next((item for item in self._profile_choices() if item.display_name == name), None)

    def _profile_changed(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            return
        self.browser[self.active_scope] = self._new_browser_state()
        self.browser[self.active_scope]["profile_id"] = profile.profile_id
        self.refresh_dates(force=True)

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
                    self.refresh_button.configure(
                        state="normal" if self._selected_profile() else "disabled"
                    )
                if status == "error":
                    if channel_is_visible:
                        self.file_notice.configure(text=str(result), fg=self.COLORS["bad"])
                        messagebox.showerror("操作失败", str(result), parent=self.root)
                elif callback is not None:
                    callback(result)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_results)

    def refresh_dates(self, *, force: bool = False) -> None:
        profile = self._selected_profile()
        if profile is None:
            self._show_empty_browser("请先在设置中添加采集服务器")
            return
        state = self.browser[self.active_scope]
        if not force and state["profile_id"] == profile.profile_id and state["dates"]:
            self._render_browser_state()
            return
        scope = self.active_scope
        state["profile_id"] = profile.profile_id

        def operation() -> dict[str, Any]:
            manager = ArchiveManager(self.store.load(), self.database)
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
            manager = ArchiveManager(self.store.load(), self.database)
            return manager.browse_files(profile_id, archive_date, scope=scope, path=path)

        self._run_async(
            f"browser:{scope}",
            f"正在读取 {archive_date}…",
            operation,
            lambda result: self._directory_loaded(scope, result),
        )

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
        if not state["archive_date"]:
            return
        if state["path"]:
            self._load_directory("/".join(state["path"].split("/")[:-1]))
            return
        state["archive_date"] = ""
        state["path"] = ""
        state["result"] = {}
        self._render_browser_state()

    def _render_browser_state(self) -> None:
        state = self.browser[self.active_scope]
        self.search_var.set("")
        self.path_var.set(
            " / ".join(part for part in ("归档根目录", state["archive_date"], state["path"]) if part)
        )
        self.up_button.configure(state="normal" if state["archive_date"] else "disabled")
        self._render_directory_tree()
        self._render_current_list()
        self._render_detail(None)
        self._update_download_action()

    def _render_directory_tree(self) -> None:
        tree = self.directory_tree
        self.updating_directory_tree = True
        try:
            tree.delete(*tree.get_children())
            self.tree_actions.clear()
            state = self.browser[self.active_scope]
            root_id = tree.insert("", "end", text="归档文件", open=True)
            self.tree_actions[root_id] = ("root", "")
            if not state["archive_date"]:
                for item in state["dates"]:
                    archive_date = str(item.get("archive_date") or "")
                    item_id = tree.insert(root_id, "end", text=f"▣  {archive_date}")
                    self.tree_actions[item_id] = ("date", archive_date)
                tree.item(root_id, open=True)
                return
            date_id = tree.insert(root_id, "end", text=f"▣  {state['archive_date']}", open=True)
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
            if not state["archive_date"]:
                return
            state["archive_date"] = ""
            state["path"] = ""
            state["result"] = {}
            self._render_browser_state()
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
        if not state["archive_date"]:
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
            if item_type == "date":
                values = (
                    "归档日期",
                    bytes_text(item.get("bytes_total")),
                    f"{int(item.get('row_count') or 0):,}",
                    STATUS_LABELS.get(str(item.get("status") or "unknown"), str(item.get("status") or "未知")),
                    "本地存在" if item.get("local") else "未下载",
                )
            elif item_type == "directory":
                values = ("文件夹", "--", "--", "--", f"{int(item.get('entry_count') or 0)} 个对象")
            elif self.active_scope == "remote":
                values = (
                    " · ".join(filter(None, (str(item.get("kind") or ""), str(item.get("table_name") or "")))) or "归档对象",
                    bytes_text(item.get("size_bytes")),
                    f"{int(item.get('row_count') or 0):,}",
                    {"present": "本地存在", "staged": "已暂存", "downloading": "下载中", "missing": "待下载", "mismatch": "大小异常"}.get(str(item.get("local_state") or ""), "未知"),
                    str(item.get("sha256") or "")[:12],
                )
            else:
                values = (
                    "本地文件",
                    bytes_text(item.get("size_bytes")),
                    "--",
                    "下载中" if item.get("state") == "downloading" else "已验证目录" if item.get("location") == "verified" else "暂存目录",
                    "已列入清单" if item.get("remote_state") == "listed" else "控制文件" if item.get("remote_state") == "control" else "仅本地",
                )
            icon = {"date": "▣", "directory": "▸"}.get(item_type, "▤")
            item_id = self.file_list.insert(
                "", "end", text=f"{icon}  {str(item.get('name') or '')}", values=values
            )
            self.list_entries[item_id] = item
        result = state.get("result") or {}
        suffix = f" · 搜索结果 {len(entries)} 项" if self.search_var.get().strip() else ""
        self.file_summary.configure(
            text=f"{len(entries)} 项 · {bytes_text(result.get('bytes_total'))} · {int(result.get('row_count') or 0):,} 行{suffix}"
        )
        if not state["archive_date"]:
            self.file_notice.configure(text=f"{len(state['dates'])} 个归档日期", fg=self.COLORS["muted"])
        else:
            detail = str(result.get("detail") or "")
            self.file_notice.configure(text=detail or "目录已读取", fg=self.COLORS["muted"])

    def _list_open(self, _event: tk.Event[Any]) -> None:
        selection = self.file_list.selection()
        if not selection:
            return
        item = self.list_entries.get(selection[0]) or {}
        if item.get("type") == "date":
            self.open_date(str(item.get("archive_date") or ""))
        elif item.get("type") == "directory":
            self._load_directory(str(item.get("path") or ""))

    def _list_selected(self, _event: tk.Event[Any]) -> None:
        selection = self.file_list.selection()
        self._render_detail(self.list_entries.get(selection[0]) if selection else None)

    def _render_detail(self, item: dict[str, Any] | None) -> None:
        state = self.browser[self.active_scope]
        result = state.get("result") or {}
        if item is None:
            values = {
                "name": state["path"].split("/")[-1] if state["path"] else state["archive_date"] or "全部归档",
                "type": "文件夹" if state["archive_date"] else "归档根目录",
                "path": state["path"] or "归档根目录",
                "size": bytes_text(result.get("bytes_total")),
                "rows": f"{int(result.get('row_count') or 0):,}",
                "status": STATUS_LABELS.get(str(result.get("state") or "unknown"), str(result.get("state") or "未知")),
                "sha256": str(result.get("manifest_sha256") or "--"),
            }
        else:
            values = {
                "name": str(item.get("name") or "--"),
                "type": "文件夹" if item.get("type") == "directory" else " · ".join(filter(None, (str(item.get("kind") or ""), str(item.get("table_name") or "")))) or "文件",
                "path": str(item.get("path") or "--"),
                "size": "--" if item.get("type") == "directory" else bytes_text(item.get("size_bytes")),
                "rows": f"{int(item.get('row_count') or 0):,}" if item.get("row_count") is not None else "--",
                "status": str(item.get("local_state") or item.get("state") or item.get("location") or "--"),
                "sha256": str(item.get("sha256") or "--"),
            }
        for key, variable in self.detail_vars.items():
            variable.set(values.get(key, "--"))

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
            label = {"scan": "检查网盘", "scan_download": "自动同步", "download": "指定日期下载", "verify": "重新校验"}.get(str(active.get("action") or ""), "后台任务")
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
        days = self.database.days(5000)
        verified = sum(item.get("status") == "verified" for item in days)
        failed = sum(item.get("status") in {"error", "remote_failed", "manifest_changed"} for item in days)
        self.status_cards["runtime"].set("执行中" if active else "自动运行" if runtime.get("auto_download") else "空闲")
        self.status_cards["verified"].set(str(verified))
        self.status_cards["pending"].set(str(failed))
        self.status_cards["disk"].set(f"{ratio}%")
        if self.active_page == "jobs":
            self._render_jobs()
        elif self.active_page == "status":
            self._render_days()
        if self.active_page == "files":
            self._update_download_action()
        self.root.after(1000, self._refresh_runtime)

    def _render_jobs(self) -> None:
        if not hasattr(self, "jobs_tree"):
            return
        self.jobs_tree.delete(*self.jobs_tree.get_children())
        profiles = {item.profile_id: item.display_name for item in self.store.load().profiles}
        for job in self.database.jobs(200):
            action = {"scan": "检查网盘", "scan_download": "自动同步", "download": "指定日期下载", "verify": "重新校验"}.get(str(job.get("action") or ""), str(job.get("action") or "任务"))
            location = " · ".join(filter(None, (profiles.get(str(job.get("profile_id") or ""), str(job.get("profile_id") or "")), str(job.get("archive_date") or "")))) or "全部采集服务器"
            values = (
                location,
                {"queued": "排队中", "running": "执行中", "cancelling": "正在取消", "completed": "已完成", "cancelled": "已取消", "failed": "失败"}.get(str(job.get("status") or ""), str(job.get("status") or "")),
                f"{int(job.get('objects_done') or 0)}/{int(job.get('object_count') or 0)}",
                f"{bytes_text(job.get('bytes_done'))} / {bytes_text(job.get('bytes_total'))}",
                str(job.get("updated_at") or ""),
            )
            self.jobs_tree.insert("", "end", text=action, values=values)

    def _render_days(self) -> None:
        if not hasattr(self, "days_tree"):
            return
        self.days_tree.delete(*self.days_tree.get_children())
        profiles = {item.profile_id: item.display_name for item in self.store.load().profiles}
        for item in self.database.days(1000):
            values = (
                profiles.get(str(item.get("profile_id") or ""), str(item.get("profile_id") or "")),
                STATUS_LABELS.get(str(item.get("status") or "unknown"), str(item.get("status") or "未知")),
                f"{int(item.get('objects_done') or 0)}/{int(item.get('object_count') or 0)}",
                f"{bytes_text(item.get('bytes_done'))} / {bytes_text(item.get('bytes_total'))}",
                str(item.get("updated_at") or ""),
            )
            self.days_tree.insert("", "end", text=str(item.get("archive_date") or ""), values=values)

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
            "rclone_binary": config.rclone_binary,
            "auto_download": config.auto_download,
        }
        for key, value in values.items():
            self.setting_vars[key].set(value)
        self.profile_drafts = list(config.profiles)
        self._render_profile_drafts()

    def _render_profile_drafts(self) -> None:
        if not hasattr(self, "profiles_tree"):
            return
        self.profiles_tree.delete(*self.profiles_tree.get_children())
        for index, profile in enumerate(self.profile_drafts):
            self.profiles_tree.insert(
                "",
                "end",
                iid=str(index),
                text=f"{profile.display_name} · {profile.profile_id}",
                values=(profile.collector_id, SOURCE_LABELS.get(profile.source_type, profile.source_type), "已启用" if profile.enabled else "已停用"),
            )

    def choose_archive_root(self) -> None:
        selected = filedialog.askdirectory(parent=self.root, initialdir=self.setting_vars["local_root"].get() or str(Path.home()))
        if selected:
            self.setting_vars["local_root"].set(selected)

    def edit_selected_profile(self) -> None:
        selection = self.profiles_tree.selection()
        if not selection:
            messagebox.showinfo("采集服务器", "请先选择一个采集服务器。", parent=self.root)
            return
        self.edit_profile(int(selection[0]))

    def delete_selected_profile(self) -> None:
        selection = self.profiles_tree.selection()
        if not selection:
            return
        index = int(selection[0])
        profile = self.profile_drafts[index]
        if not messagebox.askyesno("删除采集服务器", f"确定删除 {profile.display_name}？", parent=self.root):
            return
        self.profile_drafts.pop(index)
        self._render_profile_drafts()

    def edit_profile(self, index: int | None) -> None:
        if self.profile_dialog is not None and self.profile_dialog.winfo_exists():
            self.profile_dialog.focus_set()
            return
        profile = self.profile_drafts[index] if index is not None else ProfileConfig(
            profile_id="collector-new",
            display_name="新采集服务器",
            collector_id="collector-new",
        )
        dialog = tk.Toplevel(self.root)
        self.profile_dialog = dialog
        dialog.title("编辑采集服务器" if index is not None else "添加采集服务器")
        dialog.geometry("680x650")
        dialog.minsize(620, 560)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.configure(bg=self.COLORS["bg"])
        container = tk.Frame(dialog, bg=self.COLORS["surface"], padx=18, pady=16)
        container.pack(fill="both", expand=True, padx=14, pady=14)
        values = asdict(profile)
        variables: dict[str, tk.Variable] = {}
        fields = (
            ("profile_id", "配置 ID"), ("display_name", "显示名称"), ("collector_id", "Collector ID"),
            ("drive_remote", "rclone remote"), ("drive_prefix", "Google Drive 前缀"),
            ("sftp_host", "Ubuntu 主机"), ("sftp_port", "SSH 端口"), ("sftp_user", "只读用户"),
            ("sftp_key_file", "SSH 私钥文件"), ("sftp_known_hosts_file", "known_hosts 文件"),
            ("sftp_root", "SFTP 根目录"), ("verified_source_root", "已验证目录来源"),
        )
        source_var = tk.StringVar(value=SOURCE_LABELS.get(profile.source_type, "Google Drive"))
        variables["source_type"] = source_var
        row = 0
        for key, label in fields[:3]:
            tk.Label(container, text=label, bg=self.COLORS["surface"], fg=self.COLORS["muted"], font=("Segoe UI", 8)).grid(row=row, column=0, sticky="w", pady=5)
            variable = tk.StringVar(value=str(values.get(key) or ""))
            variables[key] = variable
            ttk.Entry(container, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=5)
            row += 1
        tk.Label(container, text="来源", bg=self.COLORS["surface"], fg=self.COLORS["muted"], font=("Segoe UI", 8)).grid(row=row, column=0, sticky="w", pady=5)
        source_combo = ttk.Combobox(container, textvariable=source_var, values=list(SOURCE_VALUES), state="readonly")
        source_combo.grid(row=row, column=1, sticky="ew", pady=5)
        row += 1
        field_rows: dict[str, tuple[tk.Widget, tk.Widget]] = {}
        browse_fields = {"sftp_key_file": "file", "sftp_known_hosts_file": "file", "verified_source_root": "directory"}
        for key, label in fields[3:]:
            label_widget = tk.Label(container, text=label, bg=self.COLORS["surface"], fg=self.COLORS["muted"], font=("Segoe UI", 8))
            label_widget.grid(row=row, column=0, sticky="w", pady=5)
            variable = tk.StringVar(value=str(values.get(key) or ""))
            variables[key] = variable
            field = tk.Frame(container, bg=self.COLORS["surface"])
            field.grid(row=row, column=1, sticky="ew", pady=5)
            field.grid_columnconfigure(0, weight=1)
            ttk.Entry(field, textvariable=variable).grid(row=0, column=0, sticky="ew")
            if key in browse_fields:
                def browse(target=variable, mode=browse_fields[key]) -> None:
                    selected = filedialog.askdirectory(parent=dialog) if mode == "directory" else filedialog.askopenfilename(parent=dialog)
                    if selected:
                        target.set(selected)
                ttk.Button(field, text="…", width=3, command=browse).grid(row=0, column=1, padx=(5, 0))
            field_rows[key] = (label_widget, field)
            row += 1
        enabled = tk.BooleanVar(value=profile.enabled)
        variables["enabled"] = enabled
        ttk.Checkbutton(container, text="启用采集服务器", variable=enabled).grid(row=row, column=1, sticky="w", pady=8)
        row += 1
        error_var = tk.StringVar()
        tk.Label(container, textvariable=error_var, bg=self.COLORS["surface"], fg=self.COLORS["bad"], font=("Segoe UI", 8), wraplength=520, justify="left").grid(row=row, column=0, columnspan=2, sticky="w", pady=5)
        row += 1
        buttons = tk.Frame(container, bg=self.COLORS["surface"])
        buttons.grid(row=row, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right", padx=4)

        def update_fields(*_args: Any) -> None:
            source = SOURCE_VALUES.get(source_var.get(), "google_drive")
            groups = {
                "google_drive": {"drive_remote", "drive_prefix"},
                "ubuntu_sftp": {"sftp_host", "sftp_port", "sftp_user", "sftp_key_file", "sftp_known_hosts_file", "sftp_root"},
                "verified_directory": {"verified_source_root"},
            }
            visible = groups[source]
            for key, (label_widget, field) in field_rows.items():
                if key in visible:
                    label_widget.grid()
                    field.grid()
                else:
                    label_widget.grid_remove()
                    field.grid_remove()

        def save_profile() -> None:
            try:
                candidate = ProfileConfig.from_mapping({
                    key: variable.get() for key, variable in variables.items()
                } | {"source_type": SOURCE_VALUES.get(source_var.get(), "google_drive")})
                errors = candidate.validate()
                if errors:
                    raise ValueError("；".join(errors))
                if index is None:
                    self.profile_drafts.append(candidate)
                else:
                    self.profile_drafts[index] = candidate
                self._render_profile_drafts()
                dialog.destroy()
            except (ValueError, TypeError) as exc:
                error_var.set(str(exc))

        ttk.Button(buttons, text="确定", style="Primary.TButton", command=save_profile).pack(side="right", padx=4)
        source_var.trace_add("write", update_fields)
        update_fields()
        container.grid_columnconfigure(1, weight=1)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.bind("<Destroy>", lambda _event: setattr(self, "profile_dialog", None))

    def save_settings(self) -> None:
        current = self.store.load()
        try:
            config = ClientConfig(
                config_version=CONFIG_VERSION,
                local_root=str(self.setting_vars["local_root"].get()).strip(),
                rclone_binary=str(self.setting_vars["rclone_binary"].get()).strip(),
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
                profiles=list(self.profile_drafts),
            )
            self.store.save(config)
            self.service.wake()
            self.browser = {"remote": self._new_browser_state(), "local": self._new_browser_state()}
            self._refresh_profile_choices()
            messagebox.showinfo("设置", "设置已保存。", parent=self.root)
        except (ValueError, TypeError) as exc:
            messagebox.showerror("设置无效", str(exc), parent=self.root)

    def _show_empty_browser(self, message: str) -> None:
        self.file_notice.configure(text=message, fg=self.COLORS["warn"])
        self.directory_tree.delete(*self.directory_tree.get_children())
        self.file_list.delete(*self.file_list.get_children())
        self.file_summary.configure(text=message)
        self.download_button.configure(state="disabled")

    def open_state_directory(self) -> None:
        if os.name == "nt":
            os.startfile(self.store.root)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(self.store.root)])

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
    store = ConfigStore()
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
    finally:
        lock.release()


if __name__ == "__main__":
    main()
