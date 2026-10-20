"""Time selection and calendar presentation, independent of attendance execution."""
from __future__ import annotations

import calendar
import datetime as dt
import tkinter as tk
from tkinter import font, ttk

import attendance_rules as rules

BLUE = "#2764E7"
INK = "#242A35"
MUTED = "#7A8495"
LINE = "#E8EBF0"
LEAVE_COLORS = {
    "vacation": ("#F0EAFE", "#7951C5"),
    "am_half": ("#FFF1DC", "#A76A11"),
    "pm_half": ("#E4F4EE", "#208566"),
    "unknown_leave": ("#FFF0ED", "#BE5B47"),
    "holiday": ("#FFF0F2", "#CC6475"),
    "work": ("#EAF1FF", BLUE),
}


class CheckButton(tk.Frame):
    """A keyboard-accessible checkbox with a consistent light appearance."""
    def __init__(self, parent, *, text, variable, command=None):
        super().__init__(parent, bg=parent.cget("bg"), cursor="hand2", takefocus=True)
        self.variable, self.command = variable, command
        self.icon = tk.Canvas(self, width=20, height=26, bg=self.cget("bg"), highlightthickness=0)
        self.icon.pack(side="left")
        self.text = tk.Label(self, text=text, bg=self.cget("bg"), fg=INK, font=("Malgun Gothic", 9), padx=4)
        self.text.pack(side="left")
        for child in (self, self.icon, self.text):
            child.bind("<Button-1>", self.toggle)
        self.bind("<space>", self.toggle)
        self.bind("<FocusIn>", lambda e: self.draw())
        self.bind("<FocusOut>", lambda e: self.draw())
        self.trace = variable.trace_add("write", self.draw)
        self.bind("<Destroy>", self.cleanup, add="+")
        self.draw()

    def draw(self, *_):
        on = self.variable.get()
        self.icon.delete("all")
        self.icon.create_rectangle(3, 6, 17, 20, fill=BLUE if on else "white",
                                   outline=BLUE if on or self.focus_get() is self else "#C9D1DF")
        if on:
            self.icon.create_line(6, 13, 9, 16, 14, 10, fill="white", width=2)

    def toggle(self, _event=None):
        self.focus_set()
        self.variable.set(not self.variable.get())
        if self.command:
            self.command()
        return "break"

    def cleanup(self, event):
        if event.widget is self:
            self.variable.trace_remove("write", self.trace)


class TimePicker(tk.Frame):
    """Hour/minute/second dropdowns bound to the existing HH:MM[:SS] setting.

    External updates (including legacy seconds) are preserved byte-for-byte until
    the user chooses a value. Optional date overrides can inherit the base time.
    """
    def __init__(self, parent, variable, *, optional=False, default="09:00"):
        super().__init__(parent, bg=parent.cget("bg"))
        self.variable, self.optional, self.default = variable, optional, default
        self.updating = False
        self.last_value = default
        self.parts = [tk.StringVar(self) for _ in range(3)]
        self.inherit = tk.BooleanVar(self)
        self.combos = []
        self.row = tk.Frame(self, bg=self.cget("bg"))
        self.row.pack(anchor="w")
        for i, (part, limit, unit) in enumerate(zip(self.parts, (24, 60, 60), ("시", "분", "초"))):
            combo = ttk.Combobox(self.row, textvariable=part, values=[f"{v:02d}" for v in range(limit)],
                                 state="readonly", width=3, height=12, style="Time.TCombobox")
            combo.pack(side="left")
            combo.bind("<<ComboboxSelected>>", self.commit)
            self.combos.append(combo)
            tk.Label(self.row, text=unit, bg=self.cget("bg"), fg=MUTED,
                     font=("Malgun Gothic", 9)).pack(side="left", padx=(3, 7 if i < 2 else 0))
        if optional:
            CheckButton(self, text="기본 시간 사용", variable=self.inherit,
                            command=self.toggle_inherit).pack(anchor="w", pady=(4, 0))
        self.trace = variable.trace_add("write", self.pull)
        self.pull()
        self.bind("<Destroy>", self.cleanup, add="+")

    def pull(self, *_):
        if self.updating:
            return
        value = self.variable.get()
        inherited = self.optional and not value
        self.inherit.set(inherited)
        display = value or self.last_value
        try:
            parsed = rules.clock_text(rules.seconds(display)).split(":")
        except ValueError:
            # Keep invalid external input intact for the normal save validation.
            parsed = ["--"] * 3
        for part, val, combo in zip(self.parts, parsed, self.combos):
            part.set(val)
            combo.configure(state="disabled" if inherited else "readonly")
        if value and "--" not in parsed:
            self.last_value = value

    def commit(self, _event=None):
        values = [p.get() for p in self.parts]
        if any(not p.isdigit() for p in values):
            return
        value = ":".join(values[:2]) + (":" + values[2] if values[2] != "00" else "")
        self.last_value = value
        self.variable.set(value)

    def toggle_inherit(self):
        self.variable.set("" if self.inherit.get() else self.last_value)

    def cleanup(self, event):
        if event.widget is self:
            self.variable.trace_remove("write", self.trace)


