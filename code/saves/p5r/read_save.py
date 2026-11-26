#!/usr/bin/env python3
"""P5R 存档只读：给 AI 看的进度卡。不写存档、不碰游戏。
用法: read_save.py <DATA.DAT> [--json]
解析借 change-of-heart (MIT) 的 core/，偏移表见其 core/editor.py。"""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "change-of-heart"))
from core.editor import SaveEditor

CONF_ZH = {
    "Fool": "愚者·伊格尔", "Magician": "魔术师·摩尔加纳", "Priestess": "女教皇·真",
    "Empress": "女皇·春", "Emperor": "皇帝·佑介", "Hierophant": "教皇·总治郎",
    "Lovers": "恋爱·杏", "Chariot": "战车·龙司", "Justice": "正义·明智",
    "Hermit": "隐者·双叶", "Fortune": "命运·千早", "Strength": "力量·双子",
    "Hanged Man": "倒吊人·岩井", "Death": "死神·武见", "Temperance": "节制·川上",
    "Devil": "恶魔·大宅", "Tower": "塔·信也", "Star": "星·一二三",
    "Moon": "月亮·三岛", "Sun": "太阳·吉田", "Judgement": "审判·冴",
    "Faith": "信念·芳泽霞", "Councillor": "顾问官·丸喜",
}
SOCIAL_ZH = {"Knowledge": "知识", "Charm": "魅力", "Proficiency": "灵巧", "Guts": "勇气", "Kindness": "体贴"}
PARTY_ZH = {"Joker": "主角", "Ryuji": "龙司", "Morgana": "摩尔加纳", "Ann": "杏", "Yusuke": "佑介",
            "Makoto": "真", "Haru": "春", "Futaba": "双叶", "Akechi": "明智", "Kasumi": "芳泽霞"}
SOCIAL_MAX = {"Knowledge": 192, "Charm": 132, "Proficiency": 87, "Guts": 113, "Kindness": 136}


def read(path):
    ed = SaveEditor()
    with open(path, "rb") as f:
        ed.load_from_bytes(f.read())
    if not ed.is_real_save():
        raise SystemExit("不是 PC 版 P5R 存档（payload 不是 0x31）")
    qi = ed.get_quick_info()
    out = {
        "file": os.path.abspath(path),
        "mtime": int(os.path.getmtime(path)),
        "day": qi.get("day"), "level": qi.get("level"), "playtime_h": round((qi.get("playtime") or 0) / 3600, 1),
        "difficulty": qi.get("difficulty"), "money": ed.get_money(),
        "social": {}, "confidants": {}, "party": [],
    }
    for k, v in ed.get_social_stats().items():
        out["social"][SOCIAL_ZH[k]] = {"rank": v["rank"], "points": v["points"], "max": v["points"] >= SOCIAL_MAX[k]}
    for k, v in ed.get_confidant_ranks().items():
        out["confidants"][CONF_ZH[k]] = {"rank": v["rank"], "points": v["points"]}
    for p in ed.get_party_stats():
        if p["joined"]:
            out["party"].append({"name": PARTY_ZH.get(p["name"], p["name"]), "level": p["level"], "hp": p["hp"], "sp": p["sp"]})
    return out


def card(o):
    lines = [f"【P5R 存档】{o['day']}　Lv{o['level']}　{o['difficulty']}　游戏时长 {o['playtime_h']}h　钱 ¥{o['money']:,}"]
    lines.append("五维：" + "  ".join(f"{k}{'MAX' if v['max'] else v['rank']}({v['points']})" for k, v in o["social"].items()))
    lines.append("队伍：" + "  ".join(f"{p['name']}{p['level']}" for p in o["party"]))
    conf = sorted(o["confidants"].items(), key=lambda kv: -kv[1]["rank"])
    lines.append("同伴：" + "  ".join(f"{k}{v['rank']}" + ("" if v["rank"] else "(未开)") for k, v in conf))
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    o = read(sys.argv[1])
    print(json.dumps(o, ensure_ascii=False, indent=1) if "--json" in sys.argv else card(o))
