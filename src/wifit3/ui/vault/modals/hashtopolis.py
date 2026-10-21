from typing import Any, Optional

from rich.markup import escape
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select

from wifit3.persist.config import Config, ConfigError
from wifit3.vault.tools.hashtopolis_api import (AccessGroup, HashtopolisApi, HashtopolisError,
                                                ServerFile)

_ACCESS_KEY_HELP = "[dim]Access key: the legacy UI on port 8080, Users → API Management.[/dim]"


class HashtopolisConfigModal(ModalScreen[dict]):
    """Server, access key, wordlist and access group for Hashtopolis jobs; dismisses with the
    job's ``config``, or None when cancelled."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    # CSS, not DEFAULT_CSS: the app's `Button { min-width: 12 }` outranks any DEFAULT_CSS rule.
    CSS = """
    #hashtopolis-test { min-width: 0; width: 8; }
    """

    DEFAULT_CSS = """
    HashtopolisConfigModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.5);
    }
    #hashtopolis-dialog {
        width: 64;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: solid $primary;
    }
    #hashtopolis-title {
        width: 1fr;
        text-align: center;
        text-style: bold;
        color: $accent;
    }
    .hashtopolis-label { margin-top: 1; }
    #hashtopolis-test-row { height: auto; margin-top: 1; }
    #hashtopolis-test-result { width: 1fr; margin-left: 1; }
    #hashtopolis-buttons {
        margin-top: 2;
        align: right middle;
        height: auto;
    }
    """

    def __init__(self, confirm_label: str = "Launch") -> None:
        """``confirm_label`` "Save" configures the tool without the caller launching a job."""
        super().__init__()
        self._confirm_label = confirm_label
        self._wordlists: dict[int, str] = {}
        if Config.hashtopolis.wordlist_id:
            self._wordlists[Config.hashtopolis.wordlist_id] = Config.hashtopolis.wordlist_name or ""

    def compose(self) -> ComposeResult:
        with Vertical(id="hashtopolis-dialog"):
            yield Label("Hashtopolis", id="hashtopolis-title")
            yield Label("Server URL:", classes="hashtopolis-label")
            yield Input(Config.hashtopolis.url or "", placeholder="http://192.168.1.10:8080",
                        id="hashtopolis-url")
            yield Label("Access key:", classes="hashtopolis-label")
            yield Input(Config.hashtopolis.access_key or "", password=True,
                        id="hashtopolis-access-key")
            yield Label(_ACCESS_KEY_HELP)
            with Horizontal(id="hashtopolis-test-row"):
                yield Button("Test", "primary", id="hashtopolis-test")
                yield Label("", id="hashtopolis-test-result")
            yield Label("Wordlist:", classes="hashtopolis-label")
            yield Select([(name, file_id) for file_id, name in self._wordlists.items()],
                         prompt="Test the connection to list wordlists",
                         value=Config.hashtopolis.wordlist_id or Select.NULL,
                         id="hashtopolis-wordlist")
            yield Label("Access group:", classes="hashtopolis-label")
            group_id = Config.hashtopolis.access_group_id
            yield Select([(str(group_id), group_id)] if group_id else [],
                         prompt="Test the connection to list access groups",
                         value=group_id or Select.NULL, id="hashtopolis-access-group")
            with Horizontal(id="hashtopolis-buttons"):
                yield Button("Cancel", variant="error", id="hashtopolis-cancel")
                yield Button(self._confirm_label, variant="success", id="hashtopolis-confirm")

    @on(Button.Pressed, "#hashtopolis-test")
    def _test_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.query_one("#hashtopolis-test-result", Label).update("[dim]Testing...[/dim]")
        self._test(self.query_one("#hashtopolis-url", Input).value.strip(),
                   self.query_one("#hashtopolis-access-key", Input).value.strip())

    @work(thread=True, exclusive=True, group="hashtopolis-test")
    def _test(self, url: str, access_key: str) -> None:
        try:
            api = HashtopolisApi(url, access_key)
            api.check_access()
            wordlists = api.wordlists()
            groups = api.access_groups()
        except HashtopolisError as e:
            self.app.call_from_thread(self._show_test_failure, str(e))
            return
        self.app.call_from_thread(self._show_server_lists, wordlists, groups)

    def _show_test_failure(self, message: str) -> None:
        if not self.is_attached:
            return
        self.query_one("#hashtopolis-test-result", Label).update(f"[red]{escape(message)}[/red]")

    def _show_server_lists(self, wordlists: list[ServerFile], groups: list[AccessGroup]) -> None:
        if not self.is_attached:
            return
        result = self.query_one("#hashtopolis-test-result", Label)
        if wordlists:
            result.update(f"[green]Connected[/green], {len(wordlists)} wordlist(s)")
        else:
            result.update("[orange1]Connected, but no wordlists found[/orange1]")
        self._wordlists = {f.file_id: f.filename for f in wordlists}
        self._fill(self.query_one("#hashtopolis-wordlist", Select),
                   [(f.filename, f.file_id) for f in wordlists])
        self._fill(self.query_one("#hashtopolis-access-group", Select),
                   [(g.name, g.group_id) for g in groups])

    @staticmethod
    def _fill(select: Select, options: list[tuple[str, int]]) -> None:
        chosen = select.value
        select.set_options(options)
        values = [value for _, value in options]
        if chosen in values:
            select.value = chosen
        elif len(values) == 1:
            select.value = values[0]

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#hashtopolis-cancel")
    def _cancel(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_cancel()

    @on(Button.Pressed, "#hashtopolis-confirm")
    def _confirm(self, event: Button.Pressed) -> None:
        event.stop()
        url = self.query_one("#hashtopolis-url", Input).value.strip()
        access_key = self.query_one("#hashtopolis-access-key", Input).value.strip()
        if not (url and access_key):
            self.notify("Please provide the server URL and access key", severity="error")
            return
        config = self._launch_config()
        if config is None:
            return
        self._remember(url, access_key, config)
        self.dismiss(config)

    def _launch_config(self) -> Optional[dict[str, Any]]:
        """The job's config; the server URL and access key stay in Config, out of jobs.json."""
        wordlist_id = self.query_one("#hashtopolis-wordlist", Select).value
        group_id = self.query_one("#hashtopolis-access-group", Select).value
        if not isinstance(wordlist_id, int) or not isinstance(group_id, int):
            self.notify("Please test the connection and choose a wordlist and access group",
                        severity="error")
            return None
        return {"wordlist_id": wordlist_id, "wordlist_name": self._wordlists.get(wordlist_id, ""),
                "access_group_id": group_id}

    def _remember(self, url: str, access_key: str, config: dict[str, Any]) -> None:
        Config.hashtopolis.url = url
        Config.hashtopolis.access_key = access_key
        Config.hashtopolis.wordlist_id = config["wordlist_id"]
        Config.hashtopolis.wordlist_name = config["wordlist_name"]
        Config.hashtopolis.access_group_id = config["access_group_id"]
        try:
            Config.save()
        except ConfigError as e:
            self.notify(str(e), title="Config Error", severity="warning")
