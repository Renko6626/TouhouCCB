"""PvE 机器人用户名生成——直接测纯函数，不走 DB。"""
import random
import re

import pytest

from app.services.pve.naming import _PHRASE_ADV, generate_usernames


def test_generate_usernames_npc_format():
    names = generate_usernames("npc", 20, set(), random.Random(1))
    assert len(names) == 20
    assert len(set(names)) == 20
    assert all(re.fullmatch(r"NPC·.+·\d{2}", n) for n in names)


def test_generate_usernames_lowkey_format():
    names = generate_usernames("lowkey", 20, set(), random.Random(1))
    assert len(names) == 20
    assert len(set(names)) == 20


def test_generate_usernames_phrase_format():
    """三期新增：形容词的名词（副词）动词句式款，副词可省略。"""
    names = generate_usernames("phrase", 50, set(), random.Random(1))
    assert len(names) == 50
    assert len(set(names)) == 50  # 大词池，50 个不该撞车
    for n in names:
        adj, rest = n.split("的", 1)
        assert adj  # 形容词非空
        assert rest  # 名词(+可选副词)+动词 拼出来的部分非空


def test_generate_usernames_phrase_not_too_long():
    """词长约定：最长 形容词3 + 的1 + 名词4 + 副词2 + 动词3 = 13 字。"""
    names = generate_usernames("phrase", 500, set(), random.Random(7))
    longest = max(names, key=len)
    assert len(longest) <= 13, f"名字过长：{longest}（{len(longest)} 字）"


def test_generate_usernames_phrase_adverb_roughly_half():
    """副词 1/2 概率出现——按有无副词切分，两边都该占相当比例。"""
    names = generate_usernames("phrase", 600, set(), random.Random(11))
    with_adv = [n for n in names if any(a in n for a in _PHRASE_ADV)]
    ratio = len(with_adv) / len(names)
    assert 0.35 < ratio < 0.65, f"带副词比例 {ratio:.2f} 偏离 1/2 太多"


def test_generate_usernames_phrase_has_touhou_flavor_mix():
    """词池里混了一部分东方 flavor 词，采样够多应该能见到。"""
    names = generate_usernames("phrase", 300, set(), random.Random(3))
    touhou_markers = ("⑨", "巫女", "妖精", "河童", "天狗", "弹幕", "结界",
                      "擦弹", "残机", "神隐", "毛玉", "车万", "封印", "退治")
    hits = {m for m in touhou_markers if any(m in n for n in names)}
    assert len(hits) >= 5, f"东方味太淡，300 个名字里只命中 {hits}"


def test_generate_usernames_respects_taken():
    taken = {"张三"}
    names = generate_usernames("phrase", 5, set(taken), random.Random(2))
    assert "张三" not in names
    assert len(names) == 5


def test_generate_usernames_raises_when_pool_exhausted():
    # npc 款理论上限 16 职业 × 99 编号 = 1584 个不同名字，要 1600 个必然撞满
    with pytest.raises(RuntimeError):
        generate_usernames("npc", 1600, set(), random.Random(4))
