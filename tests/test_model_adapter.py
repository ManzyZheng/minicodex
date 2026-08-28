from __future__ import annotations

from types import SimpleNamespace

from minicodex.model_adapter import OpenAIChatModel


def response(*, content="answer", reasoning_content=None, tool_calls=None):
    message = SimpleNamespace(content=content, reasoning_content=reasoning_content, tool_calls=tool_calls or [])
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeCompletions:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def client_with(completions):
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def test_openai_adapter_parses_tool_calls_and_sends_tools() -> None:
    call = SimpleNamespace(id="c1", function=SimpleNamespace(name="read_file", arguments='{"path":"a.py"}'))
    completions = FakeCompletions([response(content=None, tool_calls=[call])])
    model = OpenAIChatModel(client_with(completions), model="demo", sleep=lambda _seconds: None)
    reply = model.complete([{"role": "user", "content": "go"}], [{"type": "function"}])
    assert reply.tool_calls[0].name == "read_file"
    assert reply.tool_calls[0].arguments == {"path": "a.py"}
    assert completions.calls[0]["model"] == "demo"
    assert completions.calls[0]["tools"] == [{"type": "function"}]


def test_openai_adapter_retries_transient_status_three_attempts() -> None:
    transient = RuntimeError("rate limited")
    transient.status_code = 429
    completions = FakeCompletions([transient, transient, response(content="ok")])
    model = OpenAIChatModel(client_with(completions), model="demo", sleep=lambda _seconds: None)
    assert model.complete([], []).content == "ok"
    assert len(completions.calls) == 3


def test_openai_adapter_enables_qwen_thinking_in_extra_body() -> None:
    completions = FakeCompletions([response(content="ok", reasoning_content="inspect the failing test")])
    model = OpenAIChatModel(client_with(completions), model="qwen3.8-flash", enable_thinking=True)
    reply = model.complete([{"role": "user", "content": "hello"}], [])
    assert reply.reasoning_content == "inspect the failing test"
    assert completions.calls[0]["extra_body"] == {"enable_thinking": True, "preserve_thinking": False}


def test_openai_adapter_accepts_compatible_models_without_reasoning_field() -> None:
    message = SimpleNamespace(content="plain answer", tool_calls=[])
    completions = FakeCompletions([SimpleNamespace(choices=[SimpleNamespace(message=message)])])
    model = OpenAIChatModel(client_with(completions), model="plain-model")
    reply = model.complete([], [])
    assert reply.content == "plain answer"
    assert reply.reasoning_content is None


def test_openai_adapter_model_can_change_between_prompts() -> None:
    completions = FakeCompletions([response(content="one"), response(content="two")])
    model = OpenAIChatModel(client_with(completions), model="first")

    model.complete([], [])
    model.set_model("second")
    model.complete([], [])

    assert [call["model"] for call in completions.calls] == ["first", "second"]
