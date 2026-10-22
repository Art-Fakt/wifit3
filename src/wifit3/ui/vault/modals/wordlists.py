import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any, ClassVar, Optional

from rich.markup import escape
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Label

from wifit3.crack.passwords import (DEFAULT_PASSWORDS_PATH, read_passwords, ssid_passwords,
                                    ssid_variants)
from wifit3.models import PersistedCapture
from wifit3.persist.config import Config, ConfigError, CpuCrackSettings, HashcatSettings
from wifit3.ui.path_picker import BROWSE_GLYPH, PathPickerModal
from wifit3.vault.exhaustion import (SSID_VARIANTS_IDENTITY, Exhaustion, capture_exhaustion,
                                     wordlist_identity)
from wifit3.vault.tools.cracker import capture_ssid_variants

_EXAMPLE_SSID = "HomeWifi"


@cache
def _ssid_variants_per_ssid() -> int:
    return len(ssid_variants(_EXAMPLE_SSID))


@dataclass
class _PasswordSourceCounts:
    default_passwords: int
    ap_ssid_passwords: Optional[int]        # None without a capture: shown per SSID
    probed_ssid_passwords: int


def _count_password_sources(capture: Optional[PersistedCapture],
                            probed_ssids: Sequence[str]) -> _PasswordSourceCounts:
    ap_ssid_passwords = None
    if capture is not None:
        try:
            ap_ssid_passwords = len(capture_ssid_variants(capture.path))
        except OSError:
            ap_ssid_passwords = 0
    return _PasswordSourceCounts(
        default_passwords=sum(1 for _ in read_passwords(DEFAULT_PASSWORDS_PATH)),
        ap_ssid_passwords=ap_ssid_passwords,
        probed_ssid_passwords=len(ssid_passwords(probed_ssids)))


def _read_exhaustion(capture: PersistedCapture) -> dict[str, Exhaustion]:
    try:
        return capture_exhaustion(capture)
    except OSError:
        return {}


def _file_identity(path: str | Path) -> Optional[str]:
    try:
        return wordlist_identity(path) if Path(path).is_file() else None
    except OSError:
        return None


def _exhausted_tooltip(exhaustion: Exhaustion) -> str:
    date = datetime.fromtimestamp(exhaustion.latest).strftime("%Y-%m-%d")
    days = int(time.time() - exhaustion.latest) // 86400
    return (f"Exhausted {date} ({days} days ago) on "
            f"{exhaustion.exhausted_hashlines}/{exhaustion.hashlines} hashlines")


class _RemoveButton(Button):
    """The `X` beside one chosen wordlist; ``path`` is the wordlist it removes."""

    def __init__(self, path: str) -> None:
        super().__init__("X", variant="error", compact=True, classes="wordlist-remove")
        self.path = path


