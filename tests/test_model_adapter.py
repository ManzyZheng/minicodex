from __future__ import annotations

from types import SimpleNamespace

from minicodex.model_adapter import OpenAIChatModel


def response(*, content="answer", tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls or [])
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
    completions = FakeCompletions([response(content="ok")])
    model = OpenAIChatModel(client_with(completions), model="qwen3.8-flash", enable_thinking=True)
    model.complete([{"role": "user", "content": "hello"}], [])
    assert completions.calls[0]["extra_body"] == {"enable_thinking": True}
