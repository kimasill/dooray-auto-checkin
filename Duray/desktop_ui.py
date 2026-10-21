"""Tk desktop UI. All widget access stays on the main thread."""
from __future__ import annotations

import calendar
import copy
import datetime as dt
import logging
import queue
import threading
import tkinter as tk
import types
import uuid
from tkinter import messagebox, ttk

import attendance_rules as rules
import attendance_runtime as runtime
import dooray_checkin as core
import scheduler
import updater
from app_version import VERSION
from ui_widgets import TimePicker, MonthCanvas, CheckButton, day_display, LEAVE_COLORS

BG, CARD, INK, MUTED = "#F7F8FB", "#FFFFFF", "#242A35", "#7A8495"
NAV, ACCENT, SOFT, LINE = "#F1F3F7", "#2764E7", "#EAF1FF", "#E8EBF0"
TITLE = "두레이 자동 출퇴근"
MODE_LABELS = {"fixed": "고정 시각", "random": "랜덤 범위"}
POLICY_LABELS = {"checkout_time": "출근은 기본 시각 / 반차별 퇴근 시각 적용",
                 "keep_time": "출근·퇴근 모두 기본 시각 유지", "skip_checkout": "출근은 기본 시각 / 반차일 자동 퇴근 OFF"}
OVERRIDE_LABELS = {"auto": "기본 규칙", "on": "켜기", "off": "끄기"}


