from callglance.notifier import QualityNotifier


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


SETTINGS = {"notifications": True, "notify_recovery": True, "notify_after": 20,
            "notify_cooldown": 600}


def make(**over):
    clock = Clock()
    settings = {**SETTINGS, **over}
    return QualityNotifier(settings.get, clock), clock


def snap(level, headline="Your Wi-Fi is the problem"):
    return {"level": level, "headline": headline, "detail": "Calls may stutter.",
            "tips": ["Move closer to the router."]}


def feed(n, clock, level, seconds, step=2.0):
    notices = []
    for _ in range(int(seconds / step)):
        clock.now += step
        notice = n.update(snap(level))
        if notice:
            notices.append(notice)
    return notices


def test_short_blips_do_not_notify():
    n, clock = make()
    assert feed(n, clock, "poor", 10) == []
    assert feed(n, clock, "good", 60) == []


def test_sustained_problem_notifies_once_then_recovery():
    n, clock = make()
    notices = feed(n, clock, "fair", 120)
    assert len(notices) == 1
    assert notices[0].kind == "degraded" and notices[0].title == "Your Wi-Fi is the problem"
    assert "Move closer" in notices[0].body
    recovered = feed(n, clock, "good", 60)
    assert len(recovered) == 1 and recovered[0].kind == "recovered"


def test_escalation_notifies_again():
    n, clock = make()
    assert len(feed(n, clock, "fair", 40)) == 1
    escalated = feed(n, clock, "offline", 40)
    assert len(escalated) == 1 and escalated[0].urgency == 2


def test_cooldown_between_episodes():
    n, clock = make()
    assert len(feed(n, clock, "poor", 30)) == 1
    feed(n, clock, "good", 60)
    assert feed(n, clock, "poor", 60) == []  # within 10 minutes of the last one
    feed(n, clock, "good", 60)
    clock.now += 600
    assert len(feed(n, clock, "poor", 30)) == 1


def test_disabled():
    n, clock = make(notifications=False)
    assert feed(n, clock, "poor", 120) == []


def test_no_recovery_note_when_disabled():
    n, clock = make(notify_recovery=False)
    feed(n, clock, "poor", 30)
    assert feed(n, clock, "good", 60) == []
