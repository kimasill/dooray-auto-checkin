"""Read-only adapter for the signed-in user's monthly Dooray attendance page.

Only a complete, date-addressable month is accepted. Never infer a successful
sync from an empty screen, a login form, or a page-wide leave keyword.
"""
from __future__ import annotations

import datetime as dt
import re
from urllib.parse import parse_qs, urlparse

import attendance_rules as rules


def parse_off_duties(payload: dict, year: int, month: int) -> dict:
    """Parse the successful month response observed in Dooray's own calendar.

    No undocumented request is synthesized: the browser performs its ordinary
    authenticated GET and we read the response used to render the personal page.
    """
    if payload.get("header", {}).get("isSuccessful") is not True:
        raise RuntimeError("두레이 휴가 조회에 실패했습니다.")
    records = payload.get("result")
    if not isinstance(records, list) or payload.get("totalCount") != len(records):
        raise RuntimeError("두레이 휴가 목록이 완전하지 않습니다.")
    days = {d.isoformat(): {"kind": "work", "label": "근무일"} for d in rules.month_dates(year, month)}
    for record in records:
        try:
            start = dt.datetime.fromisoformat(record["attStaDateTime"])
            end = dt.datetime.fromisoformat(record["attEndDateTime"])
            start = start.replace(tzinfo=rules.KST) if start.tzinfo is None else start.astimezone(rules.KST)
            end = end.replace(tzinfo=rules.KST) if end.tzinfo is None else end.astimezone(rules.KST)
            if end <= start:
                raise ValueError("invalid leave interval")
            name = record["attendNm"]
            if not isinstance(name, str) or not name:
                raise ValueError("missing leave name")
            kind, label = classify_leave(name)
            if kind in ("vacation", "holiday"):
                label = name.strip()
            if kind == "work":
                kind, label = "unknown_leave", name + " · 수동 확인"
            first = max(start.date(), dt.date(year, month, 1))
            last = min((end - dt.timedelta(microseconds=1)).date(), rules.month_dates(year, month)[-1])
            if (end - dt.timedelta(microseconds=1)).date() != start.date() and kind in ("am_half", "pm_half"):
                kind, label = "unknown_leave", name + " · 여러 날짜 확인"
            while first <= last:
                value = first.isoformat()
                previous = days[value]["kind"]
                current_kind = kind
                current_label = label
                if {previous, kind} == {"am_half", "pm_half"} or previous == "vacation":
                    current_kind, current_label = "vacation", "종일 휴가"
                elif previous == "unknown_leave":
                    current_kind, current_label = previous, days[value]["label"]
                days[value] = {"kind": current_kind, "label": current_label,
                               "start": start.isoformat(), "end": end.isoformat()}
                first += dt.timedelta(days=1)
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("두레이 휴가의 날짜/종류를 해석하지 못했습니다.") from exc
    return days


class AttendanceReader:
    """Capture only the same-origin, personal off-duties month GET response."""
    def __init__(self, cfg: dict):
        self.host = urlparse(cfg["tenant_url"]).netloc
        self.months: dict[str, dict] = {}
        self.errors: dict[str, str] = {}

    def observe(self, response):
        parsed = urlparse(response.url)
        if parsed.netloc != self.host or parsed.path != "/wapi/work-schedule/v1/off-duties":
            return
        if response.request.method != "GET":
            return
        query = parse_qs(parsed.query)
        # The inspected personal endpoint uses only its inclusive date range.
        # Reject member/team filters instead of mistaking someone else's leave for ours.
        if set(query) != {"staDate", "endDate"}:
            return
        try:
            start = rules.valid_date(query["staDate"][0])
            end = rules.valid_date(query["endDate"][0])
            if start.day != 1 or end != rules.month_dates(start.year, start.month)[-1]:
                return
            key = start.strftime("%Y-%m")
            if response.status != 200:
                raise RuntimeError("두레이 휴가 조회 응답 오류")
            self.months[key] = parse_off_duties(response.json(), start.year, start.month)
            self.errors.pop(key, None)
        except Exception as exc:
            if "key" in locals():
                self.months.pop(key, None)
                self.errors[key] = str(exc)


