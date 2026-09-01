from __future__ import annotations

from minicodex.agent import AgentSession
from minicodex.models import ModelReply
from minicodex.permissions import AgentMode
from minicodex.tools import ToolRuntime


class ReplyModel:
    def complete(self, messages, tools) -> ModelReply:
        return ModelReply(content="完成")


def test_agent_exports_and_restores_conversation_without_transient_read_state(tmp_path) -> None:
    source = tmp_path / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    original = AgentSession(ReplyModel(), ToolRuntime(tmp_path, approver=lambda _request: True, mode=AgentMode.AUTO_ACT))
    original.run_turn("检查项目")
    original.tools.read_file("read", "app.py")

    state = original.export_state()
    restored_runtime = ToolRuntime(tmp_path, approver=lambda _request: True, mode=AgentMode.ACT)
    restored = AgentSession(ReplyModel(), restored_runtime)
    restored.restore_state(state)

    assert restored.prompt_count == 1
    assert state["schema_version"] == 2
    assert restored.execution_mode is AgentMode.AUTO_ACT
    assert any(message.get("content") == "检查项目" for message in restored.messages)
    assert not restored_runtime.read_paths


def test_agent_refreshes_memory_layer_without_adding_it_to_conversation_history(tmp_path) -> None:
    memory = {"value": "<global_memory_index>\n- 默认使用中文\n</global_memory_index>"}
    agent = AgentSession(
        ReplyModel(),
        ToolRuntime(tmp_path, approver=lambda _request: True),
        memory_prompt_provider=lambda: memory["value"],
    )

    agent.run_turn("你好")
    state = agent.export_state()

    assert agent.messages[3]["role"] == "system"
    assert "默认使用中文" in agent.messages[3]["content"]
    assert all("global_memory_index" not in str(message.get("content", "")) for message in state["messages"])


def test_restored_agent_rebuilds_memory_from_current_store_not_saved_snapshot(tmp_path) -> None:
    old = AgentSession(ReplyModel(), ToolRuntime(tmp_path, approver=lambda _request: True), memory_prompt_provider=lambda: "old memory")
    old.run_turn("task")
    state = old.export_state()

    restored = AgentSession(ReplyModel(), ToolRuntime(tmp_path, approver=lambda _request: True), memory_prompt_provider=lambda: "new memory")
    restored.restore_state(state)

    assert restored.messages[3]["content"] == "new memory"
    assert "old memory" not in restored.messages[3]["content"]
