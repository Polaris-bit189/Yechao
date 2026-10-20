"""OpenAI 兼容的模型客户端。

⚠️ 三件事必须在这里处理掉，否则症状会以「模型今天心情不好」的样子出现：

1. **输出可能被 Markdown 代码块包住** —— 而且不是每次都包
2. **输出可能被长度截断** —— 必须抛 `ModelTruncated`，不是 `ModelOutputInvalid`
3. **「关思考」参数不同端点认不同的名字** —— 所以 `enable_thinking` 是三态
"""
