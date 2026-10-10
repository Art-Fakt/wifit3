"""The decloak modal's candidate expansion and the method flags it hands the campaign."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from wifit3.campaigns.decloak import DecloakCampaign, DecloakInput
from wifit3.models import AccessPoint
from wifit3.ui.screens.focus_v2.decloak_modal import (
    SSID_TOKEN,
    decloak_defaults,
    expand_candidates,
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