EXTRACT_MONTH_JS = r"""(selector) => {
  const roots = selector ? [...document.querySelectorAll(selector)] : [document.body];
  const visible = el => !!(el.getClientRects().length) && getComputedStyle(el).visibility !== 'hidden';
  const records = [];
  for (const root of roots.filter(visible)) {
    const headings = [...root.querySelectorAll('h1,h2,h3,h4,[role=heading],caption,[data-month],.calendar-title,.fc-toolbar-title')];
    const months = new Set();
    for (const el of headings) {
      const text = el.getAttribute('data-month') || el.innerText || '';
      const m = text.match(/(20\d{2})\s*[년.\/-]\s*(\d{1,2})(?:\s*월|\b)/);
      if (m) months.add(`${m[1]}-${m[2].padStart(2,'0')}`);
    }
    const month = months.size === 1 ? [...months][0] : null;
    const nodes = [...root.querySelectorAll('[data-date],[data-day],[data-calendar-date],td,[role=gridcell]')];
    for (const el of nodes.filter(visible)) {
      let date = null;
      const attrs = ['data-date','data-day','data-calendar-date','aria-label','title'].map(k => el.getAttribute(k) || '');
      for (const value of attrs) {
        const m = value.match(/^(20\d{2})\s*[-.\/년]\s*(\d{1,2})\s*[-.\/월]\s*(\d{1,2})(?:일|\b)/);
        if (m) { date = `${m[1]}-${m[2].padStart(2,'0')}-${m[3].padStart(2,'0')}`; break; }
      }
      const text = (el.innerText || '').trim();
      if (!date && month && el.matches('td,[role=gridcell]') &&
          !/(other|outside|prev|next)[-_ ]?(month|date)|fc-day-other/i.test(el.className || '')) {
        const m = text.match(/^(\d{1,2})(?:일)?(?:\s|$)/);
        if (m) date = `${month}-${m[1].padStart(2,'0')}`;
      }
      if (date) {
        const titles = [...el.querySelectorAll('[title]')].map(x => x.title).join('\n');
        records.push({date, text: [text, titles].filter(Boolean).join('\n')});
      }
    }
  }
  return records;
}"""


def classify_leave(text: str) -> tuple[str, str]:
    # Action labels and cancelled/rejected requests are not active leave entries.
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    lines = [line for line in lines if not re.search(r"취소|반려|철회|잔여|휴가\s*신청$|연차\s*신청$", line)]
    content = " ".join(lines)
    compact = re.sub(r"\s+", "", content)
    if "반반차" in compact or "시간차" in compact or "시간연차" in compact:
        return "unknown_leave", "시간 단위 휴가 · 수동 확인"
    am = bool(re.search(r"오전.{0,8}(반차|휴가|연차)|(반차|휴가|연차).{0,8}오전", content))
    pm = bool(re.search(r"오후.{0,8}(반차|휴가|연차)|(반차|휴가|연차).{0,8}오후", content))
    if am and pm:
        return "vacation", "오전·오후 휴가"
    if am:
        return "am_half", "오전 반차"
    if pm:
        return "pm_half", "오후 반차"
    if "반차" in compact:
        return "unknown_leave", "반차 시간대 미확인"
    if re.search(r"휴가|연차|연가|병가|공가|경조|리프레시", content):
        return "vacation", "종일 휴가"
    if re.search(r"대체휴일|대체공휴일|휴무|공휴일|창립기념일", compact):
        return "holiday", "휴무 / 대체휴일"
    return "work", "근무일"


def parse_month(records: list[dict], year: int, month: int) -> dict:
    expected = {day.isoformat() for day in rules.month_dates(year, month)}
    grouped: dict[str, list[str]] = {}
    for record in records:
        value = str(record.get("date", ""))
        if value in expected:
            grouped.setdefault(value, []).append(str(record.get("text", "")))
    missing = expected - grouped.keys()
    if missing:
        raise RuntimeError(f"{year}년 {month}월 근태 달력을 확인하지 못했습니다 "
                           f"({len(grouped)}/{len(expected)}일). 본인 월별 근태 경로와 달력 영역을 확인하세요.")
    result = {}
    for value, texts in grouped.items():
        kind, label = classify_leave("\n".join(dict.fromkeys(texts)))
        result[value] = {"kind": kind, "label": label}
    return result


def read_month(page, cfg: dict, day: dt.date) -> dict:
    all_records = []
    for frame in page.frames:
        try:
            all_records.extend(frame.evaluate(EXTRACT_MONTH_JS, cfg.get("dooray_calendar_selector", "")))
        except Exception as exc:
            if cfg.get("dooray_calendar_selector"):
                raise RuntimeError("달력 영역 선택자를 읽지 못했습니다.") from exc
    return parse_month(all_records, day.year, day.month)
