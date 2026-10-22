from typing import Optional

from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.widgets import Footer, DataTable, Input
from textual.screen import ModalScreen
from textual import events, on
from textual.binding import Binding

from .vault_table import VaultTable
from .vault_item import VaultItemView
from wifit3.persist.meta import meta_path, read_meta, update_meta
from wifit3.ui.vault.job_pane import JobTrackerPane
from wifit3.ui.vault.modals.nickname import NicknameModal

class VaultDrawer(ModalScreen):
    """The Vault drawer overlay."""

    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (90, "-wide")]

    BINDINGS = [
        Binding("v", "dismiss_drawer", "Close", show=False),
        Binding("escape", "dismiss_drawer", "Close", show=True),
        Binding("f", "focus_search", "Search", show=True),
        Binding("/", "focus_search", "Search", show=False),
        Binding("n", "set_nickname", "Nickname", show=True),
        Binding("z", "export_zip", "Export Zip", show=True),
        Binding("o", "show_directory", "Show Dir", show=True),
        Binding("q", "app.quit", "Quit", show=True),
    ]

    CSS = """
    VaultDrawer {
        align: center bottom;
        background: rgba(0, 0, 0, 0.4);
    }
    #vault-content {
        width: 100%;
        max-height: 75%;
        background: $surface;
        offset-y: 100%;
        transition: offset 200ms in_out_cubic;
    }
    VaultDrawer.-narrow _CapturePanel .tools, VaultDrawer.-narrow _CapturePanel .actions {
        layout: vertical;
    }
    VaultDrawer.-narrow _CapturePanel .spacer { display: none; }
    VaultDrawer.open #vault-content {
        offset-y: 0%;
    }
    """

    def __init__(self, preselected_bssid: Optional[str] = None) -> None:
        super().__init__()
        self._preselected_bssid = preselected_bssid

    def compose(self) -> ComposeResult:
        with Container(id="vault-content"):
            with Horizontal():
                yield VaultTable(id="vault-table")
                yield VaultItemView(id="vault-item")
            yield JobTrackerPane()
            yield Footer()

    def on_mount(self) -> None:
        self.call_after_refresh(lambda: self.add_class("open"))
        self.call_after_refresh(self._focus_list)

    def _focus_list(self) -> None:
        """Land focus on the AP list so up/down works without a Tab press."""
        try:
            self.query_one("#vault-aps", DataTable).focus()
        except Exception:
            pass

    def action_focus_search(self) -> None:
        self.query_one("#vault-search", Input).focus()

    def action_dismiss_drawer(self) -> None:
        try:
            self.query_one("#vault-item", VaultItemView).cancel_verification()
        except Exception:
            pass
        if self.has_class("open"):
            self.remove_class("open")
            def _do_dismiss():
                self.dismiss()
            self.set_timer(0.35, _do_dismiss)

    @on(events.Click)
    def on_click(self, event: events.Click) -> None:
        # Dismiss if clicking outside the #vault-content container
        # Since ModalScreen catches clicks outside its children, `event.control == self` means background.
        if event.control == self:
            self.action_dismiss_drawer()

    def _load_widget(self, bssid: str) -> None:
        widget = self.query_one("#vault-item", VaultItemView)
        table = self.query_one("#vault-table", VaultTable)
        entry = table._aps.get(bssid)
        if entry is not None:
            widget.load(bssid, table.display_name(bssid, entry[0]), entry[1])
        else:
            widget.load("", None, [])

    @on(VaultTable.TableReloaded)
    def _table_reloaded(self, event: VaultTable.TableReloaded) -> None:
        table = self.query_one("#vault-aps", DataTable)
        if table.row_count > 0:
            if self._preselected_bssid in table.rows:
                table.move_cursor(row=table.get_row_index(self._preselected_bssid))
            self._preselected_bssid = None   # the opening screen's AP, honored once
            try:
                key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
                self._load_widget(key)
            except Exception:
                from textual.coordinate import Coordinate
                key = table.coordinate_to_cell_key(Coordinate(0, 0)).row_key.value
                self._load_widget(key)
        else:
            self._load_widget("")

    @on(DataTable.RowHighlighted, "#vault-aps")
    def _ap_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value is not None:
            self._load_widget(event.row_key.value)

    def _selected_network(self) -> tuple[Optional[str], str]:
        """(SSID, BSSID) of the row under the cursor; BSSID is "" when there is none."""
        aps = self.query_one("#vault-aps", DataTable)
        if aps.row_count == 0:
            return None, ""
        bssid = aps.coordinate_to_cell_key(aps.cursor_coordinate).row_key.value or ""
        entry = self.query_one("#vault-table", VaultTable)._aps.get(bssid)
        return (entry[0] if entry else None), bssid

    def action_set_nickname(self) -> None:
        ssid, bssid = self._selected_network()
        if not bssid:
            self.notify("Select an AP first", severity="warning")
            return
        meta = read_meta(meta_path(ssid, bssid))
        nickname = meta.get("nickname")
        current = nickname if isinstance(nickname, str) else ""
        meta_ssid = meta.get("ssid")
        real_ssid = meta_ssid if isinstance(meta_ssid, str) else ssid   # row SSIDs are filename-safe

        def _save(value: Optional[str]) -> None:
            if value is None:
                return
            try:
                update_meta(real_ssid, bssid, nickname=value or None)
            except OSError as exc:
                self.notify(f"Could not save nickname: {exc}", severity="error")
                return
            self.query_one("#vault-table", VaultTable).reload_nicknames()
            self.notify("Nickname saved" if value else "Nickname cleared")

        self.app.push_screen(NicknameModal(real_ssid, current), _save)

    def action_export_zip(self) -> None:
        try:
            out = self.app.vault.zip_captures(self.app.vault.all_captures())
        except OSError as exc:
            self.notify(f"Export failed: {exc}", severity="error")
            return
        if out is None:
            self.notify("Nothing to export", severity="warning")
            return
        self.notify(f"Exported to {out}")

    def action_show_directory(self) -> None:
        try:
            self.app.vault.open_directory()
        except OSError as exc:
            self.notify(f"Could not open captures dir: {exc}", severity="error")
