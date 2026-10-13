from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label

from wifit3.persist.config import Config, ConfigError
from wifit3.ui.path_picker import PathInput
import shutil
import sys

_SELF_TEST_DISABLE = "--self-test-disable"


def _default_hashcat_path() -> str:
    """A saved path wins; otherwise find hashcat on PATH, then guess per platform."""
    if Config.hashcat_path:
        return Config.hashcat_path
    if found := shutil.which("hashcat"):
        return found
    if sys.platform == "win32":
        return r"C:\hashcat\hashcat.exe"
    if sys.platform == "darwin":
        return "/opt/homebrew/bin/hashcat"
    return "/usr/bin/hashcat"


class HashcatConfigModal(ModalScreen[dict]):
    """Configuration modal for Hashcat jobs."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    HashcatConfigModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.5);
    }
    #hashcat-dialog {
        width: 60;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: solid $primary;
    }
    #hashcat-title {
        width: 1fr;
        text-align: center;
        text-style: bold;
        color: $accent;
    }
    .hashcat-label {
        margin-top: 1;
    }
    #hashcat-buttons {
        margin-top: 2;
        align: right middle;
        height: auto;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="hashcat-dialog"):
            yield Label("Launch Hashcat", id="hashcat-title")
            
            yield Label("Hashcat Executable Path:", classes="hashcat-label")
            yield PathInput(_default_hashcat_path(), title="Select hashcat executable",
                            id="hashcat-exe")
            
            yield Label("Wordlist Path:", classes="hashcat-label")
            yield PathInput(Config.wordlist_path or "", title="Select wordlist",
                            placeholder="e.g. D:\\wordlists\\Top29Million.txt",
                            id="hashcat-wordlist")
            
            args = Config.hashcat_args or ""
            yield Label("Hashcat Args:", classes="hashcat-label")
            yield Input(args, placeholder="custom hashcat args (e.g. -a 0 -w 3)",
                        id="hashcat-args")
            yield Checkbox(_SELF_TEST_DISABLE, value=_SELF_TEST_DISABLE in args,
                           id="hashcat-self-test-disable")

            with Horizontal(id="hashcat-buttons"):
                yield Button("Cancel", variant="error", id="hashcat-cancel")
                yield Button("Launch", variant="success", id="hashcat-launch")

    @on(Checkbox.Changed, "#hashcat-self-test-disable")
    def _toggle_self_test_disable(self, event: Checkbox.Changed) -> None:
        field = self.query_one("#hashcat-args", Input)
        args = field.value.strip()
        if not event.value:
            field.value = args.replace(_SELF_TEST_DISABLE, "").strip()
        elif _SELF_TEST_DISABLE not in args:
            field.value = f"{args} {_SELF_TEST_DISABLE}".strip()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "hashcat-cancel":
            self.action_cancel()
        elif event.button.id == "hashcat-launch":
            exe = self.query_one("#hashcat-exe", Input).value.strip()
            wordlist = self.query_one("#hashcat-wordlist", Input).value.strip()
            if not exe or not wordlist:
                self.notify("Please provide both executable and wordlist paths", severity="error")
                return
            args = self.query_one("#hashcat-args", Input).value.strip()
            self._remember(exe, wordlist, args)
            self.dismiss({
                "hashcat_exe": exe,
                "wordlist": wordlist,
                "hashcat_args": args
            })

    def _remember(self, exe: str, wordlist: str, args: str) -> None:
        """Persist the launch settings so the next job starts pre-filled."""
        Config.hashcat_path = exe
        Config.wordlist_path = wordlist
        Config.hashcat_args = args
        try:
            Config.save()
        except ConfigError as e:
            self.notify(str(e), title="Config Error", severity="warning")
