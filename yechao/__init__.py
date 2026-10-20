"""夜抄 · Nocturnal Notes

把散在多个群里的通知、报名、活动、失物招领，整理成一份按天可读的日报。

核心主张：**本地预处理**。敏感原文在本机完成抽取，只有脱敏后的结构化结果
离开这台机器。

分层（依赖方向只有一个：外 → 内）：

    interfaces/      HTTP 路由、CLI          ← 只做输入输出
    application/     用例编排、端口定义        ← 只依赖 domain 与自己定义的端口
    infrastructure/  SQLite、模型、配置       ← 实现端口
    domain/          纯逻辑，零 I/O           ← 不依赖任何外层

    bootstrap.py     组合根（唯一横跨多层的地方）

⚠️ 这个文件保持空壳。逻辑都在具名模块里 —— 「只有 __init__.py 的层」会让
分层护栏扫到零个有意义的文件，而护栏报告仍是绿的。判据见
`tests/contracts/test_layering.py` 的空转自检。
"""

__version__ = "0.1.0"
