"""RTL8187Driver.connect() / close() glue, with every register-touching step mocked."""
from __future__ import annotations

from unittest.mock import MagicMock

import wifit3.chips.rtl8187.driver as drv
from wifit3.chips.rtl8187.constants import CMD_RX_ENABLE, CMD_TX_ENABLE
from wifit3.chips.rtl8187.mac import ChipVariant
from wifit3.chips.rtl8187.probe import ProbeResult
from wifit3.chips.rtl8187.rtl8225 import RfSetup, RfVariant, TxPower

_POWER = TxPower(hw_value=tuple([0x55] * 14), base=0x36)
_SETUP = RfSetup(asic_rev=1, variant=RfVariant.RTL8225Z2)


def _probe_result() -> ProbeResult:
    return ProbeResult(
        mac=bytes.fromhex("020000000001"),
        chip=ChipVariant(hwver_raw=0, name="RTL8187vB", is_rtl8187b=False, hw_rev=None),
        setup=_SETUP,
        power=_POWER,
        rfkill_off=True,
    )


def _mock_bring_up(monkeypatch, calls: list[tuple]) -> drv.RTL8187Driver:
    d = drv.RTL8187Driver(MagicMock())
    monkeypatch.setattr(d, "_claim", lambda: calls.append(("claim",)))
    monkeypatch.setattr(drv, "probe", lambda t: (calls.append(("probe",)), _probe_result())[1])
    monkeypatch.setattr(drv, "bulk_out_max_packet_size", lambda dev: 64)
    monkeypatch.setattr(drv, "is_chip_warm", lambda t: False)
    monkeypatch.setattr(drv, "build_rf_init", lambda t, setup, power: lambda _t: None)
    monkeypatch.setattr(drv, "cold_bring_up",
                        lambda t, rf_init: (calls.append(("cold_bring_up",)), 0x909CFC0B)[1])
    monkeypatch.setattr(drv, "_config_channel",
                        lambda t, asic_rev, variant, ch, power:
                        calls.append(("config_channel", asic_rev, variant, ch, power)))
    monkeypatch.setattr(d.transport, "read8", lambda addr: CMD_TX_ENABLE | CMD_RX_ENABLE)
    monkeypatch.setattr(drv, "RxReaderThread", MagicMock())
    return d


async def test_connect_tunes_channel_1_after_monitor_entry(monkeypatch):
    calls: list[tuple] = []
    d = _mock_bring_up(monkeypatch, calls)
    assert d.current_channel is None

    assert await d.connect() is True

    assert calls == [
        ("claim",),
        ("probe",),
        ("cold_bring_up",),
        ("config_channel", 1, RfVariant.RTL8225Z2, 1, _POWER),
    ]
    assert d.current_channel == 1
    assert d._bulk_out_max_packet_size == 64
    assert d._bulk_in_ep == 0x81                       # usb_rcvbulkpipe(.., 1), dev.c:435


async def test_close_runs_stop_before_rx_teardown_and_release(monkeypatch):
    calls: list[tuple] = []
    d = _mock_bring_up(monkeypatch, calls)
    await d.connect()
    calls.clear()

    monkeypatch.setattr(drv, "stop", lambda t, rf_stop: calls.append(("stop",)))

    async def rx_stop() -> None:
        calls.append(("rx_stop",))

    d._rx_reader = MagicMock(stop=rx_stop)
    monkeypatch.setattr(d, "_release", lambda: calls.append(("release",)))

    await d.close()
    assert calls == [("stop",), ("rx_stop",), ("release",)]


async def test_close_before_connect_skips_stop(monkeypatch):
    d = drv.RTL8187Driver(MagicMock())
    monkeypatch.setattr(drv, "stop", lambda t, rf_stop: (_ for _ in ()).throw(AssertionError))
    await d.close()


async def test_connect_refuses_8187B_silicon_before_init(monkeypatch):
    import dataclasses

    import pytest

    from wifit3.errors import BringUpError

    calls: list[tuple] = []
    d = _mock_bring_up(monkeypatch, calls)
    early_b = dataclasses.replace(
        _probe_result(),
        chip=ChipVariant(hwver_raw=6 << 25, name="RTL8187BvB(early)", is_rtl8187b=True, hw_rev=0))
    monkeypatch.setattr(drv, "probe", lambda t: early_b)

    with pytest.raises(BringUpError):
        await d.connect()
    assert ("cold_bring_up",) not in calls


async def test_connect_fails_when_radio_switch_reads_off(monkeypatch):
    import dataclasses

    import pytest

    from wifit3.errors import BringUpError

    calls: list[tuple] = []
    d = _mock_bring_up(monkeypatch, calls)
    monkeypatch.setattr(drv, "probe",
                        lambda t: dataclasses.replace(_probe_result(), rfkill_off=False))

    with pytest.raises(BringUpError, match="radio switch reads OFF") as exc:
        await d.connect()
    assert exc.value.stage == "rfkill"
    assert ("cold_bring_up",) not in calls
