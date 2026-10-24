"""模型客户端：**结构化输出**（`response_format`）那一条线。

实测依据：Ollama 把 JSON schema 编成 GBNF，解码时物理上输出不了结构以外的
东西 —— 这消掉的是「模型回一段散文、整批记 `parse_empty`」那一种失败
（交接文档里的已知问题 #1）。改之前模型会把标题填进类目（`"社团招新"`），
然后那一条被静默丢弃。

※ 形状由**调用方**声明：同一个端点被抽取、日报、云端复核、重排共用，而它们的
输出形状各不相同。
"""
from __future__ import annotations

from yechao.domain.errors import ModelError
from yechao.infrastructure.llm.openai_client import ModelConfig, OpenAIClient


def _client(**kw) -> OpenAIClient:
    return OpenAIClient(
        ModelConfig(base_url="http://127.0.0.1:11434/v1/chat/completions",
                    model="yechao-llm", **kw)
    )


def test_给了_schema_就带在请求体里() -> None:
    body = _client().build_body(
        system="s", user="u", max_tokens=10, response_schema={"type": "object"}
    )
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["schema"] == {"type": "object"}
    assert fmt["json_schema"]["strict"] is True, (
        "严格模式没开 —— 那会让『可省略的字段』重新变成自由形状"
    )


def test_不给_schema_就完全不发这个字段() -> None:
    """※ 日报 / 云端复核 / 重排走的是同一个客户端，它们的输出形状不是抽取那一个。

    无条件带 schema 的症状：日报的模型被逼着吐 `{"items": [...]}`，
    而它本来要吐的是一行行摘要。
    """
    assert "response_format" not in _client().build_body(
        system="s", user="u", max_tokens=10
    )


def test_抽取的_schema_枚举就是当前类目() -> None:
    """※ 枚举**不能**写死：类目是用户能改的，写死一份的症状是
    「新加的类目永远抽不出来」，而它看起来像「模型漏了」。
    """
    from yechao.application.extract import _response_schema
    from yechao.domain.categories import default_categories

    slugs = tuple(c.slug for c in default_categories())
    enum = _response_schema(slugs)["properties"]["items"]["items"]["properties"][
        "kind"
    ]["enum"]
    assert enum == list(slugs)

    # 多加一个类目 ⇒ schema 立刻跟着变（说明它是每次现算的，不是固定的）
    more = slugs + ("brand_new",)
    enum2 = _response_schema(more)["properties"]["items"]["items"]["properties"][
        "kind"
    ]["enum"]
    assert enum2 == list(more)


def test_一个类目都没有时不发_schema() -> None:
    """空枚举会让模型**一个字都写不出来**（枚举里没有合法值）—— 所以退化成
    不发 schema，而不是发一个空枚举。"""
    from yechao.application.extract import _response_schema

    assert _response_schema(()) is None


def test_端点不认_response_format_时回落一次(monkeypatch) -> None:
    """※ 本项目明确支持「任何 OpenAI 兼容端点」，而结构化输出是较新的一档。

    不认它的端点回 400 / 422 —— 回落要**只试一次**：记住之后，后面的批
    不再每批白撞一个 400。
    """
    c = _client()
    试过的: list[bool] = []

    def 假_post(body):
        试过的.append("response_format" in body)
        if "response_format" in body:
            raise ModelError("端点不认这个字段", status=400)
        return {"choices": [{"message": {"content": "[]"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(c, "_post", 假_post)
    assert c.complete(system="s", user="u", response_schema={"type": "object"}) == "[]"
    assert 试过的 == [True, False], "没有回落重试"

    # 第二批：**不再**先白试一次
    assert c.complete(system="s", user="u", response_schema={"type": "object"}) == "[]"
    assert 试过的 == [True, False, False]


def test_关思考时同时发_reasoning_effort_none() -> None:
    """※ 第三种方言。

    实测（Ollama 0.40.6 + bonsai-2:4b 走 `/v1/chat/completions`）：
    `enable_thinking` / `think` / `chat_template_kwargs` 三个字段全发出去，
    模型**照样**把 512 token 全烧在 `Thinking Process` 上，`content` 为空；
    只有 `reasoning_effort: "none"` 能让 reasoning 长度为 0。
    而同一个模型走原生 `/api/chat` 时 `think: false` 是生效的 ——
    两个端点认的不是同一个字段，所以这个字段必须一起发。
    """
    body = _client(enable_thinking=False).build_body(system="s", user="u", max_tokens=10)
    assert body["reasoning_effort"] == "none"
    assert body["think"] is False
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


def test_没设开关时思考相关的字段一个都不发() -> None:
    """`None` 是「不发」，不是「发 false」—— 三态不能压成两态。"""
    body = _client().build_body(system="s", user="u", max_tokens=10)
    for key in ("enable_thinking", "think", "chat_template_kwargs", "reasoning_effort"):
        assert key not in body, f"没设开关却发了 {key}"


def test_开着思考时不发_reasoning_effort() -> None:
    """`reasoning_effort` 只在**关**思考时发。

    开着思考时随手填一个档位（`"high"` 之类）是替用户选了他没选的开关；
    这个字段代表的是「关掉」，不是一个可以随便填的调优旋钮。
    """
    body = _client(enable_thinking=True).build_body(system="s", user="u", max_tokens=10)
    assert body["think"] is True
    assert "reasoning_effort" not in body


def test_别的_4xx_不回落(monkeypatch) -> None:
    """※ 400 / 422 才可能是「不认这个字段」；401 是密钥、404 是地址、429 是限流
    —— 把它们也回落一遍，只会把真正的病因埋进第二次请求里。
    """
    c = _client()

    def 假_post(body):
        raise ModelError("没权限", status=401)

    monkeypatch.setattr(c, "_post", 假_post)
    try:
        c.complete(system="s", user="u", response_schema={"type": "object"})
    except ModelError as e:
        assert e.context.get("status") == 401
    else:  # pragma: no cover - 走不到
        raise AssertionError("401 不该被回落吞掉")
