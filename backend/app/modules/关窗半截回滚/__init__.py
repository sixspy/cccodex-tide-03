"""关窗半截回滚（已实现）

选定形态：**形态一·窗封印指纹回退**。
关窗事务中途失败时，本趟已写片与已物化缓冲全部退回趟前；封印账留下失败指纹
（有效=0），收口回执写「回退」，贯验改读空片集。切片账、入窗缓冲、窗贯验、
收口四处共用 `app.services.收口` 的同一套「现行片集」谓词。

契约：
- 关窗事务中途失败时缓冲命运整批回滚，不留半物化；
- 两位同时关窗有互斥（BEGIN IMMEDIATE），只会成片一次；
- 收口回执与页面片集合同票；
- 每次关窗前先清场上一趟残片，避免脏读半窗片；
- 不改坏基线默认关窗 happy path。

只读收口接口：GET /api/关窗/收口、/api/关窗/快照、/api/关窗/残片。
"""

NAME = "关窗半截回滚"
CONTRACT = "关窗事务中途失败时缓冲命运整批回滚，不留半物化"
形态 = "形态一·窗封印指纹回退"

HOOKS = (
    "before_load",
    "after_load",
    "decide_window",
    "before_close",
    "after_close",
    "extra_verify_checks",
)


def describe() -> dict:
    return {
        "空桩": NAME,
        "契约": CONTRACT,
        "形态": 形态,
        "钩子": list(HOOKS),
        "默认启用": True,
        "说明": "已接线：关窗服务取互斥并整趟提交，失败回退并留失败指纹；贯验改读现行片集",
        "只读接口": ["/api/关窗/收口", "/api/关窗/快照", "/api/关窗/残片"],
    }


def before_load(batch: dict) -> dict:
    """装载前：可改批次或抛出预检错误。默认透传。"""
    return batch


def after_load(report: dict) -> dict:
    """装载后：可附加旁路统计。默认透传。"""
    return report


def decide_window(event: dict, rules: dict) -> str:
    """返回 入缓冲 / 迟到 / 驳回。基线默认由装载服务按事件时判定。"""
    return "入缓冲"


def before_close(window_id: str) -> None:
    """关窗前检查，可抛错阻断。默认放行；残片由关窗服务在同一写事务内清场。"""
    return None


def after_close(report: dict) -> dict:
    """关窗后处理，例如推进下一窗。默认透传。"""
    return report


def extra_verify_checks(snapshot: dict) -> list[str]:
    """附加贯验检查：收口与现行片集须同票，且不得有半窗残片。"""
    from app.services import 收口

    错: list[str] = []
    收 = (snapshot or {}).get("收口") or 收口.当前收口()
    现行 = (snapshot or {}).get("现行片")
    if 现行 is None:
        现行 = 收口.现行片()
    if int(收.get("现行片数") or 0) != len(现行):
        错.append(
            f"收口与片集不同票：现行片数={收.get('现行片数')} 实读={len(现行)}"
        )
    if 收.get("形态") == "回退" and 现行:
        错.append(f"回退形态却有现行片 {len(现行)} 枚")
    残 = 收口.残片()
    if 残:
        错.append(f"残留半窗片 {len(残)} 枚：{', '.join(r['切片编号'] for r in 残[:3])}")
    return 错
