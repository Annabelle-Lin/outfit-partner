# -*- coding: utf-8 -*-
"""天气模块（计划书 13.3）—— Open-Meteo 实时天气。

GET /weather       获取默认城市天气
GET /weather?city= 获取指定城市天气

数据源：Open-Meteo（https://open-meteo.com/，CC BY 4.0）。
零密钥、非商用免费；接口返回的数据如需公开展示，请署名
"Weather data by Open-Meteo.com"。

策略：
- 常用中国城市走内置坐标表，未知城市走 Open-Meteo Geocoding API；
- 内存缓存 30 分钟，避免频繁打外部 API；
- 外部服务失败/超时时降级返回 mock 数据，source 字段标识来源，
  前端可据此提示「本次未使用实时天气」。
"""
import time

import httpx
from fastapi import APIRouter, Query

from app.api import ok

router = APIRouter()

_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_CACHE_TTL = 30 * 60  # 30 分钟缓存
_TIMEOUT = 8.0

# 常用城市坐标表（纬度, 经度），覆盖产品说明中的可选城市
_CITY_COORDS = {
    "北京": (39.9042, 116.4074),
    "上海": (31.2304, 121.4737),
    "广州": (23.1291, 113.2644),
    "深圳": (22.5431, 114.0579),
    "杭州": (30.2741, 120.1551),
    "成都": (30.5728, 104.0668),
    "武汉": (30.5928, 114.3055),
    "南京": (32.0603, 118.7969),
    "西安": (34.3416, 108.9398),
    "福州": (26.0745, 119.2965),
    "厦门": (24.4798, 118.0894),
    "重庆": (29.5630, 106.5516),
    "天津": (39.0842, 117.2010),
    "苏州": (31.2989, 120.5853),
    "长沙": (28.2282, 112.9388),
    "青岛": (36.0671, 120.3826),
    "昆明": (24.8801, 102.8329),
    "哈尔滨": (45.8038, 126.5349),
    "沈阳": (41.8057, 123.4315),
    "郑州": (34.7466, 113.6254),
}

# mock 兜底数据：外部服务失败时降级返回
_MOCK_WEATHER = {
    "city": "福州",
    "temp": 22.0,
    "rain": "无降雨",
    "source": "mock",
}

# 内存缓存：{city: (timestamp, data)}
_cache: dict[str, tuple[float, dict]] = {}


def _geocode_city(city: str) -> tuple[float, float] | None:
    """城市名 -> 经纬度。先查内置表，再调 Open-Meteo Geocoding API。"""
    if city in _CITY_COORDS:
        return _CITY_COORDS[city]
    try:
        resp = httpx.get(
            _GEOCODE_URL,
            params={"name": city, "count": 1, "language": "zh", "format": "json"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
        if results:
            return results[0]["latitude"], results[0]["longitude"]
    except Exception:
        pass
    return None


def _rain_desc(precipitation: float, rain_prob: float | None) -> str:
    """把降水量/降水概率映射为前端友好的文案。"""
    if precipitation <= 0:
        return "无降雨"
    if precipitation < 2.5:
        return "小雨"
    if precipitation < 8:
        return "中雨"
    return "大雨"


def _fetch_weather(city: str) -> dict | None:
    """调用 Open-Meteo 获取当前天气；失败返回 None。"""
    coords = _geocode_city(city)
    if not coords:
        return None
    lat, lon = coords
    try:
        resp = httpx.get(
            _FORECAST_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,precipitation,weather_code",
                "hourly": "precipitation_probability",
                "forecast_days": 1,
                "timezone": "Asia/Shanghai",
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        current = data.get("current") or {}
        temp = current.get("temperature_2m")
        if temp is None:
            return None
        precipitation = current.get("precipitation", 0.0) or 0.0
        probs = (data.get("hourly") or {}).get("precipitation_probability") or []
        rain_prob = probs[0] if probs else None
        return {
            "city": city,
            "temp": round(float(temp), 1),
            "rain": _rain_desc(float(precipitation), rain_prob),
            "rain_probability": rain_prob,
            "source": "open-meteo",
        }
    except Exception:
        return None


@router.get("")
def get_weather(city: str = Query(default="福州", description="城市名，如 上海")):
    """获取天气（温度 + 降雨）。优先 Open-Meteo 实时数据，失败降级 mock。"""
    now = time.time()
    cached = _cache.get(city)
    if cached and now - cached[0] < _CACHE_TTL:
        return ok(data=cached[1] | {"cached": True})

    data = _fetch_weather(city)
    if data is not None:
        _cache[city] = (now, data)
        return ok(data=data)

    # 降级：返回 mock，source 标识非实时
    fallback = _MOCK_WEATHER | {"city": city, "source": "mock(fallback)"}
    return ok(data=fallback)
