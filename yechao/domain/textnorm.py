"""文本规范化 —— 在**摄取入口**把文本收敛成规范形。

## 要防什么

**有人用不可见字符绕过脱敏。**

观察到三种真实绕过：

| 形态 | 写出来 | 在模型眼里 |
|---|---|---|
| 零宽字符 | `张​三` | 与 `张三` 是同一串 |
| 软连字符 | `张­三` | 同上 |
| 全角字母 | `ＱＱ12345678` | 与 `QQ12345678` 是同一个号 |

判据（比如"这个号码够不够长"）是按**字符**数的。插一个零宽字符，一个 11 位
手机号就变成 12 个字符、且中间断成两截 —— 任何按连续数字串写的规则都会漏。

## 为什么收敛在入口，而不是给每条规则各打一个补丁

补丁的个数会随规则数增长，而**每一条新规则都要记得打**。漏打的那条不会报错，
只会静默地少抹一处。

在入口收敛一次，所有下游规则都受益 —— 这是**分层**，不是"多一步预处理"。

## 哪些折叠、哪些不动（这是本模块唯一需要判断的地方）

⚠️ 规范化会**改写正文**，而正文是模型要读的。改写过头会改变文本的含义。

| 处理 | 例子 | 为什么 |
|---|---|---|
| ✅ 删 Cf（格式类）字符 | 零宽、软连字符、变体选择符、BOM | 它们**不携带语义**，只用于绕过 |
| ✅ 删 Cc（控制类）字符 | NUL、BEL、`\x1e` | 既可能打断提示词的框架，也可能让 tokenizer 行为异常 |
| ✅ 保留 `\n` `\t` | 换行、制表 | 它们是**排版**，改了会改变模型对结构的理解 |
| ✅ 全角→半角（**只限字母与数字**） | `ＱＱ`→`QQ`、`１２３`→`123` | 这是行文习惯，不是绕过；且**折叠后含义不变** |
| ❌ **不**折叠全角标点 | `，` `。` `：` `！` `？` | ⚠️ 它们是**正常中文写法**，折叠会改掉文本的语域 |
| ❌ 不做 NFKC 全量兼容分解 | `①`→`1`、`㎞`→`km` | 改的是含义，不只是形态；`①` 在通知里常是**序号**而非数字 1 |
| ❌ 不去空白 / 不做繁简转换 | | 同上：改变的是内容 |

⚠️ **全角标点为什么必须排除**：`ＱＱ` 是绕过（同一串的两种写法），而 `，`
不是 —— 中文里写逗号本来就用全角。把两者一起折叠，等于替用户改作文。

## 出口

`normalize()` 之后的文本**就是**库里存的文本，也是送给模型的文本。于是下游
任何比较（逐字摘录回连、判据的字面匹配）都只需要一个口径。
"""

from __future__ import annotations

import unicodedata

#: 允许保留的控制字符。其余 Cc 一概删掉。
#: ⚠️ 只留这两个：它们是排版，不是绕过的手段。
_KEEP_CONTROL = frozenset({"\n", "\t"})

#: 全角 → 半角的**字母与数字**区间（不含标点）。
#: · U+FF10–U+FF19 → `0`–`9`
#: · U+FF21–U+FF3A → `A`–`Z`
#: · U+FF41–U+FF5A → `a`–`z`
#: ⚠️ 刻意**不含** U+FF01–U+FF0F / U+FF1A–U+FF20 / U+FF3B–U+FF40 / U+FF5B–U+FF5E
#: —— 那些是标点与符号（`，` `：` `！` `？` `（` 等），是正常中文写法。
_FULLWIDTH_ALNUM = (
    (0xFF10, 0xFF19),
    (0xFF21, 0xFF3A),
    (0xFF41, 0xFF5A),
)

#: 全角空格 → 半角空格。它在中文行文里与半角空格**功能相同**，折叠无害。
_IDEOGRAPHIC_SPACE = "　"


def _fold_fullwidth(text: str) -> str:
    out: list[str] = []
    for ch in text:
        cp = ord(ch)
        folded = False
        for lo, hi in _FULLWIDTH_ALNUM:
            if lo <= cp <= hi:
                # 全角区间与 ASCII 区间是固定偏移 0xFEE0
                out.append(chr(cp - 0xFEE0))
                folded = True
                break
        if not folded:
            out.append(" " if ch == _IDEOGRAPHIC_SPACE else ch)
    return "".join(out)


def _strip_invisible(text: str) -> str:
    """删掉 Cf（格式）与 Cc（控制）字符，`\\n` `\\t` 除外。"""
    out: list[str] = []
    for ch in text:
        if ch in _KEEP_CONTROL:
            out.append(ch)
            continue
        cat = unicodedata.category(ch)
        if cat in {"Cf", "Cc", "Cs"}:
            # Cf = 格式（零宽、软连字符、变体选择符、BOM）
            # Cc = 控制（NUL、BEL、ESC、\x1e 记录分隔符）
            # Cs = 代理对残片（解码出错时的产物）
            continue
        out.append(ch)
    return "".join(out)


def _collapse_blank_lines(text: str) -> str:
    """三个以上连续换行压成两个。

    ⚠️ 这一条是**排版噪声**，不是绕过：刷屏消息里常见十几个空行，它会挤掉
    上下文预算（而预算是切批要守的东西）。压成空行段**不改变语义**。
    """
    lines = text.split("\n")
    out: list[str] = []
    blanks = 0
    for line in lines:
        if line.strip() == "":
            blanks += 1
            if blanks > 2:
                continue
        else:
            blanks = 0
        out.append(line.rstrip())
    return "\n".join(out).strip()


def normalize(text: str) -> str:
    """收敛成规范形。**幂等** —— 对已规范化的文本再调一次，结果相同。

    幂等很重要：摄取时调一次，脱敏时可能又碰到，两次结果必须一致，否则
    「库里存的那个字符串」与「送给模型的那个字符串」就对不上了。
    """
    if not text:
        return ""
    return _collapse_blank_lines(_fold_fullwidth(_strip_invisible(text)))


def is_normalized(text: str) -> bool:
    """自检用：这段文本是否已经是规范形。"""
    return normalize(text) == text
