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
                "daily": "temperature_2m_max,temperature_2m_min,weather_code",
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

        # 当天最低/最高温（daily 第 0 天即今天）
        daily = data.get("daily") or {}
        def _first(key):
            arr = daily.get(key) or []
            return arr[0] if arr else None
        t_max = _first("temperature_2m_max")
        t_min = _first("temperature_2m_min")
        d_code = _first("weather_code")

        weather_code = current.get("weather_code")
        if weather_code is None:
            weather_code = d_code

        return {
            "city": city,
            "temp": round(float(temp), 1),
            "temp_max": None if t_max is None else round(float(t_max), 1),
            "temp_min": None if t_min is None else round(float(t_min), 1),
            "weather_code": None if weather_code is None else int(weather_code),
            "rain": _rain_desc(float(precipitation), rain_prob),
            "rain_probability": rain_prob,
            "source": "open-meteo",
        }
    except Exception:
        return None


def _fetch_forecast(city: str, days: int = 7) -> dict | None:
    """调用 Open-Meteo 取未来 N 天逐日预报；失败返回 None。

    供「旅游目的地」场景使用：用户要去另一座城市，按当地未来一周的
    逐日最高/最低温与降水概率给出代表温度，再驱动推荐。
    """
    days = max(1, min(16, int(days or 7)))  # Open-Meteo 免费额度最多 16 天
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
                "daily": "temperature_2m_max,temperature_2m_min,"
                         "precipitation_probability_max,weather_code",
                "forecast_days": days,
                "timezone": "Asia/Shanghai",
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        daily = (resp.json().get("daily") or {})
        dates = daily.get("time") or []
        t_max = daily.get("temperature_2m_max") or []
        t_min = daily.get("temperature_2m_min") or []
        probs = daily.get("precipitation_probability_max") or []
        codes = daily.get("weather_code") or []
        if not dates or not t_max or not t_min:
            return None
        out_days = []
        for i, d in enumerate(dates):
            hi = t_max[i] if i < len(t_max) and t_max[i] is not None else None
            lo = t_min[i] if i < len(t_min) and t_min[i] is not None else None
            prob = probs[i] if i < len(probs) else None
            out_days.append({
                "date": d,
                "temp_max": round(float(hi), 1) if hi is not None else None,
                "temp_min": round(float(lo), 1) if lo is not None else None,
                "rain_probability": prob,
                "weather_code": int(codes[i]) if i < len(codes) and codes[i] is not None else None,
            })
        return {"city": city, "days": out_days, "source": "open-meteo-forecast"}
    except Exception:
        return None


def forecast_summary(forecast: dict, days: int | None = None) -> dict | None:
    """把逐日预报压缩成推荐可用的「代表温度 + 区间 + 降雨」。

    代表温度取各日 (max+min)/2 的均值，贴合 26℃ 穿衣法用的是体感均温；
    同时给出整段的最低温/最高温供文案标注区间，降雨取期间最大概率。
    """
    if not forecast or not forecast.get("days"):
        return None
    day_list = forecast["days"][:days] if days else forecast["days"]
    day_list = [d for d in day_list if d.get("temp_max") is not None and d.get("temp_min") is not None]
    if not day_list:
        return None
    mids = [(d["temp_max"] + d["temp_min"]) / 2 for d in day_list]
    rep = round(sum(mids) / len(mids), 1)
    t_min = min(d["temp_min"] for d in day_list)
    t_max = max(d["temp_max"] for d in day_list)
    probs = [d.get("rain_probability") for d in day_list if d.get("rain_probability") is not None]
    rain_prob = max(probs) if probs else None
    rain = "有降雨" if (rain_prob is not None and rain_prob >= 50) else "无降雨"
    return {
        "city": forecast.get("city", ""),
        "temp": rep,
        "temp_min": t_min,
        "temp_max": t_max,
        "rain": rain,
        "rain_probability": rain_prob,
        "source": forecast.get("source", "open-meteo-forecast"),
        "day_count": len(day_list),
        "start_date": day_list[0].get("date"),
        "end_date": day_list[-1].get("date"),
    }


@router.get("/forecast")
def get_forecast(city: str = Query(default="福州", description="城市名，如 厦门"),
                 days: int = Query(default=7, ge=1, le=16, description="预报天数，1-16")):
    """获取未来 N 天逐日预报（旅游场景）。

    返回 data.days 为逐日 {date,temp_max,temp_min,rain_probability,weather_code}；
    外部失败时 data 为 None（不伪造未来天气），source 标识来源。
    """
    city = (city or "").strip()
    if not city:
        return {"code": 400, "msg": "城市不能为空", "data": None}
    cache_key = f"forecast:{city}:{days}"
    now = time.time()
    cached = _cache.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL:
        return ok(data=cached[1] | {"cached": True})
    data = _fetch_forecast(city, days)
    if data is not None:
        _cache[cache_key] = (now, data)
        return ok(data=data)
    return ok(data={"city": city, "days": [], "source": "unavailable",
                    "message": "暂时无法获取该城市未来天气，请稍后再试"})


@router.get("")
def get_weather(city: str = Query(default="福州", description="城市名，如 上海")):
    """获取天气（温度 + 降雨）。

    支持任意城市名（含自由输入）：内置坐标表优先，未命中走 Open-Meteo Geocoding。

    返回 source 语义：
    - open-meteo      实时数据
    - mock(fallback)  外部服务失败/超时，返回降级样例
    - city_not_found  城市无法识别（拼写错误或不存在的城市），不返回伪造温度
    """
    city = (city or "").strip()
    if not city:
        return {"code": 400, "msg": "城市不能为空", "data": None}

    now = time.time()
    cached = _cache.get(city)
    if cached and now - cached[0] < _CACHE_TTL:
        return ok(data=cached[1] | {"cached": True})

    data = _fetch_weather(city)
    if data is not None:
        _cache[city] = (now, data)
        return ok(data=data)

    # 判断失败原因：能拿到坐标说明是外部服务问题，拿不到坐标说明城市名无法识别
    if _geocode_city(city) is None:
        return ok(data={
            "city": city,
            "temp": None,
            "rain": "未知",
            "source": "city_not_found",
            "message": "未识别该城市，请检查城市名称或换一个城市",
        })

    # 降级：返回 mock，source 标识非实时
    fallback = _MOCK_WEATHER | {"city": city, "source": "mock(fallback)"}
    return ok(data=fallback)