class WordlistConfigModal(ModalScreen[dict]):
    """Launch modal for a tool that cracks with the user's chosen wordlists; dismisses with the
    tool's ``job.config``, or None when cancelled."""

    dialog_title: ClassVar[str]

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, confirm_label: str = "Launch", capture: Optional[PersistedCapture] = None,
                 probed_ssids: Sequence[str] = ()) -> None:
        """``confirm_label`` "Save" configures the tool without the caller launching a job;
        ``capture`` is the one a launch cracks, for its SSID variant count."""
        super().__init__()
        self._confirm_label = confirm_label
        self._capture = capture
        self._probed_ssids = probed_ssids
        self._exhaustion: dict[str, Exhaustion] = {}
        self._wordlists: list[str] = list(self._settings().wordlists)

    # CSS, not DEFAULT_CSS: the app's `Button { min-width: 12 }` outranks any DEFAULT_CSS rule.
    # No type prefix: each subclass loads this scoped to its own name.
    CSS = """
    #wordlist-clear, .wordlist-remove { min-width: 0; }
    .exhausted { background: $error 30%; }
    """

    DEFAULT_CSS = """
    WordlistConfigModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.5);
    }
    #wordlist-dialog {
        width: 60;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: solid $primary;
    }
    #wordlist-title {
        width: 1fr;
        text-align: center;
        text-style: bold;
        color: $accent;
    }
    #wordlist-choose-row {
        height: auto;
    }
    #wordlist-choose {
        width: 1fr;
    }
    #wordlist-clear {
        width: 7;
    }
    .wordlist-remove {
        width: 3;
    }
    #wordlists {
        height: auto;
        max-height: 10;
    }
    .wordlist-row {
        height: 1;
    }
    .wordlist-name {
        width: 1fr;
        margin-left: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    #default-passwords {
        margin-top: 1;
    }
    #wordlist-buttons {
        margin-top: 2;
        align: right middle;
        height: auto;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="wordlist-dialog"):
            yield Label(self.dialog_title, id="wordlist-title")
            yield from self._compose_fields()
            with Horizontal(id="wordlist-buttons"):
                yield Button("Cancel", variant="error", id="wordlist-cancel")
                yield Button(self._confirm_label, variant="success", id="wordlist-launch")

    def _compose_fields(self) -> ComposeResult:
        yield from self._compose_wordlists()

    def _compose_wordlists(self) -> ComposeResult:
        with Horizontal(id="wordlist-choose-row"):
            yield Button(f"{BROWSE_GLYPH} Choose wordlists", "primary", id="wordlist-choose")
            yield Button("Clear", variant="error", id="wordlist-clear")
        yield VerticalScroll(id="wordlists")

    def _compose_password_sources(self) -> ComposeResult:
        settings = self._settings()
        yield Checkbox("Default passwords", value=settings.default_passwords,
                       id="default-passwords")
        yield Checkbox("This AP's SSID variants", value=settings.ap_ssid_passwords,
                       tooltip="Passwords based on the AP's SSID: cases, leetspeak, separators, "
                               "common suffixes and numbers 0-9999",
                       id="ap-ssid-passwords")
        yield Checkbox("Probed SSIDs", value=settings.probed_ssid_passwords,
                       tooltip="Passwords based on SSIDs probed this session (clients may "
                               "mistype the PSK into the SSID field)",
                       id="probed-ssid-passwords")

    async def on_mount(self) -> None:
        await self._show_wordlists()
        self._show_counts_and_exhaustion()

    @work
    async def _show_counts_and_exhaustion(self) -> None:
        counts = await asyncio.to_thread(_count_password_sources, self._capture,
                                         self._probed_ssids)
        try:
            default = self.query_one("#default-passwords", Checkbox)
        except NoMatches:   # dismissed while counting
            return
        default.label = f"Default passwords ({counts.default_passwords:,})"
        default.tooltip = f"RockYou's top {counts.default_passwords:,} passwords by frequency"
        ap_ssid = (f"{counts.ap_ssid_passwords:,}" if counts.ap_ssid_passwords is not None
                   else f"about {_ssid_variants_per_ssid():,} per SSID")
        self.query_one("#ap-ssid-passwords", Checkbox).label = f"This AP's SSID variants ({ap_ssid})"
        self.query_one("#probed-ssid-passwords", Checkbox).label = (
            f"Probed SSIDs ({counts.probed_ssid_passwords:,})")
        if self._capture is not None:
            self._exhaustion = await asyncio.to_thread(_read_exhaustion, self._capture)
            self._mark_exhausted()

    def _mark_exhausted(self) -> None:
        if not self._exhaustion:
            return
        for row in self.query(".wordlist-row"):
            self._mark(row, _file_identity(row.query_one(_RemoveButton).path))
        for checkbox in self.query("#default-passwords"):
            self._mark(checkbox, _file_identity(DEFAULT_PASSWORDS_PATH))
        for checkbox in self.query("#ap-ssid-passwords"):
            self._mark(checkbox, SSID_VARIANTS_IDENTITY)

    def _mark(self, widget: Widget, identity: Optional[str]) -> None:
        exhaustion = self._exhaustion.get(identity) if identity else None
        if exhaustion is not None:
            widget.add_class("exhausted")
            widget.tooltip = _exhausted_tooltip(exhaustion)

    async def _show_wordlists(self) -> None:
        self.query_one("#wordlist-clear", Button).display = bool(self._wordlists)
        rows = self.query_one("#wordlists", VerticalScroll)
        await rows.remove_children()
        if not self._wordlists:
            await rows.mount(Label("[dim]none chosen[/dim]"))
            return
        await rows.mount_all(
            Horizontal(_RemoveButton(path),
                       Label(f"[bold]{number}:[/bold] {escape(Path(path).name)}  "
                             f"[dim]{escape(str(Path(path).parent))}[/dim]",
                             classes="wordlist-name"),
                       classes="wordlist-row")
            for number, path in enumerate(self._wordlists, 1))
        self._mark_exhausted()

    @on(Button.Pressed, ".wordlist-remove")
    async def _remove_wordlist(self, event: Button.Pressed) -> None:
        event.stop()
        assert isinstance(event.button, _RemoveButton)
        self._wordlists.remove(event.button.path)
        await self._show_wordlists()

    @on(Button.Pressed, "#wordlist-clear")
    async def _clear_wordlists(self, event: Button.Pressed) -> None:
        event.stop()
        self._wordlists = []
        await self._show_wordlists()

    @on(Button.Pressed, "#wordlist-choose")
    def _choose_wordlists(self, event: Button.Pressed) -> None:
        event.stop()
        start = self._wordlists[0] if self._wordlists else str(Path.home())
        self.app.push_screen(
            PathPickerModal(start, title="Select wordlist(s)", multiple=True,
                            checked=[Path(path) for path in self._wordlists]),
            self._wordlists_chosen,
        )

    async def _wordlists_chosen(self, paths: Optional[list[Path]]) -> None:
        if paths is None:
            return
        self._wordlists = [str(path) for path in paths]
        await self._show_wordlists()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#wordlist-cancel")
    def _cancel(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_cancel()

    @on(Button.Pressed, "#wordlist-launch")
    def _launch(self, event: Button.Pressed) -> None:
        event.stop()
        config = self._launch_config()
        if config is None:
            return
        self._remember(config)
        self.dismiss(config)

    def _launch_config(self) -> Optional[dict[str, Any]]:
        config = {"wordlists": list(self._wordlists),
                  "default_passwords": self._checked("#default-passwords"),
                  "ap_ssid_passwords": self._checked("#ap-ssid-passwords"),
                  "probed_ssid_passwords": self._checked("#probed-ssid-passwords")}
        if not any(config.values()):
            self.notify("Please choose a wordlist or a password source", severity="error")
            return None
        return config

    def _checked(self, selector: str) -> bool:
        return self.query_one(selector, Checkbox).value

    def _remember(self, config: dict[str, Any]) -> None:
        """Persist the launch settings so the next job starts pre-filled."""
        self._store_settings(config)
        try:
            Config.save()
        except ConfigError as e:
            self.notify(str(e), title="Config Error", severity="warning")

    def _settings(self) -> CpuCrackSettings | HashcatSettings:
        """The Config table this tool's wordlists are saved in."""
        raise NotImplementedError

    def _store_settings(self, config: dict[str, Any]) -> None:
        settings = self._settings()
        settings.wordlists = list(config["wordlists"])
        settings.default_passwords = config["default_passwords"]
        settings.ap_ssid_passwords = config["ap_ssid_passwords"]
        settings.probed_ssid_passwords = config["probed_ssid_passwords"]