class LogHandler(logging.Handler):
    def __init__(self, events):
        super().__init__()
        self.events = events
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record):
        self.events.put(("log", self.format(record)))


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = core.load_config(validate=False)
        self.cache = core.load_calendar(self.cfg)
        self.events = queue.Queue()
        self.busy = False
        self.status_pending = False
        self.task = {}
        self.buttons = []
        self.time_pickers = {}
        self.random_inputs = {}
        self.schedule_previews = {}
        self.update_release = None
        self.display_plans = tk.BooleanVar(value=True)
        self.pages = {}
        self.nav_buttons = {}
        self.vars = {}
        self.today = rules.now_kst().date()
        self.selected = self.today
        self.month = self.today.replace(day=1)
        self.overrides = copy.deepcopy(self.cfg["date_overrides"])
        root.title(TITLE + "  ·  " + VERSION)
        root.geometry("1380x920")
        root.minsize(1180, 800)
        root.configure(bg=BG)
        root.option_add("*Font", ("Malgun Gothic", 10))
        self.configure_style()
        for key, val in self.cfg.items():
            if isinstance(val, bool):
                self.vars[key] = tk.BooleanVar(value=val)
            elif isinstance(val, (str, int)):
                self.vars[key] = tk.StringVar(value=str(val))
        self.vars["password"] = tk.StringVar()
        self.vars["time_mode"].set(MODE_LABELS[self.cfg["time_mode"]])
        self.vars["checkout_time_mode"].set(MODE_LABELS[self.cfg["checkout_time_mode"]])
        self.vars["half_day_policy"].set(POLICY_LABELS[self.cfg["half_day_policy"]])
        self.workdays = [tk.BooleanVar(value=i in self.cfg["workdays"]) for i in range(7)]
        self.skipdates = tk.StringVar(value=", ".join(self.cfg.get("skip_dates", [])))
        self.selector_vars = {key: tk.StringVar(value=self.cfg["selectors"].get(key) or "")
                              for key in ("checkin_button", "checkout_button")}

        side = tk.Frame(root, bg=NAV, width=184)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Label(side, text="Dooray!", font=("Segoe UI", 26, "bold"), bg=NAV, fg=ACCENT).pack(anchor="w", padx=25, pady=(30, 4))
        tk.Label(side, text="자동 출퇴근", bg=NAV, fg=INK, font=("Malgun Gothic", 12, "bold")).pack(anchor="w", padx=25)
        tk.Label(side, text="MY WORK CALENDAR", bg=NAV, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=25, pady=(6, 34))
        for key, label in (("home", "대시보드"), ("calendar", "근무 달력"), ("schedule", "시간 · 예약"), ("settings", "연결 · 설정"), ("logs", "실행 기록")):
            button = tk.Button(side, text=label, anchor="w", bg=NAV, fg=MUTED, relief="flat", bd=0,
                               activebackground=SOFT, activeforeground=ACCENT, padx=24, pady=13,
                               cursor="hand2", command=lambda k=key: self.show(k))
            button.pack(fill="x", padx=10, pady=3)
            self.nav_buttons[key] = button
        tk.Label(side, text=f"개인 자동화 도구\n버전 {VERSION} · 한국 시간", justify="left", bg=NAV, fg=MUTED, font=("Malgun Gothic", 9)).pack(side="bottom", anchor="w", padx=25, pady=25)
        main = tk.Frame(root, bg=BG)
        main.pack(side="left", fill="both", expand=True)
        header = tk.Frame(main, bg=CARD, height=66)
        header.pack(fill="x")
        header.pack_propagate(False)
        self.page_title = tk.Label(header, text="대시보드", bg=CARD, fg=INK, font=("Malgun Gothic", 18, "bold"))
        self.page_title.pack(side="left", padx=28)
        self.badge = tk.Label(header, text="예약 상태 확인 중", bg=BG, fg=MUTED, padx=14, pady=8)
        self.badge.pack(side="right", padx=25)
        footer = tk.Frame(main, bg=CARD, height=58)
        footer.pack(side="bottom", fill="x")
        self.feedback = tk.Label(footer, text="설정을 바꾼 뒤 저장하세요.", bg=CARD, fg=MUTED)
        self.feedback.pack(side="left", padx=25, pady=16)
        self.button(footer, "저장하고 자동 예약 시작", self.start_schedule, primary=True).pack(side="right", padx=(8, 20), pady=10)
        self.button(footer, "설정만 저장", self.save).pack(side="right", pady=10)
        self.content = tk.Frame(main, bg=BG)
        self.content.pack(fill="both", expand=True, padx=22, pady=20)
        for key in self.nav_buttons:
            frame = tk.Frame(self.content, bg=BG)
            self.pages[key] = frame
        self.build_home()
        self.build_calendar()
        self.build_schedule()
        self.build_settings()
        self.build_logs()
        core.setup_logging()
        self.log_handler = LogHandler(self.events)
        core.log.addHandler(self.log_handler)
        core.DUMP_SINK = lambda *parts: self.events.put(("log", " ".join(map(str, parts))))
        core.INSPECT_PAUSE = False
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Control-s>", lambda event: self.save())
        self.show("calendar")
        self.render_calendar()
        self.refresh_summary()
        self.refresh_status()
        self.drain()
        self.periodic()
        self.auto_sync_timer = root.after(1200, self.auto_sync)

    def configure_style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=CARD)
        style.configure("TLabel", background=CARD, foreground=INK)
        style.configure("TButton", padding=(12, 8), background="#EDF1F7", foreground=INK, borderwidth=0, font=("Malgun Gothic", 10))
        style.map("TButton", background=[("active", "#DFE9F2")])
        style.configure("Accent.TButton", background=ACCENT, foreground="white", font=("Malgun Gothic", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#1B50C0"), ("disabled", "#B7C8EB")])
        style.configure("TEntry", padding=7, fieldbackground="white", bordercolor=LINE)
        style.configure("TCombobox", padding=6, fieldbackground="white", background="white", bordercolor="#DCE2EC", lightcolor="white", darkcolor="white", arrowcolor=MUTED, arrowsize=12)
        style.map("TCombobox", fieldbackground=[("readonly", "white")], foreground=[("readonly", INK)])
        style.configure("Time.TCombobox", padding=(5, 7), font=("Segoe UI", 11))
        style.configure("TCheckbutton", background=CARD, foreground=INK, padding=(0, 5))
        style.map("TCheckbutton", background=[("active", CARD)])
        style.configure("TRadiobutton", background=CARD, foreground=INK, padding=(0, 5))
        style.map("TRadiobutton", background=[("active", CARD)])

    def label(self, parent, text, *, size=10, bold=False, color=INK, **kw):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color,
                        font=("Malgun Gothic", size, "bold" if bold else "normal"), **kw)

    def card(self, parent, title=None):
        frame = tk.Frame(parent, bg=CARD, highlightbackground=LINE, highlightthickness=1, padx=20, pady=17)
        if title:
            self.label(frame, title, size=12, bold=True).pack(anchor="w", pady=(0, 12))
        return frame

    def button(self, parent, text, command, primary=False):
        button = ttk.Button(parent, text=text, command=command, style="Accent.TButton" if primary else "TButton")
        if text in ("‹", "›"):
            button.configure(width=2)
        self.buttons.append(button)
        return button

    def field(self, parent, label, key, width=None, secret=False):
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=4)
        self.label(row, label, width=17, anchor="w").pack(side="left")
        entry = ttk.Entry(row, textvariable=self.vars[key], show="●" if secret else "", width=width or 22)
        entry.pack(side="left", fill="x", expand=width is None)
        return entry

    def scroll_page(self, key):
        outer = self.pages[key]
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        scroll.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)
        canvas.configure(yscrollcommand=scroll.set)
        inner = tk.Frame(canvas, bg=BG)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        # Restrict wheel handling to the active page; child entries keep keyboard navigation.
        def wheel(event):
            if self.current == key and not isinstance(event.widget, (ttk.Combobox, ttk.Spinbox)) and canvas.winfo_containing(event.x_root, event.y_root) is not None:
                canvas.yview_scroll(int(-event.delta / 120), "units")
        self.root.bind("<MouseWheel>", wheel, add="+")
        return inner

    def show(self, key):
        self.current = key
        for name, frame in self.pages.items():
            frame.pack_forget()
            self.nav_buttons[name].configure(bg="#E1EAFE" if name == key else NAV, fg=ACCENT if name == key else MUTED)
        self.pages[key].pack(fill="both", expand=True)
        self.page_title.configure(text=self.nav_buttons[key].cget("text"))

    def build_home(self):
        page = self.scroll_page("home")
        self.label(page, "오늘의 출퇴근", size=24, bold=True).pack(anchor="w")
        self.date_label = self.label(page, "", color=MUTED)
        self.date_label.pack(anchor="w", pady=(6, 20))
        cards = tk.Frame(page, bg=BG)
        cards.pack(fill="x")
        cards.columnconfigure((0, 1), weight=1, uniform="summary")
        self.summary = {}
        for index, action in enumerate(rules.ACTIONS):
            box = self.card(cards)
            box.grid(row=0, column=index, sticky="nsew", padx=(0, 10) if index == 0 else (10, 0))
            self.label(box, "출근 예약" if action == "checkin" else "퇴근 예약", color=MUTED).pack(anchor="w")
            value = self.label(box, "—", size=26, bold=True, color=ACCENT)
            value.pack(anchor="w", pady=(10, 4))
            detail = self.label(box, "", color=MUTED, anchor="w", justify="left", wraplength=360)
            detail.pack(anchor="w")
            self.summary[action] = (value, detail)
        control = self.card(page, "자동 실행")
        control.pack(fill="x", pady=18)
        self.task_text = self.label(control, "예약 정보를 확인하고 있습니다.", color=MUTED, anchor="w", justify="left", wraplength=760)
        self.task_text.pack(anchor="w")
        row = tk.Frame(control, bg=CARD)
        row.pack(fill="x", pady=(15, 0))
        self.toggle = self.button(row, "자동 예약 켜기", self.toggle_schedule, primary=True)
        self.toggle.pack(side="left")
        self.button(row, "시간 설정", lambda: self.show("schedule")).pack(side="left", padx=10)
        self.button(row, "근무 달력", lambda: self.show("calendar")).pack(side="left")
        manual = self.card(page, "수동 실행과 확인")
        manual.pack(fill="x")
        row = tk.Frame(manual, bg=CARD)
        row.pack(fill="x")
        for action in rules.ACTIONS:
            self.button(row, "지금 " + rules.LABELS[action], lambda a=action: self.run_action(a)).pack(side="left", padx=(0, 8))
            self.button(row, rules.LABELS[action] + " 점검 (기록 없음)", lambda a=action: self.run_action(a, dry=True)).pack(side="left", padx=(0, 15))
        self.label(manual, "점검은 로그인과 버튼 상태만 확인합니다. 실제 기록하려면 ‘지금 출근 / 지금 퇴근’을 누르세요.\n수동 실행은 확인창을 거쳐 처리하며 날짜별 OFF를 무시합니다.", color=MUTED, size=9, justify="left").pack(anchor="w", pady=(12, 0))
        self.action_status = self.label(manual, "", color=MUTED, size=10, justify="left", wraplength=820)
        self.action_status.pack(anchor="w", pady=(8, 0))
        self.home_sync = self.label(page, "", color=MUTED, anchor="w", justify="left", wraplength=850)
        self.home_sync.pack(anchor="w", pady=18)

    def build_calendar(self):
        page = self.pages["calendar"]
        toolbar = tk.Frame(page, bg=BG)
        toolbar.pack(fill="x", pady=(0, 14))
        self.month_label = self.label(toolbar, "", size=23, bold=True)
        self.month_label.pack(side="left", padx=(0, 22))
        self.button(toolbar, "‹", lambda: self.change_month(-1)).pack(side="left")
        self.button(toolbar, "›", lambda: self.change_month(1)).pack(side="left", padx=(4, 12))
        self.button(toolbar, "오늘", self.go_today).pack(side="left")
        self.button(toolbar, "↻  이번 달 근태 가져오기", self.sync, primary=True).pack(side="right")
        bar = tk.Frame(page, bg=CARD, highlightthickness=1, highlightbackground=LINE, padx=14, pady=9)
        bar.pack(fill="x", pady=(0, 14))
        self.calendar_sync = self.label(bar, "두레이 근태를 연결해 보세요.", color=MUTED, size=9)
        self.calendar_sync.pack(side="left")
        self.label(bar, "이번 달 본인 근태", color=ACCENT, size=9).pack(side="right")
        body = tk.Frame(page, bg=BG)
        body.pack(fill="both", expand=True)
        detail_outer = tk.Frame(body, bg=CARD, width=310, highlightthickness=1, highlightbackground=LINE)
        detail_outer.pack(side="right", fill="y", padx=(16, 0))
        detail_outer.pack_propagate(False)
        detail_actions = tk.Frame(detail_outer, bg=CARD, padx=16, pady=12)
        detail_actions.pack(side="bottom", fill="x")
        self.button(detail_actions, "이 날짜에 적용", self.save_day, primary=True).pack(fill="x", pady=(0, 6))
        self.button(detail_actions, "날짜별 설정 초기화", self.reset_day).pack(fill="x")
        detail_canvas = tk.Canvas(detail_outer, bg=CARD, highlightthickness=0)
        scroll = ttk.Scrollbar(detail_outer, orient="vertical", command=detail_canvas.yview)
        scroll.pack(side="right", fill="y")
        detail_canvas.pack(fill="both", expand=True)
        detail_canvas.configure(yscrollcommand=scroll.set)
        editor = tk.Frame(detail_canvas, bg=CARD, padx=16, pady=18)
        window = detail_canvas.create_window((0, 0), window=editor, anchor="nw")
        editor.bind("<Configure>", lambda e: detail_canvas.configure(scrollregion=detail_canvas.bbox("all")))
        detail_canvas.bind("<Configure>", lambda e: detail_canvas.itemconfigure(window, width=e.width))
        def wheel(event):
            inside = detail_outer.winfo_containing(event.x_root, event.y_root)
            if self.current == "calendar" and inside and str(inside).startswith(str(detail_outer)) and not isinstance(event.widget, ttk.Combobox):
                detail_canvas.yview_scroll(int(-event.delta / 120), "units")
        self.root.bind("<MouseWheel>", wheel, add="+")
        calendar_panel = tk.Frame(body, bg=BG)
        calendar_panel.pack(side="left", fill="both", expand=True)
        self.calendar_grid = MonthCanvas(calendar_panel, self.select_day)
        self.calendar_grid.pack(fill="both", expand=True)
        legend = tk.Frame(calendar_panel, bg=BG)
        legend.pack(fill="x", pady=(12, 0))
        for kind, text in (("vacation", "연차·휴가"), ("am_half", "오전 반차"), ("pm_half", "오후 반차")):
            self.label(legend, "● " + text, color=LEAVE_COLORS[kind][1], size=9).pack(side="left", padx=(0, 15))
        self.label(legend, "● 직접 설정", color=ACCENT, size=9).pack(side="left")
        options = tk.Frame(calendar_panel, bg=BG)
        options.pack(fill="x", pady=(9, 0))
        CheckButton(options, text="예약 시각 표시 (출근 · 퇴근)", variable=self.display_plans,
                        command=lambda: self.render_calendar(update_editor=False)).pack(side="left")
        self.month_counts = self.label(options, "", color=MUTED, size=9)
        self.month_counts.pack(side="right")
        self.selected_label = self.label(editor, "", size=15, bold=True)
        self.selected_label.pack(anchor="w")
        self.selected_reason = self.label(editor, "", size=9, color=MUTED, wraplength=250, justify="left")
        self.selected_reason.pack(anchor="w", pady=(6, 12))
        self.remote_detail = self.label(editor, "", color=ACCENT, size=10, wraplength=250, justify="left", anchor="w")
        self.remote_detail.pack(fill="x", pady=(0, 12))
        self.day_plan = self.label(editor, "", size=10, wraplength=250, justify="left", anchor="w")
        self.day_plan.pack(fill="x", pady=(0, 12))
        ttk.Separator(editor).pack(fill="x", pady=(0, 12))
        self.label(editor, "이 날짜의 예약 설정", size=11, bold=True).pack(anchor="w")
        self.day_vars = {key: tk.StringVar() for key in ("kind", "checkin", "checkout", "checkin_time", "checkout_time", "note")}
        self.label(editor, "날짜 유형", color=MUTED, size=9).pack(anchor="w", pady=(10, 4))
        ttk.Combobox(editor, textvariable=self.day_vars["kind"], values=list(rules.DAY_LABELS.values()),
                     state="readonly").pack(fill="x")
        row = tk.Frame(editor, bg=CARD)
        row.pack(fill="x", pady=(10, 0))
        for action in rules.ACTIONS:
            column = tk.Frame(row, bg=CARD)
            column.pack(side="left", fill="x", expand=True, padx=(0, 7) if action == "checkin" else 0)
            self.label(column, "자동 " + rules.LABELS[action], color=MUTED, size=9).pack(anchor="w", pady=(0, 4))
            ttk.Combobox(column, textvariable=self.day_vars[action], values=list(OVERRIDE_LABELS.values()), state="readonly", width=9).pack(fill="x")
        for action in rules.ACTIONS:
            self.label(editor, rules.LABELS[action] + " 시각", color=MUTED, size=9).pack(anchor="w", pady=(10, 4))
            key = action + "_time"
            self.time_pickers["day_" + key] = TimePicker(editor, self.day_vars[key], optional=True, default=self.cfg[key])
            self.time_pickers["day_" + key].pack(anchor="w")
        self.label(editor, "메모", color=MUTED, size=9).pack(anchor="w", pady=(10, 4))
        ttk.Entry(editor, textvariable=self.day_vars["note"]).pack(fill="x")
        self.label(editor, "직접 설정이 두레이 근태보다 우선합니다.\n전체 예약이 꺼져 있으면 실행되지 않습니다.", size=9, color=MUTED, justify="left", wraplength=250).pack(anchor="w", pady=(10, 0))
        bottom = tk.Frame(page, bg=BG)
        bottom.pack(fill="x", pady=(12, 0))
        CheckButton(bottom, text="두레이 근태를 자동 예약에도 반영", variable=self.vars["dooray_sync_enabled"]).pack(side="left")
        self.label(bottom, "변경 후 설정 저장", size=9, color=MUTED).pack(side="left", padx=12)

    def build_schedule(self):
        page = self.scroll_page("schedule")
        self.schedule_status = self.label(page, "자동 예약 상태 확인 중", color=MUTED, justify="left", wraplength=850)
        self.schedule_status.pack(anchor="w", pady=(0, 12))
        for action in rules.ACTIONS:
            title = rules.LABELS[action]
            box = self.card(page, title + " 예약")
            box.pack(fill="x", pady=(0, 14))
            CheckButton(box, text=title + "을 자동 예약에 포함", variable=self.vars[action + "_enabled"]).pack(anchor="w")
            row = tk.Frame(box, bg=CARD)
            row.pack(fill="x", pady=6)
            self.label(row, "기준 시각", width=12, anchor="w").pack(side="left")
            self.time_pickers[action + "_time"] = TimePicker(row, self.vars[action + "_time"])
            self.time_pickers[action + "_time"].pack(side="left")
            modekey = "time_mode" if action == "checkin" else "checkout_time_mode"
            for label in MODE_LABELS.values():
                ttk.Radiobutton(row, text=label, value=label, variable=self.vars[modekey]).pack(side="left", padx=(14, 0))
            self.label(row, "24시간 기준", size=9, color=MUTED).pack(side="left")
            row = tk.Frame(box, bg=CARD)
            row.pack(fill="x", pady=6)
            self.label(row, "랜덤 범위", width=12, anchor="w").pack(side="left")
            prefix = "" if action == "checkin" else "checkout_"
            self.random_inputs[action] = []
            range_keys = []
            for direction, label in (("before", "초 전부터"), ("after", "초 후까지")):
                minutes_key = prefix + "random_" + direction + "_minutes"
                seconds_key = prefix + "random_" + direction + "_seconds"
                range_keys.extend((minutes_key, seconds_key))
                widget = ttk.Spinbox(row, from_=0, to=720, textvariable=self.vars[minutes_key], width=5)
                widget.pack(side="left")
                self.random_inputs[action].append(widget)
                self.label(row, "분", color=MUTED).pack(side="left", padx=(5, 8))
                widget = ttk.Combobox(row, values=list(range(60)), textvariable=self.vars[seconds_key], width=3, state="readonly")
                widget.pack(side="left")
                self.random_inputs[action].append(widget)
                self.label(row, label, color=MUTED).pack(side="left", padx=(5, 16))
            preview = self.label(box, "", size=10, color=ACCENT, justify="left", wraplength=850)
            preview.pack(anchor="w", pady=(5, 0))
            self.schedule_previews[action] = preview
            for key in (modekey, action + "_time", *range_keys):
                self.vars[key].trace_add("write", lambda *_args, a=action: self.update_time_preview(a))
            self.update_time_preview(action)
        box = self.card(page, "근무일과 반차")
        box.pack(fill="x", pady=(0, 14))
        row = tk.Frame(box, bg=CARD)
        row.pack(fill="x")
        for index, name in enumerate("월화수목금토일"):
            CheckButton(row, text=name, variable=self.workdays[index]).pack(side="left", padx=(0, 16))
        CheckButton(box, text="한국 공휴일과 대체공휴일 제외", variable=self.vars["skip_korean_holidays"]).pack(anchor="w", pady=6)
        ttk.Combobox(box, textvariable=self.vars["half_day_policy"], values=list(POLICY_LABELS.values()), state="readonly", width=50).pack(anchor="w", pady=6)
        for key, label in (("am_half_checkout_time", "오전 반차 퇴근"), ("pm_half_checkout_time", "오후 반차 퇴근")):
            row = tk.Frame(box, bg=CARD)
            row.pack(fill="x", pady=6)
            self.label(row, label, color=MUTED).pack(side="left", padx=(0, 8))
            self.time_pickers[key] = TimePicker(row, self.vars[key])
            self.time_pickers[key].pack(side="left", padx=(0, 24))
        self.label(box, "반차에도 출근 시각은 유지합니다. 퇴근은 두레이 근태 또는 달력의 반차 유형에 맞춰 적용됩니다.", size=9, color=MUTED).pack(anchor="w")
        box = self.card(page, "예약 실행 허용 시간")
        box.pack(fill="x", pady=(0, 14))
        self.field(box, "지연 허용 시간 (분)", "late_grace_minutes", width=10)
        self.label(box, "PC를 늦게 켠 경우 이 시간 안에서만 실행합니다. 0분이면 예약 시각부터 5초 이내입니다.\nPC가 켜져 있고 Windows에 로그인되어 있어야 합니다. 당일 출근·퇴근만 지원합니다.", color=MUTED, size=9, justify="left").pack(anchor="w", pady=(8, 0))

    def build_settings(self):
        page = self.scroll_page("settings")
        update_box = self.card(page, "앱 업데이트")
        update_box.pack(fill="x", pady=(0, 14))
        self.update_status = self.label(update_box, f"현재 v{VERSION} · 업데이트해도 기존 계정·시간·달력 설정을 유지합니다.",
                                        color=MUTED, justify="left", wraplength=760)
        self.update_status.pack(anchor="w")
        self.update_button = self.button(update_box, "업데이트 확인", self.update_app)
        self.update_button.pack(anchor="w", pady=(10, 0))
        box = self.card(page, "두레이 계정")
        box.pack(fill="x", pady=(0, 14))
        self.field(box, "회사 두레이 주소", "tenant_url")
        self.label(box, "예: https://회사.dooray.com  ·  /home 등은 아래 페이지 경로에 입력하세요.", color=MUTED, size=9).pack(anchor="w", pady=(0, 5))
        self.field(box, "출퇴근 페이지 경로", "attendance_path")
        self.field(box, "로그인 ID", "login_id")
        self.password_entry = self.field(box, "비밀번호", "password", secret=True)
        self.password_visible = tk.BooleanVar(value=False)
        CheckButton(box, text="입력한 비밀번호 보기", variable=self.password_visible,
                    command=lambda: self.password_entry.configure(show="" if self.password_visible.get() else "●")).pack(anchor="w")
        self.password_label = self.label(box, "비밀번호를 비워두면 이 ID에 저장된 값을 사용합니다. ID를 바꾸면 비밀번호도 다시 입력하세요.", color=MUTED, size=9)
        self.password_label.pack(anchor="w", pady=(6, 0))
        self.connection_status = self.label(box, "연결 미확인 · 아래 버튼으로 입력값 저장과 로그인을 함께 확인하세요.",
                                            color=MUTED, size=10, justify="left", wraplength=820)
        self.connection_status.pack(anchor="w", pady=(12, 4))
        row = tk.Frame(box, bg=CARD)
        row.pack(fill="x", pady=(12, 0))
        self.button(row, "저장하고 연결 확인", self.connect, primary=True).pack(side="left")
        self.button(row, "브라우저에서 직접 로그인", lambda: self.connect(manual=True)).pack(side="left", padx=8)
        self.cancel_connection = ttk.Button(row, text="연결 취소", state="disabled",
                                            command=self.request_connection_cancel)
        self.cancel_connection.pack(side="left")
        self.connection_cancel = threading.Event()
        self.button(box, "저장된 비밀번호 삭제", self.delete_password).pack(anchor="w", pady=(10, 0))
        self.label(box, "직접 로그인: 입력한 설정을 저장한 뒤 브라우저를 엽니다. SSO·OTP는 직접 완료하세요.\n로그인 성공과 이번 달 근태 조회를 따로 확인합니다. 출근·퇴근 버튼은 누르지 않습니다.",
                   color=MUTED, size=9, justify="left", wraplength=820).pack(anchor="w", pady=(8, 0))
        for key in ("tenant_url", "attendance_path", "login_id", "password", "dooray_calendar_path"):
            self.vars[key].trace_add("write", lambda *_: self.connection_status.configure(
                text="입력값 변경 · 저장하고 연결 확인을 눌러 현재 값으로 확인하세요.", fg=MUTED))
        box = self.card(page, "두레이 근태 연동")
        box.pack(fill="x", pady=(0, 14))
        CheckButton(box, text="본인 근태 달력의 휴가 / 반차 / 휴무를 자동 반영", variable=self.vars["dooray_sync_enabled"]).pack(anchor="w")
        self.field(box, "본인 월별 근태 경로", "dooray_calendar_path")
        self.field(box, "달력 영역 (선택)", "dooray_calendar_selector")
        self.field(box, "갱신 간격 (분)", "sync_interval_minutes", width=10)
        self.label(box, "로그인 세션으로 이번 달을 읽습니다. 팀 달력 대신 본인 월별 근태 화면을 지정하세요.\n일자 전체를 확인하지 못하면 자동 실행을 보류합니다. 날짜별 수동 설정은 우선 적용됩니다.\n화면 구조가 다르면 달력 영역 CSS 선택자가 필요할 수 있습니다.", color=MUTED, size=9, justify="left", wraplength=780).pack(anchor="w", pady=8)
        self.sync_status = self.label(box, "", color=MUTED, size=9, wraplength=780, justify="left")
        self.sync_status.pack(anchor="w", pady=4)
        self.button(box, "이번 달 근태 가져오기", self.sync, primary=True).pack(anchor="w", pady=6)
        box = self.card(page, "실행 환경과 점검")
        box.pack(fill="x", pady=(0, 14))
        CheckButton(box, text="백그라운드 브라우저로 실행", variable=self.vars["headless"]).pack(anchor="w")
        CheckButton(box, text="실패 시 알림 표시", variable=self.vars["notify_on_failure"]).pack(anchor="w")
        self.field(box, "실패 시 최대 시도 수", "retry_count", width=10)
        self.field(box, "재시도 간격 (초)", "retry_delay_seconds", width=10)
        self.label(box, "기존 제외 날짜 (쉼표 구분, YYYY-MM-DD)", color=MUTED, size=9).pack(anchor="w", pady=(10, 5))
        ttk.Entry(box, textvariable=self.skipdates).pack(fill="x")
        for key, var in self.selector_vars.items():
            self.label(box, "출근 버튼 선택자 (선택)" if key == "checkin_button" else "퇴근 버튼 선택자 (선택)", color=MUTED, size=9).pack(anchor="w", pady=(10, 5))
            ttk.Entry(box, textvariable=var).pack(fill="x")
        row = tk.Frame(box, bg=CARD)
        row.pack(fill="x", pady=(14, 0))
        self.button(row, "Chromium 설치", self.install_browser).pack(side="left")
        self.button(row, "로그인 화면 점검", lambda: self.probe(True)).pack(side="left", padx=10)
        self.button(row, "출퇴근 버튼 점검", lambda: self.probe(False)).pack(side="left")

    def build_logs(self):
        page = self.pages["logs"]
        self.label(page, "예약과 수동 실행 내역", size=15, bold=True).pack(anchor="w", pady=(0, 12))
        panel = tk.Frame(page, bg="#1D2635")
        panel.pack(fill="both", expand=True)
        self.log_text = tk.Text(panel, bg="#1D2635", fg="#D9E6F3", insertbackground="white", font=("Consolas", 10),
                                wrap="word", relief="flat", padx=16, pady=16, state="disabled")
        scroll = ttk.Scrollbar(panel, command=self.log_text.yview)
        scroll.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(yscrollcommand=scroll.set)
        row = tk.Frame(page, bg=BG)
        row.pack(fill="x", pady=(12, 0))
        from dooray_gui import open_folder
        for name, path in (("로그 폴더", core.LOG_DIR), ("스크린샷", core.SHOT_DIR), ("설정 폴더", core.CONFIG_PATH.parent)):
            self.button(row, name, lambda p=path: open_folder(p)).pack(side="left", padx=(0, 8))
        self.button(row, "최근 로그 불러오기", self.load_logs).pack(side="right")

    def update_time_preview(self, action):
        mode = "time_mode" if action == "checkin" else "checkout_time_mode"
        random_mode = self.vars[mode].get() == MODE_LABELS["random"]
        for widget in self.random_inputs[action]:
            active_state = "readonly" if isinstance(widget, ttk.Combobox) else "normal"
            widget.configure(state=active_state if random_mode else "disabled")
        try:
            cfg = copy.deepcopy(self.cfg)
            cfg[mode] = "random" if random_mode else "fixed"
            cfg[action + "_time"] = self.vars[action + "_time"].get()
            prefix = "" if action == "checkin" else "checkout_"
            for direction in ("before", "after"):
                for unit, maximum in (("minutes", 720), ("seconds", 59)):
                    key = prefix + "random_" + direction + "_" + unit
                    cfg[key] = int(self.vars[key].get())
                    if not 0 <= cfg[key] <= maximum:
                        raise ValueError()
            # Show the normal-day range; date-specific times take priority.
            cfg["date_overrides"] = {}
            start, end = rules.time_bounds(cfg, self.today, action)
            text = (f"랜덤 적용 범위 {rules.clock_text(start)} ~ {rules.clock_text(end)} · 1초 단위로 무작위 선택" if random_mode
                    else f"고정 {rules.clock_text(start)} · 랜덤 범위는 적용하지 않습니다.")
            text += "\n저장 후 적용 · 날짜별 시간과 반차 퇴근 시간이 있으면 우선합니다."
            text += "\n선택 시각에 처리를 시작하며, 접속·로그인 시간만큼 실제 기록은 늦어질 수 있습니다."
        except (ValueError, TypeError):
            text = "유효한 시각과 0~720분, 0~59초 범위를 입력하세요."
        self.schedule_previews[action].configure(text=text)

    def update_app(self):
        def failed(error):
            self.update_status.configure(text="업데이트 실패 · " + error, fg="#B25545")
        if self.update_release is None:
            def checked(release):
                self.update_release = release
                if release:
                    self.update_status.configure(text=f"v{release.version} 사용 가능 · 아래 버튼을 누르면 검증 후 설치하고 앱을 다시 엽니다.\n계정·시간·달력·자동 예약 ON/OFF를 보존합니다.", fg=ACCENT)
                    self.update_button.configure(text=f"v{release.version} 다운로드 및 설치")
                else:
                    self.update_status.configure(text=f"최신 버전입니다 · v{VERSION}", fg=ACCENT)
            self.background("업데이트 확인", updater.check_latest, checked, failed=failed)
        else:
            def prepared(folder):
                try:
                    process = updater.launch_installer(folder)
                    self.update_button.configure(state="disabled")
                    self.update_status.configure(text="설치 중 · 설치 프로그램이 앱을 종료한 뒤 새 버전을 엽니다. 실패 내용은 업데이트 폴더의 install.log에 저장됩니다.", fg=ACCENT)
                    def monitor():
                        result = process.wait()
                        if result:
                            self.events.put(("update_failed", f"설치가 완료되지 않았습니다 (종료 코드 {result}).\n{folder / 'install.log'}에서 오류를 확인한 뒤 다시 시도하세요."))
                    threading.Thread(target=monitor, daemon=True).start()
                except Exception as exc:
                    failed(str(exc))
            self.background("업데이트 다운로드", lambda: updater.prepare_update(self.update_release, core.DATA_DIR / "updates",
                progress=lambda text: self.events.put(("update_progress", text))), prepared, failed=failed)

    def collect(self):
        cfg = copy.deepcopy(self.cfg)
        for key, var in self.vars.items():
            if key == "password":
                continue
            value = var.get()
            if type(self.cfg.get(key)) is int:
                try:
                    value = int(value)
                except ValueError as exc:
                    raise ValueError(f"{key}: 정수로 입력하세요.") from exc
            cfg[key] = value.strip() if isinstance(value, str) else value
        for key, labels in (("time_mode", MODE_LABELS), ("checkout_time_mode", MODE_LABELS), ("half_day_policy", POLICY_LABELS)):
            cfg[key] = {v: k for k, v in labels.items()}[cfg[key]]
        cfg["tenant_url"] = cfg["tenant_url"].rstrip("/")
        cfg["workdays"] = [i for i, var in enumerate(self.workdays) if var.get()]
        cfg["skip_weekends"] = 5 not in cfg["workdays"] and 6 not in cfg["workdays"]
        cfg["skip_dates"] = [d.strip() for d in self.skipdates.get().split(",") if d.strip()]
        cfg["date_overrides"] = copy.deepcopy(self.overrides)
        for key, var in self.selector_vars.items():
            cfg["selectors"][key] = var.get().strip() or None
        rules.validate_config(cfg)
        return cfg

    def save(self, after=None):
        if self.busy:
            return
        try:
            cfg = self.collect()
            password = self.vars["password"].get()
            if password and not cfg["login_id"]:
                raise ValueError("비밀번호를 저장하려면 로그인 ID가 필요합니다.")
        except (ValueError, KeyError) as exc:
            messagebox.showerror(TITLE, str(exc), parent=self.root)
            return
        def worker():
            if password:
                import keyring
                keyring.set_password(core.KEYRING_SERVICE, cfg["login_id"], password)
                cfg["login_revision"] = uuid.uuid4().hex
            core.save_config(cfg)
            for action in rules.ACTIONS:
                prefix = "" if action == "checkin" else "checkout_"
                core.log.info("설정 저장: %s %s / 모드 %s / 앞 %s분 %s초 뒤 %s분 %s초", rules.LABELS[action],
                              cfg[action + "_time"], cfg[prefix + "time_mode"],
                              cfg[prefix + "random_before_minutes"], cfg[prefix + "random_before_seconds"],
                              cfg[prefix + "random_after_minutes"], cfg[prefix + "random_after_seconds"])
            st = scheduler.status()
            if st.get("exists") and st.get("state") != "Disabled" and not st.get("watcher"):
                scheduler.enable(cfg)
            return cfg
        def done(value):
            self.cfg = value
            self.vars["password"].set("")
            on = self.task.get("exists") and self.task.get("state") != "Disabled"
            self.feedback.configure(text="설정 저장 완료" if on else "설정 저장 완료 · 자동 예약은 아직 OFF입니다.", fg=ACCENT if on else "#B25545")
            self.cache = core.load_calendar(self.cfg)
            self.refresh_summary()
            self.render_calendar()
            if after:
                after()
        self.background("설정 저장", worker, done)

    def toggle_schedule(self):
        if self.task.get("error"):
            messagebox.showerror(TITLE, "Windows 예약 상태를 확인할 수 없습니다. 잠시 후 다시 시도하세요.", parent=self.root)
            self.refresh_status()
            return
        on = self.task.get("exists") and self.task.get("state") != "Disabled"
        if on:
            self.background("자동 예약 끄기", scheduler.disable)
            return
        self.start_schedule()

    def start_schedule(self):
        if self.busy:
            return
        def enable():
            try:
                rules.validate_config(self.cfg, require_account=True)
                if not any(self.cfg[a + "_enabled"] for a in rules.ACTIONS):
                    raise ValueError("출근 또는 퇴근 사용을 먼저 켜세요.")
            except ValueError as exc:
                messagebox.showerror(TITLE, str(exc), parent=self.root)
                return
            def started(_):
                self.feedback.configure(text="자동 예약 시작 확인 · 오늘의 선택 시각을 확인하세요.", fg=ACCENT)
            self.background("자동 예약 시작", lambda: scheduler.enable(self.cfg), started)
        self.save(after=enable)

    def background(self, label, fn, done=None, quiet=False, failed=None):
        if self.busy:
            return
        self.busy = True
        self.quiet_work = quiet
        self.failed_work = failed
        for button in self.buttons:
            button.configure(state="disabled")
        self.feedback.configure(text=label + "…", fg=MUTED)
        core.log.info("%s 시작", label)
        def worker():
            try:
                value = fn()
                self.events.put(("done", (done, value, None, label)))
            except (Exception, SystemExit) as exc:
                self.events.put(("done", (done, None, str(exc), label)))
        threading.Thread(target=worker, daemon=True).start()

    def account_dirty(self):
        return bool(self.vars["password"].get()) or any(
            self.vars[key].get().strip().rstrip("/") != self.cfg.get(key, "").rstrip("/")
            for key in ("tenant_url", "login_id", "attendance_path", "dooray_calendar_path"))

    def request_connection_cancel(self):
        self.connection_cancel.set()
        self.connection_status.configure(text="취소 요청됨 · 현재 페이지 응답 대기가 끝나면 닫습니다.", fg=MUTED)
        self.cancel_connection.configure(state="disabled")

    def connect(self, manual=False):
        if self.busy:
            return
        def start():
            cfg = copy.deepcopy(self.cfg)
            self.connection_cancel.clear()
            self.connection_status.configure(text="연결 확인 중 · 브라우저 준비…", fg=ACCENT)
            def progress(message):
                self.events.put(("connection", message))
            def finish(result):
                self.cancel_connection.configure(state="disabled")
                self.connection_status.configure(
                    text=result["message"] if not self.account_dirty() else "확인 중 입력값이 변경되었습니다. 다시 저장하고 연결 확인을 누르세요.",
                    fg=ACCENT if result["ok"] else "#B25545")
                self.cache = core.load_calendar(self.cfg)
                self.render_calendar(update_editor=False)
            def failed(error):
                self.cancel_connection.configure(state="disabled")
                self.connection_status.configure(text=error, fg="#B25545")
            self.background("두레이 연결 확인",
                            lambda: runtime.check_connection(cfg, manual=manual, progress=progress,
                                                             cancel=self.connection_cancel),
                            finish, failed=failed)
            self.cancel_connection.configure(state="normal")
        self.save(after=start)

    def run_action(self, action, dry=False):
        if self.account_dirty():
            self.show("settings")
            self.connection_status.configure(text="계정 변경 후 저장하고 연결 확인을 먼저 누르세요.", fg="#B25545")
            return
        if not dry and not messagebox.askyesno(TITLE, f"지금 실제 {rules.LABELS[action]} 처리할까요?\n오늘의 휴일/휴가 및 날짜별 OFF 설정을 무시합니다.", parent=self.root):
            return
        cfg = copy.deepcopy(self.cfg)
        args = types.SimpleNamespace(action=action, dry_run=dry, inspect=False, inspect_login=False,
                                     headed=not cfg["headless"], force=True, scheduled=False,
                                     progress=lambda message: self.events.put(("action_progress", message)))
        def run():
            if core.do_run(cfg, args):
                raise RuntimeError(getattr(args, "error", "실행에 실패했습니다. 실행 기록을 확인하세요."))
            return getattr(args, "result", "확인 완료")
        def done(result):
            text = (rules.LABELS[action] + " 점검 완료 · 새 출퇴근 기록을 생성하지 않았습니다.\n" + result) if dry else result
            self.action_status.configure(text=text, fg=ACCENT)
            messagebox.showinfo(TITLE, text, parent=self.root)
        self.show("home")
        self.action_status.configure(text="다른 작업이 진행 중이면 완료될 때까지 기다립니다.", fg=MUTED)
        self.background(rules.LABELS[action] + (" 점검 (기록 없음)" if dry else " 수동 실행"), run, done,
                        failed=lambda error: self.action_status.configure(text=error, fg="#B25545"))

    def sync(self, quiet=False):
        if self.busy:
            return
        if self.account_dirty():
            if not quiet:
                self.show("settings")
                self.connection_status.configure(text="계정 입력값이 아직 저장되지 않았습니다. 저장하고 연결 확인을 먼저 누르세요.", fg="#B25545")
            return
        cfg = copy.deepcopy(self.cfg)
        def run():
            rules.validate_config(cfg, require_account=True)
            return runtime.sync_calendar(cfg)
        def done(cache):
            self.cache = cache
            self.refresh_summary()
            self.render_calendar(update_editor=False)
            # Keep pending date edits while refreshing the remote information.
            pending = {k: v.get() for k, v in self.day_vars.items()}
            self.select_day(self.selected, redraw=False)
            for k, v in pending.items():
                self.day_vars[k].set(v)
        self.background("두레이 근태 가져오기", run, done, quiet=quiet)

    def auto_sync(self):
        now = rules.now_kst()
        if self.busy or self.account_dirty() or now.timestamp() < getattr(self, "next_auto_sync", 0):
            return
        if self.cfg.get("login_id") and "YOURCOMPANY" not in self.cfg.get("tenant_url", "") and not rules.cache_fresh(self.cache, self.cfg, rules.now_kst()):
            self.next_auto_sync = now.timestamp() + 300
            self.sync(quiet=True)

    def probe(self, login):
        if self.account_dirty():
            self.show("settings")
            self.connection_status.configure(text="계정 변경 후에는 저장하고 연결 확인을 먼저 누르세요.", fg="#B25545")
            return
        cfg = copy.deepcopy(self.cfg)
        args = types.SimpleNamespace(action="checkin", dry_run=False, inspect=not login, inspect_login=login, headed=True, force=True)
        self.show("logs")
        def run():
            if core.do_run(cfg, args):
                raise RuntimeError("화면 점검 실패. 로그를 확인하세요.")
        self.background("화면 점검", run)

    def install_browser(self):
        from dooray_gui import install_chromium
        def run():
            if not install_chromium(lambda line: self.events.put(("log", line))):
                raise RuntimeError("Chromium 설치 실패. 실행 기록을 확인하세요.")
        self.show("logs")
        self.background("Chromium 설치", run)

    def delete_password(self):
        login = self.vars["login_id"].get().strip()
        if not login or not messagebox.askyesno(TITLE, "이 계정의 저장된 비밀번호를 삭제할까요?", parent=self.root):
            return
        def run():
            import keyring
            keyring.delete_password(core.KEYRING_SERVICE, login)
        self.background("비밀번호 삭제", run)

    def change_month(self, offset):
        index = self.month.year * 12 + self.month.month - 1 + offset
        self.month = dt.date(index // 12, index % 12 + 1, 1)
        self.selected = self.month
        self.render_calendar()

    def go_today(self):
        self.selected = rules.now_kst().date()
        self.month = self.selected.replace(day=1)
        self.render_calendar()

    def render_calendar(self, update_editor=True):
        self.month_label.configure(text=f"{self.month.year}년 {self.month.month}월")
        self.calendar_grid.set_month(self.month, self.selected, self.today, self.cfg, self.cache, self.display_plans.get())
        records = [record for key, record in self.cache.get("days", {}).items() if key.startswith(self.month.strftime("%Y-%m"))]
        full = sum(r.get("kind") == "vacation" for r in records)
        half = sum(r.get("kind") in ("am_half", "pm_half") for r in records)
        self.month_counts.configure(text=f"두레이 휴가 {full}일 · 반차 {half}일" if records else "두레이 근태 미수집")
        if update_editor:
            self.select_day(self.selected, redraw=False)

    def select_day(self, day, redraw=True):
        self.selected = day
        if day.month != self.month.month or day.year != self.month.year:
            self.month = day.replace(day=1)
        if redraw:
            self.render_calendar()
            return
        item = self.overrides.get(day.isoformat(), {})
        self.day_vars["kind"].set(rules.DAY_LABELS[item.get("kind", "auto")])
        for action in rules.ACTIONS:
            self.day_vars[action].set(OVERRIDE_LABELS[item.get(action, "auto")])
            kind, _ = rules.day_kind(self.cfg, day, self.cache)
            base_key = kind + "_checkout_time" if action == "checkout" and kind in ("am_half", "pm_half") and self.cfg["half_day_policy"] == "checkout_time" else action + "_time"
            self.time_pickers["day_" + action + "_time"].last_value = self.cfg[base_key]
            self.day_vars[action + "_time"].set(item.get(action + "_time", ""))
        self.day_vars["note"].set(item.get("note", ""))
        self.selected_label.configure(text=f"{day.month}월 {day.day}일  {'월화수목금토일'[day.weekday()]}요일")
        data = day_display(self.cfg, day, self.cache)
        self.selected_reason.configure(text="예약 기준 · " + data["reason"])
        remote = data["remote"]
        if not remote:
            info = "두레이 근태 미수집\n이번 달 근태 가져오기를 눌러 주세요." if day.strftime("%Y-%m") == rules.now_kst().strftime("%Y-%m") else "이번 달 외 근태는 직접 설정해 주세요."
        elif remote.get("kind") == "work":
            info = "Dooray · 등록된 휴가 없음"
        else:
            info = "Dooray · " + remote.get("label", "근태")
            if remote.get("start") and remote.get("end"):
                start = dt.datetime.fromisoformat(remote["start"])
                end = dt.datetime.fromisoformat(remote["end"])
                info += f"\n{start:%m.%d %H:%M} – {end:%m.%d %H:%M}"
            if not self.cfg["dooray_sync_enabled"]:
                info += "\n캘린더 표시 중 · 예약 반영 꺼짐"
        if self.cache.get("error") and remote:
            info += "\n최근 갱신 실패 · 이전 조회 내용"
        self.remote_detail.configure(text=info)
        self.day_plan.configure(text="   /   ".join(rules.LABELS[a] + " " + (rules.clock_text(p.target).removesuffix(":00") if p.enabled else "OFF") for a, p in data["plans"].items()))

    def save_day(self):
        value = {key: var.get().strip() for key, var in self.day_vars.items()}
        value["kind"] = {v: k for k, v in rules.DAY_LABELS.items()}[value["kind"]]
        for action in rules.ACTIONS:
            value[action] = {v: k for k, v in OVERRIDE_LABELS.items()}[value[action]]
        old = copy.deepcopy(self.overrides)
        self.overrides[self.selected.isoformat()] = value
        try:
            self.collect()
        except ValueError as exc:
            self.overrides = old
            messagebox.showerror(TITLE, str(exc), parent=self.root)
            return
        self.save()

    def reset_day(self):
        self.overrides.pop(self.selected.isoformat(), None)
        self.skipdates.set(", ".join(d.strip() for d in self.skipdates.get().split(",") if d.strip() and d.strip() != self.selected.isoformat()))
        self.save()

    def refresh_summary(self):
        today = rules.now_kst().date()
        state = core.load_state()
        self.date_label.configure(text=f"{today:%Y년 %m월 %d일} {'월화수목금토일'[today.weekday()]}요일  ·  Asia/Seoul")
        for action, (value, detail) in self.summary.items():
            plan = rules.plan_action(self.cfg, today, action, self.cache)
            running = self.task.get("exists") and self.task.get("state") != "Disabled" and self.task.get("worker_running")
            value.configure(text=(rules.clock_text(plan.target) if running else "시작 필요") if plan.enabled else "OFF", fg=ACCENT if running and plan.enabled else MUTED)
            text = (plan.description + "\n" if plan.enabled else "") + plan.reason
            if plan.enabled:
                text += "\n오늘 선택 시각 " + rules.clock_text(plan.target)
                if not running:
                    text += "\n‘저장하고 자동 예약 시작’을 눌러주세요."
            if state.get("last_" + action + "_date") == today.isoformat():
                text += "\n오늘 " + rules.LABELS[action] + " 처리 확인됨"
            elif state.get("last_" + action + "_error") and state.get(
                    "last_" + action + "_error_at", state.get("last_error_at", "")).startswith(today.isoformat()):
                text += "\n최근 시도 실패: " + state["last_" + action + "_error"].splitlines()[0]
                text += "\n점검으로 현재 두레이 기록을 확인하세요."
            detail.configure(text=text)
        if self.cache.get("error"):
            sync = "근태 동기화 실패 · " + self.cache["error"]
        elif self.cache.get("synced_at"):
            sync = f"근태: {self.cache.get('month')} · 마지막 갱신 {self.cache['synced_at'][:19].replace('T', ' ')}"
        else:
            sync = "두레이 근태를 아직 가져오지 않았습니다. 연결 · 설정에서 연동할 수 있습니다."
        if not self.cfg["dooray_sync_enabled"]:
            sync += "\n자동 근태 반영 OFF · 수동 달력 설정은 적용됩니다."
        elif not rules.cache_fresh(self.cache, self.cfg, rules.now_kst()):
            sync += "\n갱신이 필요합니다. 근태를 확인한 뒤 자동 실행합니다."
        self.home_sync.configure(text=sync)
        self.sync_status.configure(text=sync)
        if self.cache.get("error"):
            short = "최근 갱신 실패 · 이전 근태 표시 중 · 실행 기록에서 확인"
        elif self.cache.get("synced_at"):
            stamp = self.cache["synced_at"][:16].replace("T", " ")
            short = f"Dooray 연결됨  ·  {self.cache.get('month', '')}  ·  마지막 갱신 {stamp}"
        else:
            short = "연차·반차를 가져오면 날짜별로 표시됩니다."
        self.calendar_sync.configure(text=short)

    def refresh_status(self):
        if self.status_pending:
            return
        self.status_pending = True
        def run():
            try:
                result = scheduler.status()
            except Exception as exc:
                result = {"error": str(exc)}
            self.events.put(("status", result))
        threading.Thread(target=run, daemon=True).start()

    def apply_status(self, st):
        self.status_pending = False
        self.task = st
        on = st.get("exists") and st.get("state") != "Disabled"
        running = on and st.get("worker_running")
        self.toggle.configure(text="자동 예약 끄기" if on else "자동 예약 켜기")
        self.badge.configure(text="예약 상태 확인 실패" if st.get("error") else ("● 자동 예약 실행 중" if running else "예약 시작 필요" if on else "○ 자동 예약 OFF"),
                             fg=ACCENT if running else "#B25545", bg=SOFT if running else BG)
        text = "예약 감시 실행 중 · 창을 닫아도 자동 예약이 유지됩니다." if running else "자동 예약이 실행되지 않습니다. ‘저장하고 자동 예약 시작’을 눌러주세요."
        if not on:
            text += "\n출근·퇴근 포함 체크와 시간 저장만으로는 자동 실행이 시작되지 않습니다."
        if on and not st.get("watcher"):
            text += "\n기존 예약입니다. 설정을 저장하면 달력 기반 예약으로 전환됩니다."
        if st.get("error"):
            text = "Windows 예약 상태 조회 실패: " + st["error"]
        text += "\nChromium " + ("준비 완료" if core.chromium_ready() else "설치 필요 · 연결 · 설정에서 설치하세요.")
        self.task_text.configure(text=text)
        self.schedule_status.configure(text=text, fg=ACCENT if running else "#B25545")
        self.refresh_summary()

    def load_logs(self):
        path = core.LOG_DIR / "checkin.log"
        if path.exists():
            self.events.put(("log", "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-250:])))

    def drain(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", value + "\n")
                    if int(self.log_text.index("end-1c").split(".")[0]) > 3000:
                        self.log_text.delete("1.0", "1000.0")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "status":
                    self.apply_status(value)
                elif kind == "connection":
                    self.connection_status.configure(text=value, fg=ACCENT)
                elif kind == "action_progress":
                    self.action_status.configure(text=value, fg=MUTED)
                    self.feedback.configure(text=value, fg=MUTED)
                    self.events.put(("log", value))
                elif kind == "update_progress":
                    self.update_status.configure(text=value, fg=ACCENT)
                elif kind == "update_failed":
                    self.update_status.configure(text=value, fg="#B25545")
                    self.update_button.configure(state="normal")
                elif kind == "done":
                    done, result, error, label = value
                    self.busy = False
                    for button in self.buttons:
                        button.configure(state="normal")
                    self.feedback.configure(text=label + (" 실패 · 실행 기록 확인" if error else " 완료"), fg="#B25545" if error else ACCENT)
                    (core.log.error if error else core.log.info)("%s", label + (": " + error if error else " 완료"))
                    if error:
                        self.cache = core.load_calendar(self.cfg)
                        if self.failed_work:
                            self.failed_work(error)
                        if not self.quiet_work:
                            messagebox.showerror(TITLE, error, parent=self.root)
                    elif done:
                        done(result)
                    self.refresh_summary()
                    # A completion callback may have started the next job (save
                    # -> register). Query its result after that job completes.
                    if not self.busy:
                        self.refresh_status()
        except queue.Empty:
            pass
        self.drain_timer = self.root.after(100, self.drain)

    def periodic(self):
        if not self.busy:
            old_cache = self.cache
            self.cache = core.load_calendar(self.cfg)
            self.refresh_summary()
            if self.cache != old_cache:
                self.render_calendar(update_editor=False)
            self.refresh_status()
            if self.today != rules.now_kst().date():
                self.today = rules.now_kst().date()
                self.render_calendar()
        if self.current == "calendar":
            self.auto_sync()
        self.periodic_timer = self.root.after(60000, self.periodic)

    def close(self):
        if self.busy:
            messagebox.showinfo(TITLE, "진행 중인 작업이 끝난 후 닫아주세요.", parent=self.root)
            return
        core.log.removeHandler(self.log_handler)
        for timer in (getattr(self, "drain_timer", None), getattr(self, "periodic_timer", None), getattr(self, "auto_sync_timer", None)):
            if timer:
                self.root.after_cancel(timer)
        self.root.destroy()


def build_app():
    root = tk.Tk()
    root.app = App(root)
    return root
