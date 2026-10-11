"""Edit the exact candidate SSIDs and pick which methods send them, before Decloak starts."""
from pathlib import Path
from typing import Iterable, Optional

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, TextArea

from wifit3.campaigns.decloak import (
    ASSOC_TIMEOUT_S,
    PROBE_RESPONSE_WAIT_S,
    DecloakInput,
    candidates_from_sibling,
    named_sibling_ssid,
)
from wifit3.dot11 import SSID_MAX_OCTETS
from wifit3.models import AccessPoint
from wifit3.persist.config import Config
from wifit3.ui.path_picker import PathInput
from wifit3.wlan.array import WlanArray

SSID_TOKEN = "$ssid"
TRUNCATE_BUDGETS_S = (300, 1200, 3600)


def sendable_ssids(names: Iterable[str]) -> list[str]:
    """``names`` minus duplicates and anything over SSID_MAX_OCTETS, order kept."""
    out: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name not in seen and len(name.encode("utf-8")) <= SSID_MAX_OCTETS:
            seen.add(name)
            out.append(name)
    return out


def expand_candidates(text: str, base: str) -> list[str]:
    """One SSID per line, with SSID_TOKEN replaced by ``base``. Blank lines are skipped;
    surviving lines keep their spacing, since an AP matches an SSID byte for byte. Raises
    ValueError on a token with no base."""
    expanded: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if SSID_TOKEN in line and not base:
            raise ValueError(f"{SSID_TOKEN} needs a base SSID")
        expanded.append(line.replace(SSID_TOKEN, base))
    return sendable_ssids(expanded)


def wordlist_names(path: Path) -> int:
    """Lines in the file, counted a megabyte at a time: a wordlist can be gigabytes."""
    count, last = 0, b"\n"
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            count += chunk.count(b"\n")
            last = chunk[-1:]
    return count + (last != b"\n")


def wordlist_head(path: Path, limit: int) -> list[str]:
    """The first ``limit`` non-blank lines, byte for byte as written. A line that is not
    UTF-8 is skipped: an SSID we cannot decode is one we cannot send."""
    names: list[str] = []
    if limit <= 0:
        return names
    with path.open("rb") as handle:
        for raw in handle:
            try:
                name = raw.rstrip(b"\r\n").decode("utf-8")
            except UnicodeDecodeError:
                continue
            if name.strip():
                names.append(name)
                if len(names) >= limit:
                    break
    return names


def sweep_seconds(ssids: int, methods: DecloakInput) -> float:
    """How long the run takes: every enabled method waits out its own reply window once per
    SSID."""
    per_ssid = (PROBE_RESPONSE_WAIT_S * methods.use_directed_probe
                + PROBE_RESPONSE_WAIT_S * methods.use_broadcast_probe
                + ASSOC_TIMEOUT_S * methods.use_authassoc_probe)
    return ssids * per_ssid


def ssids_within(budget_s: float, methods: DecloakInput) -> int:
    """How many SSIDs fit in ``budget_s``; 0 when no method is enabled."""
    one = sweep_seconds(1, methods)
    return int(budget_s / one) if one else 0


def candidate_prefill(base: str, remembered: Optional[str]) -> list[str]:
    """One guess per sibling suffix, with an SSID remembered for this BSSID first."""
    guesses = candidates_from_sibling(base)
    return sendable_ssids([remembered, *guesses]) if remembered else guesses


def decloak_defaults(array: WlanArray, ap: AccessPoint) -> tuple[str, list[str]]:
    """The base SSID to offer and the candidate list to prefill."""
    base = named_sibling_ssid(array, ap)
    return base, candidate_prefill(base, Config.decloaked_ssid(ap.bssid))


def _span(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} h"
    if seconds >= 60:
        return f"{seconds / 60:.0f} min"
    return f"{seconds:.0f}s"


