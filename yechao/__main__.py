"""`python -m yechao <命令>` 的入口。

⚠️ 这里是入口层的入口：它只做一件事 —— 把 argv 交给 CLI 并把返回值当退出码。
任何判断（比如"这个命令该不该跑"）都不该写在这儿，否则它就绕过 CLI 那一层
的测试。
"""

from __future__ import annotations

import sys

from yechao.interfaces.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
