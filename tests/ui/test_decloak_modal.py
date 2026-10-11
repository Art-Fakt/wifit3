"""The decloak modal's candidate expansion and the method flags it hands the campaign."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from wifit3.campaigns.decloak import DecloakCampaign, DecloakInput
from wifit3.models import AccessPoint
from wifit3.persist.config import Config
from wifit3.ui.screens.focus_v2.decloak_modal import (
    SSID_TOKEN,
    decloak_defaults,
    expand_candidates,
    sweep_seconds,
    ssids_within,
    wordlist_head,
    wordlist_names,
)

_BSSID = "aa:bb:cc:dd:ee:ff"


# ----- candidate expansion -----------------------------------------------------


def test_token_expands_to_the_base_ssid():
    assert expand_candidates(f"{SSID_TOKEN}-Guest", "Net") == ["Net-Guest"]


def test_token_without_a_base_is_rejected():
    with pytest.raises(ValueError):
        expand_candidates(f"{SSID_TOKEN}-Guest", "")


def test_literal_lines_need_no_base():
    assert expand_candidates("Alpha\nBeta", "") == ["Alpha", "Beta"]


def test_blank_lines_are_skipped_but_spacing_is_kept():
    # An AP matches an SSID byte for byte, so " Net" must survive as its own candidate.
    assert expand_candidates("\n  \nNet\n Net\n", "") == ["Net", " Net"]


def test_duplicates_collapse_preserving_order():
    assert expand_candidates("B\nA\nB", "") == ["B", "A"]


def test_names_over_32_octets_are_dropped():
    assert expand_candidates("X" * 33 + "\nok", "") == ["ok"]
    assert expand_candidates("é" * 17, "") == []          # 34 octets, 17 characters


def test_defaults_come_from_the_named_sibling():
    array = MagicMock()
    sibling = AccessPoint(bssid="aa:bb:cc:dd:ee:f0", channel=6)
    sibling.ssid, sibling.beacons = "Loud", 9
    array.access_points = {sibling.bssid: sibling}
    hidden = AccessPoint(bssid=_BSSID, channel=6)
    hidden.siblings.append(sibling.bssid)

    base, prefill = decloak_defaults(array, hidden)

    assert base == "Loud"
    assert prefill[0] == "Loud"
    assert "Loud-Guest" in prefill


def test_a_remembered_ssid_is_offered_first(monkeypatch):
    monkeypatch.setattr(Config, "decloaked_ssids", {_BSSID: "Loud-Guest"})
    array = MagicMock()
    sibling = AccessPoint(bssid="aa:bb:cc:dd:ee:f0", channel=6)
    sibling.ssid, sibling.beacons = "Loud", 9
    array.access_points = {sibling.bssid: sibling}
    hidden = AccessPoint(bssid=_BSSID, channel=6)
    hidden.siblings.append(sibling.bssid)

    _base, prefill = decloak_defaults(array, hidden)

    assert prefill[0] == "Loud-Guest"
    assert prefill.count("Loud-Guest") == 1


# ----- what the campaign does with the modal's output --------------------------


def _campaign(chosen):
    array = MagicMock()
    array.access_points = {}
    return DecloakCampaign(array, AccessPoint(bssid=_BSSID, channel=6), chosen)


def test_methods_carry_through_to_the_campaign():
    campaign = _campaign(DecloakInput(["A"], use_directed_probe=False,
                                      use_broadcast_probe=True, use_authassoc_probe=True))

    assert not campaign.use_directed_probe
    assert campaign.use_broadcast_probe and campaign.use_authassoc_probe


def test_planned_counts_every_enabled_method_over_every_ssid():
    campaign = _campaign(DecloakInput(["A", "B"], use_broadcast_probe=True,
                                      use_authassoc_probe=True))
    assert campaign.planned == 6

    campaign.use_broadcast_probe = False
    campaign.use_authassoc_probe = False
    assert campaign.planned == 2


def test_an_explicit_empty_list_does_not_fall_back_to_sibling_guesses():
    sibling = AccessPoint(bssid="aa:bb:cc:dd:ee:f0", channel=6)
    sibling.ssid, sibling.beacons = "Loud", 9
    array = MagicMock()
    array.access_points = {sibling.bssid: sibling}
    hidden = AccessPoint(bssid=_BSSID, channel=6)
    hidden.siblings.append(sibling.bssid)

    campaign = DecloakCampaign(array, hidden, DecloakInput([]))

    assert campaign.candidate_ssids == []
    assert campaign.planned == 0


def test_no_input_falls_back_to_sibling_guesses():
    sibling = AccessPoint(bssid="aa:bb:cc:dd:ee:f0", channel=6)
    sibling.ssid, sibling.beacons = "Loud", 9
    array = MagicMock()
    array.access_points = {sibling.bssid: sibling}
    hidden = AccessPoint(bssid=_BSSID, channel=6)
    hidden.siblings.append(sibling.bssid)

    campaign = DecloakCampaign(array, hidden)

    assert campaign.candidate_ssids[0] == "Loud"
    assert campaign.use_directed_probe
    assert not campaign.use_broadcast_probe


# ----- the modal on screen -----------------------------------------------------


async def test_two_presses_confirm_the_normalised_list():
    """load_text posts TextArea.Changed, so the review state must survive our own rewrite."""
    from textual.app import App
    from wifit3.ui.screens.focus_v2.decloak_modal import DecloakInputModal

    app, got = App(), []
    async with app.run_test() as pilot:
        modal = DecloakInputModal(_BSSID, "Net", ["$ssid", "$ssid-Guest", "$ssid-Guest", ""])
        app.push_screen(modal, got.append)
        await pilot.pause()
        modal._broadcast.value = True
        await pilot.pause()

        modal._accept()                                   # first press: review only
        await pilot.pause()
        assert got == []
        assert modal._candidates.text == "Net\nNet-Guest"
        assert "2 SSIDs, 4 requests" in modal.warning

        modal._accept()                                   # second press: send
        await pilot.pause()

    assert got[0] == DecloakInput(["Net", "Net-Guest"], use_directed_probe=True,
                                 use_broadcast_probe=True)


async def test_editing_the_base_re_guesses_the_list():
    from textual.app import App
    from textual.widgets import Input
    from wifit3.ui.screens.focus_v2.decloak_modal import DecloakInputModal, candidate_prefill

    app = App()
    async with app.run_test() as pilot:
        modal = DecloakInputModal(_BSSID, "Beachball 2.4",
                                  candidate_prefill("Beachball 2.4", None))
        app.push_screen(modal, lambda result: None)
        await pilot.pause()
        assert "Beachball 2.4-Guest" in modal._candidates.text

        modal.query_one("#base", Input).value = "Beachball"
        await pilot.pause()

        assert "Beachball-Guest" in modal._candidates.text
        assert "Beachball 2.4-Guest" not in modal._candidates.text


async def test_a_hand_edited_list_outranks_the_base():
    from textual.app import App
    from textual.widgets import Input
    from wifit3.ui.screens.focus_v2.decloak_modal import DecloakInputModal, candidate_prefill

    app = App()
    async with app.run_test() as pilot:
        modal = DecloakInputModal(_BSSID, "Loud", candidate_prefill("Loud", None))
        app.push_screen(modal, lambda result: None)
        await pilot.pause()
        modal._candidates.load_text("Beachball\nBeachball-Guest")
        await pilot.pause()

        modal.query_one("#base", Input).value = "Quiet"
        await pilot.pause()

        assert modal._candidates.text == "Beachball\nBeachball-Guest"


async def test_a_long_wordlist_is_cut_to_a_time_budget(tmp_path):
    """24,001 names is past the longest budget, so the file is never loaded whole: the modal
    picks the shortest budget and the buttons move it."""
    from textual.app import App
    from textual.widgets import Input
    from wifit3.ui.screens.focus_v2.decloak_modal import DecloakInputModal

    wordlist = tmp_path / "huge.txt"
    wordlist.write_text("\n".join(f"n{i}" for i in range(24_001)), encoding="utf-8")

    app, got = App(), []
    async with app.run_test() as pilot:
        modal = DecloakInputModal(_BSSID, "", ["Net"])
        app.push_screen(modal, got.append)
        await pilot.pause()
        modal.query_one("#wordlist", Input).value = str(wordlist)
        await pilot.pause()

        assert "24,001 names" in modal.warning and "truncated to 5 min" in modal.warning

        modal._accept()                                   # first press: review only
        await pilot.pause()
        assert "2,000 SSIDs" in modal.warning              # 300s / 0.15s per directed probe

        modal._truncate(1200)
        await pilot.pause()
        assert "8,000 SSIDs" in modal.warning

        modal._accept()
        await pilot.pause()
        modal._accept()
        await pilot.pause()

    assert len(got[0].candidate_ssids) == 8000
    assert got[0].candidate_ssids[:2] == ["Net", "n0"]      # typed list first, then the file


def test_wordlist_names_counts_a_file_with_no_trailing_newline(tmp_path):
    path = tmp_path / "w.txt"
    path.write_bytes(b"Alpha\nBeta")
    assert wordlist_names(path) == 2


def test_wordlist_head_keeps_spacing_and_skips_undecodable_lines(tmp_path):
    path = tmp_path / "w.txt"
    path.write_bytes(b" Alpha \n\n\xff\xfe\nBeta\r\n")
    assert wordlist_head(path, 9) == [" Alpha ", "Beta"]
    assert wordlist_head(path, 1) == [" Alpha "]
    assert wordlist_head(path, 0) == []


def test_run_cost_counts_every_enabled_method():
    directed = DecloakInput([])
    assert sweep_seconds(10, directed) == pytest.approx(1.5)
    assert ssids_within(300, directed) == 2000
    assert ssids_within(300, DecloakInput([], True, True, True)) == 500
    assert ssids_within(300, DecloakInput([], False, False, False)) == 0


async def test_no_method_selected_is_refused():
    from textual.app import App
    from wifit3.ui.screens.focus_v2.decloak_modal import DecloakInputModal

    app, got = App(), []
    async with app.run_test() as pilot:
        modal = DecloakInputModal(_BSSID, "", ["Alpha"])
        app.push_screen(modal, got.append)
        await pilot.pause()
        modal._directed.value = False
        await pilot.pause()
        modal._accept()
        await pilot.pause()

        assert got == []
        assert modal.warning == "Pick at least one method"