class DecloakInputModal(ModalScreen[Optional[DecloakInput]]):
    """Review the SSIDs a decloak sweep will send, and which methods will send them."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=True)]

    DEFAULT_CSS = """
    DecloakInputModal { align: center middle; }
    DecloakInputModal #dialog {
        width: 64; height: auto; max-height: 90%; overflow-y: auto;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    DecloakInputModal #title { content-align: center middle; text-style: bold; }
    DecloakInputModal #hint { color: $text-muted; }
    DecloakInputModal TextArea { height: 8; margin-bottom: 1; }
    DecloakInputModal PathInput { margin-bottom: 1; }
    DecloakInputModal #methods { height: auto; }
    DecloakInputModal #methods Checkbox {
        border: none; height: 1; padding: 0 1; background: transparent;
    }
    DecloakInputModal #warn { color: $error; display: none; }
    DecloakInputModal #truncate { height: 1; display: none; }
    DecloakInputModal #truncate Label { padding: 0 1 0 0; color: $text-muted; }
    DecloakInputModal #button-row { height: auto; align: center middle; }
    """

    def __init__(self, bssid: str, base: str, prefill: list[str],
                 chosen: Optional[DecloakInput] = None) -> None:
        super().__init__()
        self._bssid = bssid
        start = chosen or DecloakInput()
        self._base = Input(value=base, placeholder="base SSID (optional)", id="base")
        self._generated_from = base            # the base the list was last built from
        self._generated = "\n".join(prefill)   # the list as we last wrote it; see _base_changed
        self._candidates = TextArea(self._generated, id="candidates")
        self._remembered = Config.decloaked_ssid(bssid)
        self._wordlist_input = PathInput("", title="Select wordlist",
                                         placeholder="wordlist file (optional)", id="wordlist")
        self._directed = Checkbox("Directed Probe Requests",
                                  value=start.use_directed_probe, id="use-directed")
        self._broadcast = Checkbox("Broadcast Probe Requests",
                                   value=start.use_broadcast_probe, id="use-broadcast")
        self._authassoc = Checkbox("Auth + Association Requests",
                                   value=start.use_authassoc_probe, id="use-authassoc")
        self._warn = Label("", id="warn")
        self._truncate_row = Horizontal(id="truncate")
        self._accept_button = Button("Decloak", variant="primary", id="btn-accept")
        self._wordlist: Optional[Path] = None   # picked file, sent after the typed list
        self._wordlist_names = 0
        self._budget_s: Optional[int] = None    # None sends every candidate, however long
        self._reviewed = False
        self.warning = ""                 # last message shown in #warn, for tests

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Decloak {self._bssid}", id="title")
            yield Label(f"One SSID per line. {SSID_TOKEN} becomes the base SSID.", id="hint")
            yield self._base
            yield self._candidates
            yield self._wordlist_input
            with Vertical(id="methods"):
                yield self._directed
                yield self._broadcast
                yield self._authassoc
            yield self._warn
            with self._truncate_row:
                yield Label("Truncate:")
                for budget in TRUNCATE_BUDGETS_S:
                    yield Button(f"{budget // 60} min", id=f"truncate-{budget}", compact=True)
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
        elif event.button.id and event.button.id.startswith("truncate-"):
            self._truncate(int(event.button.id.removeprefix("truncate-")))
        elif event.button.id == "btn-cancel":
            self.dismiss(None)

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        self._unreview()
        self._set_warn("")

    @on(Input.Changed, "#base")
    def _base_changed(self, event: Input.Changed) -> None:
        """Re-guess from the new base, unless the list has been hand-edited: the base is there
        to drive the list, but once the user has typed in it the list is theirs. Input posts
        this on mount too, which must not overwrite the prefill we were handed."""
        if event.value == self._generated_from or self._candidates.text != self._generated:
            return
        self._generated_from = event.value
        self._fill_candidates(candidate_prefill(event.value, self._remembered))
        self._unreview()
        self._set_warn("")

    def _fill_candidates(self, ssids: list[str]) -> None:
        self._generated = "\n".join(ssids)
        self._candidates.load_text(self._generated)

    @on(Input.Changed, "#wordlist")
    def _wordlist_chosen(self, event: Input.Changed) -> None:
        """Count the file and decide how much of it a run can afford. Only as much as the
        budget allows is ever read, so a rockyou-sized list costs one pass to count."""
        path = Path(event.value)
        if not event.value or not path.is_file():
            return
        try:
            names = wordlist_names(path)
        except OSError as exc:
            self._set_warn(f"{path.name}: {exc.strerror or exc}")
            return
        methods = self._chosen_methods()
        self._wordlist, self._wordlist_names = path, names
        whole = sweep_seconds(names, methods)
        if names <= ssids_within(TRUNCATE_BUDGETS_S[-1], methods):
            self._budget_s = None
            self._set_warn(f"{path.name}: {names:,} names, {_span(whole)}")
        else:
            self._budget_s = TRUNCATE_BUDGETS_S[0]
            self._set_warn(f"{path.name}: {names:,} names, {_span(whole)} in full "
                           f"- truncated to {self._budget_s // 60} min")
        self._truncate_row.display = True
        self._unreview()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _unreview(self) -> None:
        """Send the dialog back to its review step: the plan changed under the user."""
        self._reviewed = False
        self._accept_button.label = "Decloak"

    def _plan(self) -> Optional[tuple[list[str], list[str]]]:
        """(the typed list the run will send, every SSID it will send), or None once the
        reason it cannot be built is in #warn. The budget cuts the typed list too, so what
        the box ends up showing is what goes out; the wordlist's share is counted, not shown."""
        methods = self._chosen_methods()
        try:
            typed = expand_candidates(self._candidates.text, self._base.value)
        except ValueError as exc:
            self._set_warn(str(exc))
            return None
        limit = ssids_within(self._budget_s, methods) if self._budget_s else None
        if limit is not None:
            typed = typed[:limit]
        ssids = typed
        if self._wordlist is not None:
            room = self._wordlist_names if limit is None else max(0, limit - len(ssids))
            try:
                ssids = sendable_ssids(ssids + wordlist_head(self._wordlist, room))
            except OSError as exc:
                self._set_warn(f"{self._wordlist.name}: {exc.strerror or exc}")
                return None
        return typed, ssids

    def _plan_line(self, ssids: list[str], methods: DecloakInput) -> str:
        enabled = sum((methods.use_directed_probe, methods.use_broadcast_probe,
                       methods.use_authassoc_probe))
        return (f"{len(ssids):,} SSIDs, {len(ssids) * enabled:,} requests, "
                f"{_span(sweep_seconds(len(ssids), methods))}")

    def _truncate(self, budget_s: int) -> None:
        """Cut the run to what fits ``budget_s``, taking the typed list first and filling the
        rest from the wordlist."""
        self._budget_s = budget_s
        self._unreview()
        plan = self._plan()
        if plan is None:
            return
        self._set_warn(f"{self._plan_line(plan[1], self._chosen_methods())} "
                       f"(truncated to {budget_s // 60} min)")

    def _accept(self) -> None:
        """First press normalises the list and shows what the run costs; the second sends it.
        Seeing the exact SSIDs matters because an AP only answers a byte-for-byte match."""
        methods = self._chosen_methods()
        if not any((methods.use_directed_probe, methods.use_broadcast_probe,
                    methods.use_authassoc_probe)):
            self._set_warn("Pick at least one method")
            return
        plan = self._plan()
        if plan is None:
            return
        typed, ssids = plan
        if not ssids:
            self._set_warn("No candidate SSIDs to send")
            return
        normalised = "\n".join(typed)
        if not self._reviewed or self._candidates.text != normalised:
            self._fill_candidates(typed)
            self._reviewed = True
            self._accept_button.label = "Confirm"
            self._truncate_row.display = (
                self._wordlist is not None
                or len(ssids) > ssids_within(TRUNCATE_BUDGETS_S[0], methods))
            self._set_warn(f"{self._plan_line(ssids, methods)}. Confirm to send.")
            return
        methods.candidate_ssids = ssids
        self.dismiss(methods)
