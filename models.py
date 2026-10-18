from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
import re
from zoneinfo import ZoneInfo

SG = ZoneInfo('Asia/Singapore')


def now_sg():
    return datetime.now(SG)


def parse_time(value):
    value = value.strip().lower().replace(' ', '')
    match = re.fullmatch(r'(\d{1,2})(?::(\d{2}))?(am|pm)?', value)
    if not match:
        raise ValueError('时间格式应为 19:00 或 7pm。')
    hour, minute, suffix = int(match[1]), int(match[2] or 0), match[3]
    if suffix:
        if not 1 <= hour <= 12:
            raise ValueError('12 小时制的小时必须在 1 至 12 之间。')
        hour = hour % 12 + (12 if suffix == 'pm' else 0)
    if hour > 23 or minute > 59:
        raise ValueError('时间无效。')
    return f'{hour:02d}:{minute:02d}'


def parse_dates(value):
    parts = re.split(r'\s*(?:~|至|到|—|–)\s*', value.strip())
    if len(parts) == 1:
        parts *= 2
    if len(parts) != 2:
        raise ValueError('日期格式应为 2026-10-12~2026-10-18。')
    return parts


def parse_window(value):
    parts = re.split(r'\s*[-–—~至]\s*', value.strip())
    if len(parts) != 2:
        raise ValueError('时间段格式应为 19:00-22:00。')
    return [parse_time(part) for part in parts]


@dataclass(frozen=True)
class Config:
    activity: str
    venue: str
    date_from: str
    date_to: str
    time_from: str
    time_to: str
    interval_seconds: int = 600

    def validate(self, future=False, now=None):
        if not self.activity.strip() or len(self.activity) > 120 or not self.venue.strip() or len(self.venue) > 200:
            raise ValueError('请填写运动和场馆的官网英文名称。')
        try:
            first, last = date.fromisoformat(self.date_from), date.fromisoformat(self.date_to)
        except (TypeError, ValueError):
            raise ValueError('请填写有效的 YYYY-MM-DD 日期。') from None
        if first.isoformat() != self.date_from or last.isoformat() != self.date_to:
            raise ValueError('日期必须为 YYYY-MM-DD。')
        if not 0 <= (last - first).days <= 30:
            raise ValueError('日期范围最多包含 31 天，结束日期不能早于开始日期。')
        if any(not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', t) for t in (self.time_from, self.time_to)):
            raise ValueError('时间必须为 HH:mm。')
        if minutes(self.time_to) - minutes(self.time_from) < 60:
            raise ValueError('时间范围需在同一天内，且至少包含完整一小时。')
        if self.interval_seconds != 600:
            raise ValueError('本工具每 10 分钟查询一次。')
        if future and self.expired(now):
            raise ValueError('日期和时间已过期，请填写未来的监控条件。')
        return self

    def expired(self, now=None):
        end = datetime.fromisoformat(f'{self.date_to}T{self.time_to}').replace(tzinfo=SG)
        return (now or now_sg()) >= end

    def requested_days(self):
        current, last = date.fromisoformat(self.date_from), date.fromisoformat(self.date_to)
        while current <= last:
            yield current.isoformat()
            current += timedelta(days=1)

    def days(self, now=None):
        first, last = date.fromisoformat(self.date_from), date.fromisoformat(self.date_to)
        current = max(first, (now or now_sg()).date())
        while current <= last:
            yield current.isoformat()
            current += timedelta(days=1)

    def text(self):
        return f'{self.activity} / {self.venue}\n{self.date_from} 至 {self.date_to}\n{self.time_from}–{self.time_to}（新加坡时间）\n任意完整一小时，每 10 分钟查询。'

    def dict(self):
        return asdict(self)


def minutes(t):
    hour, minute = t.split(':')
    return int(hour) * 60 + int(minute)


def parse_set(text, now=None):
    parts = [p.strip() for p in text.strip().split('/')]
    if len(parts) != 4:
        raise ValueError('格式：/set Tennis / Kallang Tennis Centre / 2026-10-12~2026-10-18 / 19:00-22:00')
    first, last = parse_dates(parts[2])
    start, end = parse_window(parts[3])
    return Config(parts[0], parts[1], first, last, start, end).validate(future=True, now=now)


def slot_from_control(config, requested_date, control, now=None):
    if not control.get('visible') or control.get('disabled') or control.get('ariaDisabled') == 'true':
        return None
    try:
        stamp = int(control['value'])
    except (ValueError, TypeError, KeyError):
        raise ValueError('官网时段标识发生变化，本次查询不采用。') from None
    # The official form currently uses Unix milliseconds; accept Unix seconds too.
    if stamp > 100_000_000_000:
        stamp /= 1000
    try:
        start = datetime.fromtimestamp(stamp, SG)
    except (ValueError, OSError, OverflowError):
        raise ValueError('官网时段时间戳无效。') from None
    if control.get('timeText') and parse_time(control['timeText']) != start.strftime('%H:%M'):
        raise ValueError('官网显示时间与时段标识不一致，本次查询不采用。')
    end = start + timedelta(hours=1)
    if start.date().isoformat() != requested_date:
        raise ValueError('官网返回了其他日期的时段，本次查询不采用。')
    if start <= (now or now_sg()) or end.date() != start.date():
        return None
    start_text, end_text = start.strftime('%H:%M'), end.strftime('%H:%M')
    if minutes(start_text) < minutes(config.time_from) or minutes(end_text) > minutes(config.time_to):
        return None
    return {'date': requested_date, 'start': start_text, 'end': end_text, 'key': f'{requested_date}|{start_text}'}


UNAVAILABLE_REASONS = {
    'not_selectable': '官网暂不可选',
    'not_displayed': '官网未显示该日期',
    'disabled': '官网日期按钮不可点击',
    'past': '日期已过期',
}


def unavailable_dates(result):
    return {row['date']: UNAVAILABLE_REASONS.get(row['status'], '官网暂不可查询')
            for row in result.get('results', []) if row['status'] != 'checked'}


def _slot_rows(slots):
    by_date = {}
    for item in slots:
        by_date.setdefault(item['date'], {})[item['start']] = item.get('booking_type', '')
    return {day: day + ' ' + '、'.join(
        start + ('（抽签）' if booking_type == 'ballot' else '')
        for start, booking_type in sorted(times.items()))
        for day, times in sorted(by_date.items())}


def _pack_rows(rows, prefix=''):
    messages, current = [], prefix.rstrip()
    for row in rows:
        candidate = current + ('\n' if current else '') + row
        if len(candidate) > 3900 and current:
            messages.append(current)
            current = row
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def summaries(slots, unavailable=None):
    rows = _slot_rows(slots)
    rows.update({day: day + ' 不可查询（' + reason + '）'
                 for day, reason in (unavailable or {}).items()})
    return _pack_rows([rows[day] for day in sorted(rows)])


def query_summaries(result, prefix=''):
    rows = {row['date']: row['date'] + ' 无符合条件的时间点'
            for row in result.get('results', [])}
    rows.update(_slot_rows(result['matches']))
    rows.update({day: day + ' 不可查询（' + reason + '）'
                 for day, reason in unavailable_dates(result).items()})
    return _pack_rows([rows[day] for day in sorted(rows)] or ['未发现符合条件的可选时间点。'], prefix)
