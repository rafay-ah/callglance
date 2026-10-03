from callglance.stats import Sample, SampleWindow, summarize


def burst(train, rtts, t0=0.0):
    return [Sample(t=t0 + i * 0.02, train=train, idx=i, rtt=r, done=True)
            for i, r in enumerate(rtts)]


def test_empty_summary():
    s = summarize([], 5)
    assert s.sent == 0 and s.received == 0 and s.loss_pct is None and not s.measurable


def test_warm_up_probe_is_ignored_for_latency_and_jitter_but_counts_for_loss():
    # The first probe pays for Wi-Fi power saving (80 ms); the rest are steady.
    samples = burst(1, [80.0, 10.0, 10.0, 10.0, 10.0]) + burst(2, [75.0, 10.0, 10.0, 10.0, None], 2)
    s = summarize(samples, 5)
    assert s.sent == 10 and s.received == 9
    assert s.loss_pct == 10.0
    assert s.latency_ms == 10.0
    assert s.jitter_ms == 0.0


def test_jitter_is_mean_absolute_difference_within_bursts():
    s = summarize(burst(1, [0.0, 10.0, 20.0, 10.0, 30.0]), 5)
    # pairs (excluding warm-up): |20-10|, |10-20|, |30-10| -> mean 13.33
    assert round(s.jitter_ms, 2) == 13.33


def test_jitter_skips_pairs_broken_by_a_lost_probe():
    s = summarize(burst(1, [0.0, 10.0, None, 50.0, 52.0]), 5)
    assert s.jitter_ms == 2.0  # only (50, 52) are consecutive


def test_single_probe_bursts_use_consecutive_probes():
    samples = [Sample(t=i * 2.0, train=i, idx=0, rtt=r, done=True)
               for i, r in enumerate([10.0, 14.0, 10.0])]
    s = summarize(samples, 1)
    assert s.jitter_ms == 4.0
    assert s.latency_ms == 10.0


def test_in_flight_probes_are_not_counted():
    samples = burst(1, [10.0, 10.0, 10.0])
    samples.append(Sample(t=1.0, train=2, idx=0))  # not done yet
    assert summarize(samples, 3).sent == 3


def test_window_prunes_old_samples():
    w = SampleWindow(keep=10)
    for i in range(20):
        w.add(Sample(t=float(i), train=i, idx=0, rtt=1.0, done=True))
    w.prune(now=19.0)
    assert min(s.t for s in w.samples) >= 9.0
    assert len(w.since(15.0)) == 5
