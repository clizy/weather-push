#!/usr/bin/env python3
"""每天定时把当天天气压缩成一句话，通过微信公众平台测试号的模板消息推到微信。

数据源：和风天气新版 API（/weather/v1/daily，需要项目 API Host + API KEY）
推送通道：微信公众平台测试号（官方免费、无需实名、无需企业资质）
运行方式：本机 python3 直接跑，或 GitHub Actions 定时跑（secrets 注入凭据）

用法：
    python3 weather_push.py            # 查天气 + 推送微信
    python3 weather_push.py --dry-run  # 只查天气并打印，不推送

配置优先级：环境变量 > config.json，便于在 CI 里用 secrets 覆盖敏感信息。
"""

import gzip
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
# 本地敏感配置（密钥），已被 .gitignore 排除，CI 上通过环境变量注入
LOCAL_CONFIG_PATH = os.path.join(BASE_DIR, "config.local.json")
LOG_PATH = os.path.join(BASE_DIR, "logs", "weather_push.log")

# 不同系统的根证书路径不同，逐个探测；都找不到就交给 Python 默认逻辑
CA_CANDIDATES = [
    "/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
]
CA_FILE = next((path for path in CA_CANDIDATES if os.path.exists(path)), None)
SSL_CONTEXT = ssl.create_default_context(cafile=CA_FILE) if CA_FILE else ssl.create_default_context()

# 环境变量 -> config.json 里的位置
ENV_OVERRIDES = {
    "QWEATHER_KEY": ("qweather", "key"),
    "QWEATHER_HOST": ("qweather", "api_host"),
    "WECHAT_APPID": ("wechat_mp", "appid"),
    "WECHAT_APPSECRET": ("wechat_mp", "appsecret"),
    "WECHAT_OPENID": ("wechat_mp", "openid"),
    "WECHAT_TEMPLATE_ID": ("wechat_mp", "template_id"),
}


def log(message):
    """同时输出到控制台和日志文件，方便排查定时任务的问题。"""
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def merge_config(base, extra):
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge_config(base[key], value)
        else:
            base[key] = value
    return base


def load_config():
    config = {}
    for path in (CONFIG_PATH, LOCAL_CONFIG_PATH):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                merge_config(config, json.load(f))
    for env_key, (section, field) in ENV_OVERRIDES.items():
        value = os.environ.get(env_key)
        if value:
            config.setdefault(section, {})[field] = value.strip()
    return config


def api_get(url, headers=None):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "weather-push/1.0", "Accept-Encoding": "gzip", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=20, context=SSL_CONTEXT) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "ignore").strip()[:300]
        # 网关/代理返回的非 JSON 响应没有排查价值，只保留状态码
        detail = f"：{body}" if body.startswith(("{", "[")) else ""
        raise RuntimeError(f"请求失败 HTTP {error.code}{detail}") from error

    # 和风天气会返回 gzip 压缩内容，urllib 不会自动解压
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return json.loads(body.decode("utf-8"))


def qweather_get(path, query, qweather_config):
    host = (qweather_config.get("api_host") or "").strip()
    key = (qweather_config.get("key") or "").strip()
    if not host:
        raise RuntimeError("没有配置和风天气 API Host（qweather.api_host 或 QWEATHER_HOST）")
    if not key:
        raise RuntimeError("没有配置和风天气 API KEY（qweather.key 或 QWEATHER_KEY）")
    params = urllib.parse.urlencode({**query, "key": key})
    return api_get(f"https://{host}{path}?{params}")


def fetch_weather(latitude, longitude, qweather_config):
    """取今日预报（新版 API 返回 daytime / nighttime 两段）。"""
    result = qweather_get(
        f"/weather/v1/daily/{latitude}/{longitude}",
        {"days": 1, "localTime": "true"},
        qweather_config,
    )
    # 新版接口的预报数组字段名是 days（旧版 v7 才是 daily）
    days = result.get("days") or []
    if not days:
        raise RuntimeError(f"和风天气返回数据为空：{str(result)[:200]}")
    return days[0]


