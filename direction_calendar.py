"""Pure calendar rules for the existing ETH chart; no orders or network calls.

This reproduces the chart's existing month-index logic, including index 11
after Dec 7 and before Feb 4. It is not a corrected calendrical calculation.
All returned dates use UTC+08:00. Input datetimes must include a timezone.
"""
from datetime import datetime, timedelta, timezone
from functools import lru_cache

BEIJING = timezone(timedelta(hours=8))
DAY = timedelta(days=1)
TWO_HOURS = timedelta(hours=2)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
RULE_ID = 'yijing-midpoint-v1'
RULE_LABEL = '红绿带中点方向限制'
BOUNDARIES = ((2, 4), (3, 6), (4, 5), (5, 6), (6, 6), (7, 7),
              (8, 7), (9, 8), (10, 8), (11, 7), (12, 7), (1, 6))


def beijing(t):
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError('Timestamp must include a timezone; use +08:00 for Beijing.')
    return t.astimezone(BEIJING)


def month_index(t):
    t = beijing(t)
    index = 11
    for i, boundary in enumerate(BOUNDARIES):
        if (t.month, t.day) < boundary:
            index = i - 1 if i else 11
            break
    return index


def band_color(t):
    t = beijing(t)
    stem = (2 * ((t.year - 4) % 5) + 2 + month_index(t)) % 10
    return 'red' if stem <= 5 else 'green' if stem >= 8 else 'neutral'


@lru_cache(maxsize=16)
def switch_events(year):
    """Return complete band centers surrounding year; outer clipped bands omitted."""
    day = datetime(year - 2, 1, 1, tzinfo=BEIJING)
    stop = datetime(year + 3, 1, 1, tzinfo=BEIJING)
    bands = []
    while day < stop:
        color = band_color(day)
        if not bands or color != bands[-1][2]:
            bands.append([day, day + DAY, color])
        else:
            bands[-1][1] = day + DAY
        day += DAY
    events = []
    for start, end, color in bands[1:-1]:
        if color == 'neutral':
            continue
        center = start + (end - start) / 2
        # First 2H candle starting at/after center, then its close: same as backtest.
        elapsed = center - EPOCH
        steps, remainder = divmod(elapsed, TWO_HOURS)
        switch = EPOCH + (steps + bool(remainder) + 1) * TWO_HOURS
        events.append((switch.astimezone(BEIJING), 1 if color == 'green' else -1,
                       center, start, end))
    return tuple(events)


def direction(t):
    """+1: long only or flat; -1: short only or flat. Boundary includes new side."""
    t = beijing(t)
    eligible = [event for event in switch_events(t.year) if event[0] <= t]
    return eligible[-1][1]


def allow_increase(t, position_side):
    """Gate opening/adding a LONG or SHORT position; not a buy/sell order-side test.

    This gate must not block reducing/closing positions. Independent strategy and
    risk checks must also pass before any order is sent.
    """
    if position_side not in ('long', 'short'):
        return False
    return direction(t) == (1 if position_side == 'long' else -1)


def permitted_target(t, requested_net_position):
    """Project a requested signed position onto the permitted side; zero is allowed.

    In hedge mode, check long and short legs separately; a zero net can hide both.
    This returns a target only. It does not close or open any real position.
    """
    side = direction(t)
    return side * max(0.0, side * requested_net_position)


def event_window(start, end):
    """Calendar events only; includes the predecessor to establish initial state."""
    start, end = beijing(start), beijing(end)
    if end <= start:
        raise ValueError('结束时间必须晚于开始时间')
    events = {row[0]: row for year in range(start.year, end.year + 1)
              for row in switch_events(year)}
    ordered = sorted(events.values())
    previous = [row for row in ordered if row[0] <= start][-1:]
    return previous + [row for row in ordered if start < row[0] < end]


def direction_array(timestamps):
    """Vectorized UTC-millisecond lookup, including the new side at a boundary."""
    import numpy as np
    ts = np.asarray(timestamps, dtype=np.int64)
    if ts.ndim != 1 or not len(ts) or np.any(np.diff(ts) < 0):
        raise ValueError('方向日历需要递增的UTC毫秒时间戳')
    rows = event_window(datetime.fromtimestamp(int(ts[0])/1000, BEIJING),
                        datetime.fromtimestamp(int(ts[-1])/1000, BEIJING) + timedelta(milliseconds=1))
    when = np.array([int(row[0].timestamp()*1000) for row in rows], dtype=np.int64)
    values = np.array([row[1] for row in rows], dtype=np.int8)
    index = np.searchsorted(when, ts, side='right')-1
    if np.any(index < 0):
        raise ValueError('未找到起点之前的方向事件，拒绝猜测方向')
    result = values[index]
    result.flags.writeable = False
    return result