def day_display(cfg, day, cache):
    """Show remote leave even when its application to automation is disabled."""
    remote = cache.get("days", {}).get(day.isoformat(), {})
    remote_kind = remote.get("kind", "work")
    manual = cfg.get("date_overrides", {}).get(day.isoformat(), {})
    kind, reason = rules.day_kind(cfg, day, cache)
    events = []
    if remote_kind != "work":
        events.append({"kind": remote_kind, "label": remote.get("label", "두레이 근태"), "source": "Dooray"})
    manual_kind = manual.get("kind", "auto")
    if manual_kind != "auto":
        events.append({"kind": manual_kind, "label": rules.DAY_LABELS[manual_kind], "source": "직접 설정"})
    elif not events and kind == "holiday":
        name = rules.holiday_name(day) if cfg.get("skip_korean_holidays") else None
        if name or day.isoformat() in cfg.get("skip_dates", []):
            events.append({"kind": "holiday", "label": name or "제외 날짜", "source": "휴일"})
    return {"events": events, "manual": bool(manual), "kind": kind, "reason": reason,
            "plans": {a: rules.plan_action(cfg, day, a, cache) for a in rules.ACTIONS},
            "remote": remote, "note": manual.get("note", "")}


class MonthCanvas(tk.Canvas):
    """A quiet, responsive month grid with leave chips and keyboard navigation."""
    def __init__(self, parent, on_select):
        super().__init__(parent, bg="white", highlightthickness=1, highlightbackground=LINE, highlightcolor=BLUE,
                         takefocus=True, cursor="hand2")
        self.on_select = on_select
        self.model = None
        self.cells = []
        self.small = font.Font(family="Malgun Gothic", size=9)
        self.tiny = font.Font(family="Malgun Gothic", size=8)
        self.normal = font.Font(family="Malgun Gothic", size=10)
        self.bold = font.Font(family="Malgun Gothic", size=10, weight="bold")
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Button-1>", self.click)
        for key, step in (("Left", -1), ("Right", 1), ("Up", -7), ("Down", 7)):
            self.bind("<" + key + ">", lambda e, n=step: self.navigate(n))
        self.bind("<Home>", lambda e: self.on_select(rules.now_kst().date()))

    def set_month(self, month, selected, today, cfg, cache, show_plans=True):
        self.model = (month, selected, today, cfg, cache, show_plans)
        self.draw()

    def fit(self, text, width, face=None):
        face = face or self.small
        if face.measure(text) <= width:
            return text
        while text and face.measure(text + "…") > width:
            text = text[:-1]
        return text + "…" if text else ""

    def draw(self):
        if not self.model:
            return
        month, selected, today, cfg, cache, show_plans = self.model
        self.delete("all")
        self.cells = []
        width, height = max(7, self.winfo_width()), max(100, self.winfo_height())
        head, cell_w = 36, width / 7
        weeks = calendar.Calendar(firstweekday=6).monthdatescalendar(month.year, month.month)
        cell_h = (height - head) / len(weeks)
        self.create_rectangle(0, 0, width, head, fill="#FAFBFD", outline="")
        for col, name in enumerate("일월화수목금토"):
            self.create_text(col * cell_w + 12, head / 2, text=name, anchor="w", font=self.small,
                             fill="#D36A78" if col == 0 else ("#6586C7" if col == 6 else MUTED))
        for row, week in enumerate(weeks):
            for col, day in enumerate(week):
                x, y = col * cell_w, head + row * cell_h
                active = day.month == month.month
                bg = "#EFF5FF" if day == selected else ("white" if active else "#FAFBFD")
                self.create_rectangle(x, y, x + cell_w, y + cell_h, fill=bg, outline=LINE)
                if day == selected:
                    self.create_rectangle(x+1, y+1, x+cell_w-1, y+cell_h-1, outline=BLUE, width=2)
                color = INK if active else "#BEC4CF"
                if active and col == 0:
                    color = "#CE6575"
                if active and col == 6:
                    color = "#6686C5"
                if day == today:
                    self.create_oval(x+8, y+7, x+34, y+33, fill=BLUE, outline="")
                    color = "white"
                self.create_text(x+21, y+20, text=str(day.day), font=self.bold if day == today else self.normal, fill=color)
                data = day_display(cfg, day, cache)
                if active and data["manual"]:
                    self.create_oval(x+cell_w-15, y+13, x+cell_w-9, y+19, fill=BLUE, outline="")
                if active:
                    event_y = y + 34
                    for event in data["events"][:2]:
                        if event_y + 20 > y + cell_h - 4:
                            break
                        tint, ink = LEAVE_COLORS.get(event["kind"], LEAVE_COLORS["unknown_leave"])
                        self.create_rectangle(x+7, event_y, x+cell_w-7, event_y+21, fill=tint, outline="")
                        self.create_rectangle(x+7, event_y, x+10, event_y+21, fill=ink, outline="")
                        self.create_text(x+15, event_y+10, text=self.fit(event["label"], cell_w-27), font=self.small, fill=ink, anchor="w")
                        event_y += 24
                    if show_plans:
                        enabled = [(a, p) for a, p in data["plans"].items() if p.enabled]
                        if len(enabled) == 2 and event_y + 36 > y + cell_h - 3:
                            if event_y + 14 <= y + cell_h - 2:
                                text = " · ".join(rules.clock_text(p.target)[:5] for _, p in enabled)
                                self.create_text(x+8, event_y+7, text=self.fit(text, cell_w-14, self.tiny),
                                                 font=self.tiny, fill=BLUE, anchor="w")
                        else:
                            for action, plan in enabled:
                                if event_y + 15 > y + cell_h - 2:
                                    continue
                                text = rules.LABELS[action] + " " + rules.clock_text(plan.target)[:5]
                                self.create_text(x+12, event_y+7, text=self.fit(text, cell_w-22), font=self.small,
                                                 fill=BLUE if action == "checkin" else "#5C7085", anchor="w")
                                event_y += 18
                self.cells.append((x, y, x+cell_w, y+cell_h, day))

    def click(self, event):
        self.focus_set()
        for x, y, right, bottom, day in self.cells:
            if x <= event.x < right and y <= event.y < bottom:
                self.on_select(day)
                break

    def navigate(self, step):
        if self.model:
            self.on_select(self.model[1] + dt.timedelta(days=step))
        return "break"
