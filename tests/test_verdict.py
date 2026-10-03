import pytest

from callglance.stats import Summary
from callglance.verdict import (
    FAIR,
    GOOD,
    OFFLINE,
    POOR,
    UNKNOWN,
    Inputs,
    Thresholds,
    Verdict,
    VerdictTracker,
    assess,
    metric_level,
)

TH = Thresholds()


def s(latency=10.0, jitter=1.0, loss=0.0, sent=150, min_ms=None):
    received = round(sent * (1 - loss / 100))
    return Summary(sent=sent, received=received, latency_ms=latency, jitter_ms=jitter,
                   loss_pct=loss, min_ms=latency if min_ms is None else min_ms,
                   max_ms=latency + jitter * 3, last_ms=latency)


def inputs(**kw):
    base = dict(internet=s(20), internet_recent=s(20, sent=15), router=s(2),
                router_recent=s(2, sent=15), isp=s(9), isp_recent=s(9, sent=15), link_kind="wifi",
                wifi={"signal_dbm": -55, "quality": "excellent", "band": "5 GHz"})
    base.update(kw)
    return Inputs(**base)


def test_metric_levels():
    assert metric_level(None, 1, 2) == UNKNOWN
    assert metric_level(0.5, 1, 2) == GOOD
    assert metric_level(1, 1, 2) == FAIR
    assert metric_level(5, 1, 2) == POOR


def test_good_connection():
    v = assess(inputs(), TH)
    assert v.level == GOOD and v.headline == "Good for calls" and v.culprit is None


def test_measuring_until_enough_samples():
    v = assess(inputs(internet=s(20, sent=2)), TH)
    assert v.level == UNKNOWN


def test_no_route_is_offline():
    v = assess(inputs(has_route=False), TH)
    assert v.level == OFFLINE and "offline" in v.headline


def test_wifi_jitter_is_blamed_on_wifi():
    v = assess(inputs(internet=s(40, jitter=45), router=s(25, jitter=38), isp=s(30, jitter=40)), TH)
    assert v.level == FAIR
    assert v.culprit == "wifi" and v.origin == "router"
    assert v.headline == "Your Wi-Fi is the problem"


def test_wired_router_problem_says_home_network():
    v = assess(inputs(link_kind="ethernet", wifi=None, internet=s(20, loss=6),
                      router=s(2, loss=5)), TH)
    assert v.culprit == "local" and v.headline == "Your home network is the problem"


def test_loss_past_a_clean_router_is_the_isp():
    v = assess(inputs(internet=s(20, loss=6), router=s(2, loss=0), isp=s(9, loss=5)), TH)
    assert v.level == POOR and v.culprit == "isp" and v.origin == "isp"
    assert "Your Wi-Fi is fine" in v.detail


def test_problem_beyond_the_first_isp_router():
    v = assess(inputs(internet=s(20, jitter=60), router=s(2), isp=s(9, jitter=1)), TH)
    assert v.culprit == "isp" and v.origin == "internet"


def test_router_ping_loss_alone_is_ignored():
    # Routers rate-limit pings aimed at themselves: loss at the router that does
    # not show up further along is not a problem for calls.
    v = assess(inputs(router=s(2, loss=20), internet=s(20, loss=0)), TH)
    assert v.level == GOOD


def test_few_router_samples_with_router_jitter_still_point_at_wifi():
    # Loss statistics are inconclusive with 15 router probes, but the router hop
    # is clearly jittery: that settles it.
    v = assess(inputs(internet=s(30, jitter=12, loss=6), router=s(25, jitter=14, sent=15),
                      isp=s(30, jitter=12, sent=5)), TH)
    assert v.culprit == "wifi"


def test_many_clean_router_samples_rule_the_router_out():
    v = assess(inputs(internet=s(20, loss=4, sent=300), router=s(2, loss=0, sent=150),
                      isp=None), TH)
    assert v.culprit == "isp" and v.origin == "internet"


def test_outage_past_the_router():
    v = assess(inputs(internet_recent=Summary(sent=6, received=0, loss_pct=100.0),
                      isp_recent=Summary(sent=3, received=0, loss_pct=100.0)), TH)
    assert v.level == OFFLINE and v.culprit == "isp" and v.origin == "isp"


def test_outage_beyond_the_isp_router():
    v = assess(inputs(internet_recent=Summary(sent=6, received=0, loss_pct=100.0)), TH)
    assert v.level == OFFLINE and v.origin == "internet"
    assert "first router answer" in v.detail


def test_cannot_reach_router():
    dead = Summary(sent=6, received=0, loss_pct=100.0)
    v = assess(inputs(internet_recent=dead, router_recent=dead), TH)
    assert v.level == OFFLINE and v.culprit == "wifi" and "router" in v.detail


def test_unmeasurable_router_is_not_blamed():
    dead = Summary(sent=6, received=0, loss_pct=100.0)
    v = assess(inputs(internet_recent=dead, router_recent=dead, router_measurable=False), TH)
    assert v.culprit is None and v.headline == "No internet connection"


def test_weak_signal_tip_when_wifi_is_the_problem():
    v = assess(inputs(internet=s(40, jitter=45), router=s(25, jitter=38),
                      wifi={"signal_dbm": -78, "quality": "poor", "band": "2.4 GHz"}), TH)
    assert any("weak" in t for t in v.tips)
    assert any("2.4 GHz" in t for t in v.tips)


def test_high_latency_suggests_speed_test():
    v = assess(inputs(internet=s(320), router=s(2), isp=s(300)), TH)
    assert v.level == POOR and v.cause == "latency" and v.culprit == "isp"
    assert any("speed test" in t for t in v.tips)


@pytest.mark.parametrize("data,expected", [({"jitter_fair": 10}, 10.0), ({"bogus": 1}, 30.0),
                                           ({"jitter_fair": "x"}, 30.0)])
def test_thresholds_from_config(data, expected):
    assert Thresholds.from_dict(data).jitter_fair == expected


def test_tracker_gets_worse_at_once_and_better_slowly():
    t = VerdictTracker(settle=3)
    good = Verdict(GOOD, "Good for calls", "")
    bad = Verdict(POOR, "Your ISP is the problem", "")
    assert t.update(good).level == GOOD
    assert t.update(bad).level == POOR
    assert t.update(good).level == POOR
    assert t.update(good).level == POOR
    assert t.update(good).level == GOOD
