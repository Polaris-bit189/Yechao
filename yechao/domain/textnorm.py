"""文本规范化 —— 在**摄取入口**把文本收敛成规范形。

## 要防什么

下游每一条判据都是**按码点写的**（「11 位连续数字」「`1[3-9]` 开头」
「两侧不是数字」）。而这些判据想描述的是**人读到的那个字符串**。
只要有一个码点「人看不见、判据看得见」，这两者就对不上了 ——
判据于是**静默地**少判一处。

这不是「有人恶意攻击」才有的形状。键盘输入法、复制粘贴、富文本编辑器
都会产生这类字符，它们**自然地**出现在语料里。

## 失败模式（从输入的形状推，不是从某个案例抄）

| 形态 | 例子 | 为什么判据会漏 |
|---|---|---|
| Cf（格式类）| 零宽空格 `林​小满`、软连字符 `林­小满`、BOM | 它**把一段连续的数字/汉字切开**。人读到的是 `林小满`，判据看到的是 3 个码点 |
| **Mn（变体选择符）** | `1️7700001001`（`1` 与 `7` 之间夹了 U+FE0F） | 同上。※ **它不是 Cf，是 Mn** —— 见下面「一个踩过的坑」 |
| Cc（控制类）| NUL、BEL、`\x1e` | 既能打断提示词的框架，也可能让 tokenizer 行为异常 |
| 全角字母数字 | `ＱＱ`、`９０００` | 人读到的与半角**完全一样**，但 `1[3-9]` 这种**写死 ASCII 区间**的判据匹配不上 |

## 一个踩过的坑：变体选择符不是 Cf

第一版这里写着「删 Cf（格式类）字符：零宽、软连字符、**变体选择符**、BOM」。
**那句话是错的**：`unicodedata.category("️")` 是 **`Mn`**（非间距记号），
不是 `Cf`。所以按类别删 Cf 的代码**根本删不掉变体选择符**，
而注释让人以为它删掉了 —— 一条**看起来在起作用、其实没有**的判据。

实测（改前）：`normalize("1️7700001001")` 原样返回，
手机号判据在这个输入上返回空。改后：变体选择符被删掉，判据正常命中。

⇒ 判据改成**显式枚举变体选择符的码点区间**（U+FE00–FE0F、U+E0100–E01EF），
而不是指望某个 Unicode 类别替我管住它。**枚举一个封闭且稳定的集合**是安全的
（这两个区间是标准里写死的，不会随版本增长）；枚举一个会增长的集合才危险。

※ 只删**变体选择符**，不删整个 `Mn` 类。其它非间距记号（拉丁语系的声调符
等）**携带语义**，删了就是改内容。

## 为什么收敛在入口，而不是给每条规则各打一个补丁

补丁的个数会随规则数增长，而**每一条新规则都要记得打**。漏打的那条不会报错，
只会静默地少抹一处。在入口收敛一次，所有下游规则都受益。

## 哪些折叠、哪些不动（这是本模块唯一需要判断的地方）

※ 规范化会**改写正文**，而正文是模型要读的，**也是库里存下来、界面上显示
的那一份**。改写过头就是替用户改作文。

所以这一节的判据只有一句话：**折叠前后，人读到的还是同一个东西吗？**

| 处理 | 例子 | 判据 |
|---|---|---|
| √ 删 Cf / Cc / Cs / 变体选择符 | 见上表 | 它们**不携带语义**，人读到的是「没有它」的那串 |
| √ 保留 `\n` `\t` | 换行、制表 | 它们是**排版**。改掉会改变模型对结构的理解（群公告的条目靠换行分节）|
| √ 全角→半角（**只限字母与数字**）| `ＱＱ`→`QQ`、`９`→`9` | 折叠后**读起来一模一样**：`Ｑ` 与 `Q` 是同一种字符的两种宽度 |
| √ 全角空格 `　`→半角空格 | | 功能相同，折叠无害 |
| × **不**折叠全角标点 | `，` `。` `：` `！` `？` | ※ 它们**不是**「同一种字符的另一种宽度」，它们是**中文里的规范写法**。把 `，` 折成 `,`，界面上的每一句话都换了语域 |

### 这条线的代价（已知、接受）

全角标点不折叠 ⇒ `abc＠example.test`、`10／28` 这类**用全角标点写的 token**
不会被折叠，于是邮箱/日期判据匹配不上它们。

**为什么不为了堵它而扩大折叠集**：`／` 在中文里是**正常写法**
（`他／她`、`和／或`），折成 `/` 会改掉这些句子；而 `＠` 出现在邮箱里
是罕见的写法。**为了让一个罕见写法可匹配，去改所有正常句子的写法，代价反了。**
残余风险记在这里，不假装它不存在。

## 出口

`normalize()` 之后的文本**就是**库里存的文本，也是送给模型的文本。于是下游
任何比较（逐字摘录回连、判据的字面匹配）都只需要一个口径。
"""

