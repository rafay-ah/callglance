from callglance.netinfo import (
    default_route,
    dns_servers,
    iface_kind,
    is_cgnat,
    is_private,
    pick_isp_hop,
)

ROUTE = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
wlp2s0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0
enp3s0\t00000000\t0100A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0
wlp2s0\t0001A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\t0\t0\t0
"""


def test_default_route_picks_lowest_metric(tmp_path):
    path = tmp_path / "route"
    path.write_text(ROUTE)
    route = default_route(str(path))
    assert route.iface == "enp3s0" and route.gateway == "192.168.0.1" and route.metric == 100


def test_no_default_route(tmp_path):
    path = tmp_path / "route"
    path.write_text(ROUTE.splitlines()[0] + "\n")
    assert default_route(str(path)) is None
    assert default_route(str(tmp_path / "missing")) is None


def test_dns_servers_skip_the_local_stub(tmp_path):
    stub = tmp_path / "stub.conf"
    stub.write_text("nameserver 127.0.0.53\noptions edns0\n")
    upstream = tmp_path / "upstream.conf"
    upstream.write_text("# real servers\nnameserver 192.168.1.1\nnameserver fe80::1%wlp2s0\n")
    assert dns_servers((str(stub), str(upstream))) == ["192.168.1.1"]
    assert dns_servers((str(stub),)) == []


def test_iface_kind(tmp_path):
    (tmp_path / "wlan0" / "wireless").mkdir(parents=True)
    (tmp_path / "eth0" / "device").mkdir(parents=True)
    (tmp_path / "eth0" / "type").write_text("1\n")
    (tmp_path / "tun0").mkdir()
    (tmp_path / "tun0" / "tun_flags").write_text("0x1001\n")
    assert iface_kind("wlan0", str(tmp_path)) == "wifi"
    assert iface_kind("eth0", str(tmp_path)) == "ethernet"
    assert iface_kind("tun0", str(tmp_path)) == "vpn"
    assert iface_kind("wg0", str(tmp_path)) == "vpn"


def test_private_and_cgnat():
    assert is_private("192.168.1.1") and is_private("10.0.0.1") and is_private("172.16.5.4")
    assert not is_private("100.64.0.1") and is_cgnat("100.64.0.1")
    assert not is_private("203.0.113.1") and not is_private(None) and not is_private("x")


def test_isp_hop_skips_a_second_home_router():
    hops = [(1, "192.168.1.1"), (2, "192.168.0.1"), (3, "100.72.0.1"), (4, "203.0.113.9")]
    assert pick_isp_hop(hops, "192.168.1.1") == (3, "100.72.0.1")


def test_isp_hop_tolerates_silent_hops_and_private_isp_cores():
    hops = [(1, "192.168.1.1"), (2, None), (3, "10.20.0.1")]
    assert pick_isp_hop(hops, "192.168.1.1") == (3, "10.20.0.1")
    assert pick_isp_hop([(1, "192.168.1.1")], "192.168.1.1") is None
