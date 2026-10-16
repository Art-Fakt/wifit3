from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label
from rich.markup import escape

from wifit3.persist.config import Config, ConfigError
from wifit3.ui.path_picker import BROWSE_GLYPH, PathInput, PathPickerModal
import shutil
import sys
from pathlib import Path
from typing import Optional

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

    def __init__(self) -> None:
        super().__init__()
        self._wordlists: list[str] = list(Config.wordlist_paths)

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
    #hashcat-choose {
        width: 1fr;
    }
    #hashcat-wordlists {
        margin-left: 2;
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
            
            yield Button(f"{BROWSE_GLYPH} Choose wordlists", "primary", id="hashcat-choose")
            yield Label(self._wordlists_markup(), id="hashcat-wordlists")
            
            args = Config.hashcat_args or ""
            yield Label("Hashcat Args:", classes="hashcat-label")
            yield Input(args, placeholder="custom hashcat args (e.g. -a 0 -w 3)",
                        id="hashcat-args")
            yield Checkbox(_SELF_TEST_DISABLE, value=_SELF_TEST_DISABLE in args,
                           id="hashcat-self-test-disable")

            with Horizontal(id="hashcat-buttons"):
                yield Button("Cancel", variant="error", id="hashcat-cancel")
                yield Button("Launch", variant="success", id="hashcat-launch")

    def _wordlists_markup(self) -> str:
        if not self._wordlists:
            return "[dim]none chosen[/dim]"
        return "\n".join(f"[bold]{number}:[/bold] {escape(Path(path).name)}"
                        for number, path in enumerate(self._wordlists, 1))

    @on(Button.Pressed, "#hashcat-choose")
    def _choose_wordlists(self, event: Button.Pressed) -> None:
        event.stop()
        start = self._wordlists[0] if self._wordlists else str(Path.home())
        self.app.push_screen(
            PathPickerModal(start, title="Select wordlist(s)", multiple=True,
                            checked=[Path(path) for path in self._wordlists]),
            self._wordlists_chosen,
        )

    def _wordlists_chosen(self, paths: Optional[list[Path]]) -> None:
        if paths is None:
            return
        self._wordlists = [str(path) for path in paths]
        self.query_one("#hashcat-wordlists", Label).update(self._wordlists_markup())

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
            if not exe or not self._wordlists:
                self.notify("Please provide the executable and at least one wordlist",
                            severity="error")
                return
            args = self.query_one("#hashcat-args", Input).value.strip()
            self._remember(exe, self._wordlists, args)
            self.dismiss({
                "hashcat_exe": exe,
                "wordlists": list(self._wordlists),
                "hashcat_args": args
            })

    def _remember(self, exe: str, wordlists: list[str], args: str) -> None:
        """Persist the launch settings so the next job starts pre-filled."""
        Config.hashcat_path = exe
        Config.wordlist_paths = list(wordlists)
        Config.hashcat_args = args
        try:
            Config.save()
        except ConfigError as e:
            self.notify(str(e), title="Config Error", severity="warning")
