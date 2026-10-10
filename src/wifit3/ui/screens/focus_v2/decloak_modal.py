"""Edit the exact candidate SSIDs and pick which methods send them, before Decloak starts."""
from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, TextArea

from wifit3.campaigns.decloak import (
    SSID_MAX_OCTETS,
    DecloakInput,
    candidates_from_sibling,
    named_sibling_ssid,
)
from wifit3.models import AccessPoint
from wifit3.wlan.array import WlanArray

SSID_TOKEN = "$ssid"


def expand_candidates(text: str, base: str) -> list[str]:
    """One SSID per line, with SSID_TOKEN replaced by ``base``. Blank lines are skipped;
    surviving lines keep their spacing, since an AP matches an SSID byte for byte. Drops
    duplicates and anything over SSID_MAX_OCTETS. Raises ValueError on a token with no base."""
    out: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if SSID_TOKEN in line and not base:
            raise ValueError(f"{SSID_TOKEN} needs a base SSID")
        ssid = line.replace(SSID_TOKEN, base)
        if ssid not in out and len(ssid.encode("utf-8")) <= SSID_MAX_OCTETS:
            out.append(ssid)
    return out


def decloak_defaults(array: WlanArray, ap: AccessPoint) -> tuple[str, list[str]]:
    """The base SSID to offer and the candidate list to prefill, from the AP's siblings."""
    base = named_sibling_ssid(array, ap)
    return base, candidates_from_sibling(base)


class DecloakInputModal(ModalScreen[Optional[DecloakInput]]):
    """Review the SSIDs a decloak sweep will send, and which methods will send them."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=True)]

    DEFAULT_CSS = """
    DecloakInputModal { align: center middle; }
    DecloakInputModal #dialog {
        width: 64; height: auto; max-height: 90%;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    DecloakInputModal #title { content-align: center middle; text-style: bold; }
    DecloakInputModal #hint { color: $text-muted; margin-bottom: 1; }
    DecloakInputModal TextArea { height: 12; margin-bottom: 1; }
    DecloakInputModal #methods { height: auto; margin-bottom: 1; }
    DecloakInputModal #methods Checkbox {
        border: none; height: 1; padding: 0 1; background: transparent;
    }
    DecloakInputModal #warn { color: $error; display: none; }
    DecloakInputModal #button-row { height: auto; align: center middle; }
    """

    def __init__(self, bssid: str, base: str, prefill: list[str],
                 chosen: Optional[DecloakInput] = None) -> None:
        super().__init__()
        self._bssid = bssid
        start = chosen or DecloakInput()
        self._base = Input(value=base, placeholder="base SSID (optional)", id="base")
        self._candidates = TextArea("\n".join(prefill), id="candidates")
        self._directed = Checkbox("Directed Probe Requests",
                                  value=start.use_directed_probe, id="use-directed")
        self._broadcast = Checkbox("Broadcast Probe Requests",
                                   value=start.use_broadcast_probe, id="use-broadcast")
        self._authassoc = Checkbox("Auth + Association Requests",
                                   value=start.use_authassoc_probe, id="use-authassoc")
        self._warn = Label("", id="warn")
        self._accept_button = Button("Decloak", variant="primary", id="btn-accept")
        self._reviewed = False
        self.warning = ""                 # last message shown in #warn, for tests

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Decloak {self._bssid}", id="title")
            yield Label(f"One SSID per line. {SSID_TOKEN} becomes the base SSID.", id="hint")
            yield self._base
            yield self._candidates
            with Vertical(id="methods"):
                yield self._directed
                yield self._broadcast
                yield self._authassoc
            yield self._warn
            with Horizontal(id="button-row"):
                yield self._accept_button
                yield Button("Cancel", id="btn-cancel")

    def _set_warn(self, message: str) -> None:
        self.warning = message
        self._warn.update(message)
        self._warn.display = bool(message)

    def _chosen_methods(self) -> DecloakInput:
        return DecloakInput([], self._directed.value, self._broadcast.value,
                            self._authassoc.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button is self._accept_button:
            self._accept()
        else:
            self.dismiss(None)

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        self._reviewed = False
        self._accept_button.label = "Decloak"
        self._set_warn("")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _accept(self) -> None:
        """First press normalises the list and shows it; the second sends it. Seeing the
        exact SSIDs matters because an AP only answers a byte-for-byte match."""
        try:
            ssids = expand_candidates(self._candidates.text, self._base.value)
        except ValueError as exc:
            self._set_warn(str(exc))
            return
        if not ssids:
            self._set_warn("No candidate SSIDs to send")
            return
        methods = self._chosen_methods()
        if not any((methods.use_directed_probe, methods.use_broadcast_probe,
                    methods.use_authassoc_probe)):
            self._set_warn("Pick at least one method")
            return
        normalised = "\n".join(ssids)
        if not self._reviewed or self._candidates.text != normalised:
            self._candidates.load_text(normalised)
            self._reviewed = True
            self._accept_button.label = "Confirm Decloak"
            self._set_warn("")
            total = len(ssids) * sum((methods.use_directed_probe,
                                      methods.use_broadcast_probe,
                                      methods.use_authassoc_probe))
            self._set_warn(f"{len(ssids)} SSIDs, {total} requests. Confirm to send.")
            return
        methods.candidate_ssids = ssids
        self.dismiss(methods)
