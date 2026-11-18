"""Explicit Python wall-clock and locale settings for an isolated worker."""
import datetime
import locale
import os
import time
from zoneinfo import ZoneInfo


def clock_settings(instant=None, *, timezone=None, locale_name=None):
    if instant is None:
        if timezone is not None or locale_name is not None:
            raise ValueError('设置区域或时区时，请同时提供带 UTC 偏移的固定时间。')
        return None
    if not isinstance(instant, str) or len(instant) > 48:
        raise ValueError('固定时间须为带 UTC 偏移的 ISO 8601 时间。')
    instant = datetime.datetime.fromisoformat(instant)
    if instant.utcoffset() is None:
        raise ValueError('固定时间须带 UTC 偏移。')
    for value in (timezone, locale_name):
        if value is not None and (not isinstance(value, str) or not value or len(value) > 120 or not value.isprintable()):
            raise ValueError('区域或时区设置无效。')
    return {'instant': instant.astimezone(datetime.timezone.utc).isoformat(),
            'timezone': timezone, 'locale': locale_name}


def validate_clock(value):
    if value is not None and (not isinstance(value, dict) or value.keys() != {'instant', 'timezone', 'locale'}
                              or clock_settings(value['instant'], timezone=value['timezone'],
                                                locale_name=value['locale']) != value):
        raise ValueError('固定时钟记录无效。')


def freeze_worker_clock(settings):
    validate_clock(settings)
    zone = settings['timezone']
    if zone is not None:
        ZoneInfo(zone)
        if not hasattr(time, 'tzset'):
            raise ValueError('当前 Python 不能设置进程级时区。')
        os.environ['TZ'] = zone
        time.tzset()
    if name := settings['locale']:
        locale.setlocale(locale.LC_ALL, name)
    instant = datetime.datetime.fromisoformat(settings['instant'])
    stamp = instant.timestamp()
    nanos = ((instant - datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc))
             // datetime.timedelta(microseconds=1)) * 1000
    raw_localtime, raw_gmtime, raw_ctime = time.localtime, time.gmtime, time.ctime

    class FixedDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(stamp, tz)

        @classmethod
        def utcnow(cls):
            return cls.fromtimestamp(stamp, datetime.timezone.utc).replace(tzinfo=None)

        @classmethod
        def today(cls):
            return cls.now()

    class FixedDate(datetime.date):
        @classmethod
        def today(cls):
            return cls.fromtimestamp(stamp)

    FixedDateTime.__name__ = 'datetime'
    FixedDate.__name__ = 'date'
    datetime.datetime = FixedDateTime
    datetime.date = FixedDate
    time.time = lambda: stamp
    time.time_ns = lambda: nanos
    time.localtime = lambda seconds=None: raw_localtime(stamp if seconds is None else seconds)
    time.gmtime = lambda seconds=None: raw_gmtime(stamp if seconds is None else seconds)
    time.ctime = lambda seconds=None: raw_ctime(stamp if seconds is None else seconds)
    if hasattr(time, 'clock_gettime') and hasattr(time, 'CLOCK_REALTIME'):
        clock, clock_ns = time.clock_gettime, time.clock_gettime_ns
        time.clock_gettime = lambda kind: stamp if kind == time.CLOCK_REALTIME else clock(kind)
        time.clock_gettime_ns = lambda kind: nanos if kind == time.CLOCK_REALTIME else clock_ns(kind)