from __future__ import annotations

import unicodedata

#: 允许保留的控制字符。其余 Cc 一概删掉。
#: ※ 只留这两个：它们是排版，不是绕过的手段。
_KEEP_CONTROL = frozenset({"\n", "\t"})

#: 变体选择符的码点区间（含两端）。
#: ※ 这两个区间是 Unicode 标准里**写死**的封闭集合，不会随版本增长 ——
#: 这是「可以枚举」与「不该枚举」的分界：封闭且稳定的集合，枚举是安全的。
#: · U+FE00–FE0F  ：VS1–VS16（文本呈现 / emoji 呈现选择符）
#: · U+E0100–E01EF：VS17–VS256（补充变体选择符）
#: ※ 它们**不在** `Cf` 里（是 `Mn`）—— 按类别删是删不掉它们的，见模块 docstring。
_VARIATION_SELECTOR_RANGES = ((0xFE00, 0xFE0F), (0xE0100, 0xE01EF))

#: 全角 → 半角的**字母与数字**区间（不含标点）。
#: · U+FF10–U+FF19 → `0`–`9`
#: · U+FF21–U+FF3A → `A`–`Z`
#: · U+FF41–U+FF5A → `a`–`z`
#: ※ 刻意**不含** U+FF01–U+FF0F / U+FF1A–U+FF20 / U+FF3B–U+FF40 / U+FF5B–U+FF5E
#: —— 那些是标点与符号（`，` `：` `！` `？` `（` `／` 等），是正常中文写法。
_FULLWIDTH_ALNUM = (
    (0xFF10, 0xFF19),
    (0xFF21, 0xFF3A),
    (0xFF41, 0xFF5A),
)

#: 全角空格 → 半角空格。它在中文行文里与半角空格**功能相同**，折叠无害。
_IDEOGRAPHIC_SPACE = "　"


def _is_variation_selector(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in _VARIATION_SELECTOR_RANGES)


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
    """删掉不携带语义的码点：Cf / Cc / Cs / 变体选择符。

    `\\n` `\\t` 除外（它们是排版）。
    """
    out: list[str] = []
    for ch in text:
        if ch in _KEEP_CONTROL:
            out.append(ch)
            continue
        if _is_variation_selector(ord(ch)):
            # ※ 变体选择符是 Mn，不在下面的类别集合里 —— 必须单独判。
            continue
        cat = unicodedata.category(ch)
        if cat in {"Cf", "Cc", "Cs"}:
            # Cf = 格式（零宽空格、软连字符、BOM）
            # Cc = 控制（NUL、BEL、ESC、\x1e 记录分隔符）
            # Cs = 代理对残片（解码出错时的产物）
            continue
        out.append(ch)
    return "".join(out)


def _collapse_blank_lines(text: str) -> str:
    """三个以上连续换行压成两个。

    ※ 这一条是**排版噪声**，不是绕过：刷屏消息里常见十几个空行，它会挤掉
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
