"""Supported per-category and global best-ranking export sizes."""

TOP_LIMITS = (5000, 10000)


def top_limit(settings=None):
    if settings is not None and not isinstance(settings, dict):
        raise ValueError("排行榜设置必须是对象")
    value = (settings or {}).get("最优名额", 5000)
    if type(value) is not int or value not in TOP_LIMITS:
        raise ValueError("最优榜导出数量只能选择5000或10000")
    return value