def build_weather(city_name, daily):
    """返回 (一句话摘要, 模板消息字段)。"""
    daytime = daily.get("daytime") or {}
    nighttime = daily.get("nighttime") or {}

    text_day = (daytime.get("condition") or {}).get("text", "")
    text_night = (nighttime.get("condition") or {}).get("text", "")
    weather_text = text_day if text_day == text_night or not text_night else f"{text_day}转{text_night}"

    low = round(float(daily["temperatureMin"]["value"]))
    high = round(float(daily["temperatureMax"]["value"]))
    # 新版接口的降水概率是 0~1 的小数
    rain_chance = round(
        max(
            (daytime.get("precipitation") or {}).get("probability") or 0,
            (nighttime.get("precipitation") or {}).get("probability") or 0,
        )
        * 100
    )
    wind_scale = (daytime.get("wind") or {}).get("scale") or 0
    hint = make_hint(weather_text, low, high, rain_chance)

    summary = f"{datetime.now():%m月%d日} {city_name}：{weather_text}，{low}~{high}℃，降水概率 {rain_chance}%"
    if wind_scale >= 5:
        summary += f"，风力 {wind_scale} 级"
    summary += f"。{hint}。"

    fields = {
        "date": f"{datetime.now():%m月%d日}",
        "city": city_name,
        "weather": weather_text,
        "temp": f"{low}~{high}℃",
        "rain": f"{rain_chance}%",
        "tip": hint + "。" + (f"风力 {wind_scale} 级。" if wind_scale >= 5 else ""),
    }
    return summary, fields


def make_hint(text, low, high, rain_chance):
    """一句生活提示，优先命中越靠前的规则。"""
    if any(word in text for word in ("雪", "冻")):
        return "路面可能湿滑，注意保暖"
    if rain_chance >= 50 or any(word in text for word in ("雨", "雷")):
        return "出门记得带伞"
    if any(word in text for word in ("霾", "沙", "尘")):
        return "空气质量一般，外出可戴口罩"
    if high - low >= 10:
        return "昼夜温差大，注意添减衣物"
    if high >= 33:
        return "天气炎热，注意防晒补水"
    if high <= 5:
        return "天气寒冷，注意保暖"
    return "天气不错，适合外出"


def get_access_token(appid, appsecret):
    params = urllib.parse.urlencode(
        {"grant_type": "client_credential", "appid": appid, "secret": appsecret}
    )
    result = api_get(f"https://api.weixin.qq.com/cgi-bin/token?{params}")
    if "access_token" not in result:
        raise RuntimeError(f"获取 access_token 失败：{result}")
    return result["access_token"]


def push_to_wechat(wechat_config, fields):
    """用测试号模板消息推送给关注的用户。"""
    missing = [
        key
        for key in ("appid", "appsecret", "openid", "template_id")
        if not wechat_config.get(key)
    ]
    if missing:
        raise RuntimeError(f"缺少微信配置字段：{', '.join(missing)}")

    access_token = get_access_token(wechat_config["appid"], wechat_config["appsecret"])
    payload = {
        "touser": wechat_config["openid"],
        "template_id": wechat_config["template_id"],
        "data": {key: {"value": value} for key, value in fields.items()},
    }
    request = urllib.request.Request(
        f"https://api.weixin.qq.com/cgi-bin/message/template/send?access_token={access_token}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20, context=SSL_CONTEXT) as response:
        result = json.load(response)
    if result.get("errcode") != 0:
        raise RuntimeError(f"模板消息发送失败：{result}")
    return result


def main():
    dry_run = "--dry-run" in sys.argv
    config = load_config()
    locations = config.get("locations") or []
    if not locations:
        log("执行失败：config.json 里没有配置 locations")
        sys.exit(1)

    qweather_config = config.get("qweather", {})
    failed = 0
    for location in locations:
        city_name = location.get("display_name") or f"{location.get('latitude')},{location.get('longitude')}"
        try:
            daily = fetch_weather(location["latitude"], location["longitude"], qweather_config)
            summary, fields = build_weather(city_name, daily)
            log(f"天气：{summary}")

            if dry_run:
                log(f"dry-run 模式，跳过推送。字段：{json.dumps(fields, ensure_ascii=False)}")
                continue

            push_to_wechat(config.get("wechat_mp", {}), fields)
            log(f"{city_name} 推送成功")
        except Exception as error:  # 单个城市失败不影响其他城市
            log(f"{city_name} 执行失败：{error}")
            failed += 1

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
