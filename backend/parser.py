import re
from datetime import datetime

STATION = re.compile(r"^[A-Z]{2}\d{2}$")
FLAGS = {"A": "实测", "M": "插补", "B": "可疑"}


def parse_telegram_line(line: str):
    """
    返回 (parsed, reason)。
    时刻段为空时 parsed 里 observed_at 为 None，reason 明确给出空时刻，
    由服务端裁决为“观测时刻空着，不参与去重”，不进入任何站时刻去重。
    """
    parts = [part.strip() for part in line.strip().split("|")]
    if len(parts) != 4:
        return None, "列数不是四段"
    station, stamp, tide, flag = parts
    if not STATION.match(station):
        return None, "站码应为两位字母加两位数字"
    if stamp == "":
        # 只标空时刻，不在这里判失败：由服务端去重裁决定性并记录站码
        return {
            "station_code": station,
            "observed_at": None,
            "tide_cm": None,
            "quality_flag": None,
            "flag_label": None,
        }, None
    try:
        observed = datetime.strptime(stamp, "%Y%m%d%H%M")
    except ValueError:
        return None, "时间不是十二位数字"
    if not re.fullmatch(r"-?\d+", tide):
        return None, "潮位不是整数厘米"
    centimeters = int(tide)
    if centimeters < -500 or centimeters > 2000:
        return None, "潮位超出量程"
    if flag not in FLAGS:
        return None, "质量旗不在 A、M、B"
    return {
        "station_code": station,
        "observed_at": observed,
        "tide_cm": centimeters,
        "quality_flag": flag,
        "flag_label": FLAGS[flag],
    }, None
