from netutil.retry import backoff_delays


def test_sequence():
    assert backoff_delays(0.5, 2, 4, 10) == [0.5, 1.0, 2.0, 4.0]


def test_cap():
    assert backoff_delays(0.5, 2, 5, 3) == [0.5, 1.0, 2.0, 3.0, 3.0]


def test_zero_retries():
    assert backoff_delays(1, 2, 0, 5) == []
