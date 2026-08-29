"""机器人用户名生成（spec §1：半明牌——辨识度款 + 低调款两种风格）。"""
from __future__ import annotations

import random
from typing import List, Set

# 辨识度款：一眼 NPC，东方 flavor 的"职业/身份"
_NPC_ROLES = [
    "妖精女仆", "河童工匠", "天狗记者", "冰之妖精", "夜雀屋台", "红魔馆门卫",
    "地灵殿看门", "雾之湖渔夫", "竹林迷路人", "神社香客", "人里商贩", "冥界庭师",
    "守矢参拜客", "旧地狱矿工", "命莲寺扫地", "辉针城小人",
]

# 低调款：拼装出"像真人玩家"的 id
_LOWKEY_HEADS = [
    "苍", "雪村", "星夜", "风见", "云上", "湖畔", "竹林", "雾里", "白玉楼", "月见",
    "早苗厨", "帕秋莉的", "废怯", "摸鱼", "梦浮", "曲奇", "低语", "远野",
]
_LOWKEY_TAILS = [
    "散人", "收藏家", "观测者", "研究员", "小号", "书虫", "常客", "路人",
    "炼金术士", "投资人", "咸鱼", "信徒", "看板娘", "打工人", "自机", "亚空穴",
]


def generate_usernames(
    style: str, count: int, taken: Set[str], rng: random.Random
) -> List[str]:
    """生成 count 个不与 taken 冲突的用户名。style: "npc" | "lowkey"。"""
    out: List[str] = []
    guard = 0
    while len(out) < count:
        guard += 1
        if guard > count * 200:
            raise RuntimeError("用户名生成多次碰撞，请扩充词库或减少数量")
        if style == "npc":
            name = f"NPC·{rng.choice(_NPC_ROLES)}·{rng.randint(1, 99):02d}"
        else:
            name = f"{rng.choice(_LOWKEY_HEADS)}{rng.choice(_LOWKEY_TAILS)}"
            if rng.random() < 0.45:
                name += str(rng.randint(2, 9999))
        if name in taken:
            continue
        taken.add(name)
        out.append(name)
    return out
