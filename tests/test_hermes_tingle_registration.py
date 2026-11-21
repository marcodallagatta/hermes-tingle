import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import __init__ as plugin


class Context:
    def __init__(self):
        self.tools = []
        self.commands = []
        self.skills = []

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_command(self, name, **kwargs):
        self.commands.append((name, kwargs))

    def register_skill(self, name, path):
        self.skills.append((name, Path(path)))


def test_registered_setup_command_can_be_cancelled_without_writes(synthetic_settings):
    class CancelContext(Context):
        def ask_choice(self, question, choices):
            assert 'Set up Hermes Tingle' in question
            return 'cancel'

        def dispatch_tool(self, *args, **kwargs):
            raise AssertionError('cancelled setup must not change jobs')

    ctx = CancelContext()
    plugin.register(ctx)
    result = ctx.commands[0][1]['handler']('setup')
    assert 'authenticated human' in result.lower()
    assert 'direct message' in result.lower()
    assert 'tingle_init.py' not in result


def test_plugin_registers_hermes_tingle_tool_and_tingle_command():
    ctx = Context()

    plugin.register(ctx)

    assert len(ctx.tools) == 1
    assert ctx.tools[0]["name"] == "hermes_tingle"
    assert ctx.tools[0]["toolset"] == "hermes_tingle"
    assert ctx.tools[0]["schema"]["name"] == "hermes_tingle"
    assert [name for name, _ in ctx.commands] == ["tingle"]
    assert ctx.skills[0][0] == "usage"
    manifest = Path(__file__).resolve().parents[1] / "plugin.yaml"
    assert "name: hermes-tingle" in manifest.read_text()
