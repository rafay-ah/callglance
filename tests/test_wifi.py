from callglance.wifi import (
    band_for,
    channel_for,
    parse_iw_link,
    read_proc_wireless,
    signal_quality,
)

PROC = """Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE
 face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22
wlp2s0: 0000   58.  -52.  -256        0      0      0      0     35        0
"""

IW = """Connected to aa:bb:cc:dd:ee:ff (on wlp2s0)
	SSID: Home Net
	freq: 5180.0
	RX: 1234 bytes (10 packets)
	signal: -61 dBm
	rx bitrate: 866.7 MBit/s VHT-MCS 9 80MHz short GI VHT-NSS 2
	tx bitrate: 1201.0 MBit/s 80MHz HE-MCS 11 HE-NSS 2 HE-GI 0 HE-DCM 0
"""


def test_bands_and_channels():
    assert band_for(2437) == "2.4 GHz" and channel_for(2437) == 6
    assert band_for(5180) == "5 GHz" and channel_for(5180) == 36
    assert band_for(5955) == "6 GHz" and channel_for(5955) == 1
    assert channel_for(2484) == 14 and band_for(None) is None


def test_signal_quality_uses_voice_grade_guidance():
    assert signal_quality(-55) == "excellent"
    assert signal_quality(-67) == "good"
    assert signal_quality(-72) == "fair"
    assert signal_quality(-80) == "poor"
    assert signal_quality(None, 90) == "excellent"
    assert signal_quality(None) == "unknown"


def test_proc_wireless(tmp_path):
    path = tmp_path / "wireless"
    path.write_text(PROC)
    assert read_proc_wireless("wlp2s0", str(path)) == -52
    assert read_proc_wireless("wlan9", str(path)) is None


def test_iw_link_parsing():
    info = parse_iw_link(IW)
    assert info == {"ssid": "Home Net", "frequency_mhz": 5180, "signal_dbm": -61,
                    "bitrate_mbps": 1201.0, "standard": "Wi-Fi 6"}
    assert parse_iw_link("Not connected.") == {}
