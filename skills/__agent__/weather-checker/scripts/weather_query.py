#!/usr/bin/env python3
"""
天气查询工具 - 使用 wttr.in 免费天气服务
用法: python weather_query.py <城市名>
示例: python weather_query.py Beijing
      python weather_query.py 东京
"""

import sys
import json
import urllib.request
from datetime import datetime


def fetch_weather(city: str) -> dict:
    """获取城市天气数据"""
    url = f"https://wttr.in/{urllib.parse.quote(city)}?format=j1"
    req = urllib.request.Request(url, headers={"User-Agent": "WeatherChecker/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def format_weather_desc(code: str) -> str:
    """将天气代码转换为中文描述"""
    mapping = {
        "113": "晴天",
        "116": "多云",
        "119": "阴天",
        "122": "乌云",
        "143": "薄雾",
        "176": "局部小雨",
        "179": "局部小雪",
        "182": "局部雨夹雪",
        "185": "局部冻毛毛雨",
        "200": "可能有雷暴",
        "227": "吹雪",
        "230": "暴风雪",
        "248": "雾",
        "260": "冻雾",
        "263": "局部冻毛毛雨",
        "266": "毛毛雨",
        "281": "冻毛毛雨",
        "284": "大冻毛毛雨",
        "293": "局部小雨",
        "296": "小雨",
        "299": "有时中雨",
        "302": "中雨",
        "305": "有时大雨",
        "308": "大雨",
        "311": "小冻雨",
        "314": "中或大冻雨",
        "317": "小雨夹雪",
        "320": "中或大雨夹雪",
        "323": "局部雨夹雪",
        "326": "小雪",
        "329": "中雪",
        "332": "大雪",
        "335": "局部大雪",
        "338": "大暴雪",
        "350": "小冻雨",
        "353": "局部毛毛雨",
        "356": "中或大毛毛雨",
        "359": "大雨毛毛雨",
        "362": "小雨夹雪",
        "365": "中或大雨夹雪",
        "368": "小雪",
        "371": "中或大雪",
        "374": "小雨夹雪",
        "377": "中或大雨夹雪",
        "386": "局部小雨",
        "389": "有时中雨",
        "392": "小雪",
        "395": "中或大雪",
    }
    return mapping.get(code, "未知")


def format_uv_index(uv: int) -> str:
    """格式化紫外线指数"""
    if uv <= 2:
        return f"{uv} (弱)"
    elif uv <= 5:
        return f"{uv} (中等)"
    elif uv <= 7:
        return f"{uv} (强)"
    elif uv <= 10:
        return f"{uv} (很强)"
    else:
        return f"{uv} (极强)"


def format_wind_direction(deg: str) -> str:
    """将风向角度转换为中文"""
    directions = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]
    idx = round(int(deg) / 45) % 8
    return directions[idx]


def print_weather(city: str, data: dict):
    """格式化输出天气信息"""
    current = data["current_condition"][0]
    weather = data["weather"][0]

    # 当前天气
    print(f"\n{'='*40}")
    print(f"  📍 {city} 天气")
    print(f"  📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*40}")
    print(f"  🌡️  当前温度: {current['temp_C']}°C ({current['temp_F']}°F)")
    print(f"  🌤️  天气状况: {format_weather_desc(current['weatherCode'])}")
    print(f"  💧  湿度: {current['humidity']}%")
    print(f"  💨  风速: {current['windspeedKmph']} km/h ({current['winddir16Point']})")
    print(f"  👁️  能见度: {current['visibility']} km")
    print(f"  📊  气压: {current['pressure']} hPa")
    print(f"  ☀️  紫外线指数: {format_uv_index(int(current['uvIndex']))}")

    # 今日预报
    print(f"\n  {'─'*36}")
    print(f"  📅 今日预报")
    print(f"  {'─'*36}")
    print(f"  最高温度: {weather['maxtempC']}°C")
    print(f"  最低温度: {weather['mintempC']}°C")
    print(f"  平均温度: {weather['avgtempC']}°C")

    # 小时预报（取几个关键时段）
    print(f"\n  {'─'*36}")
    print(f"  ⏰ 分时段预报")
    print(f"  {'─'*36}")
    for hour in [8, 14, 20]:
        time_str = f"{hour * 100:04d}"
        for h in weather.get("hourly", []):
            if h["time"] == time_str:
                hour_time = int(h["time"]) / 100
                print(f"  {hour_time:0.0f}:00  {format_weather_desc(h['weatherCode'])} "
                      f"{h['tempC']}°C  湿度{h['humidity']}%  "
                      f"降水{h['precipMM']}mm")
                break

    print(f"\n{'='*40}\n")


def main():
    if len(sys.argv) < 2:
        print("用法: python weather_query.py <城市名>")
        print("示例: python weather_query.py Beijing")
        print("      python weather_query.py 东京")
        sys.exit(1)

    city = sys.argv[1]
    try:
        data = fetch_weather(city)
        print_weather(city, data)
    except Exception as e:
        print(f"❌ 查询失败: {e}")
        print("请检查城市名是否正确，或网络连接是否正常。")
        sys.exit(1)


if __name__ == "__main__":
    main()
