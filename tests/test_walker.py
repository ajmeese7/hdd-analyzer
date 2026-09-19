from hdd_analyzer.walker import is_cycle


def test_is_cycle_false_for_unseen_identity():
    assert not is_cycle((1, 2), set())


def test_is_cycle_true_for_already_visited_identity():
    assert is_cycle((1, 2), {(1, 2)})


def test_is_cycle_false_when_identity_unknown():
    assert not is_cycle(None, {(1, 2)})


def test_is_cycle_distinguishes_by_device_and_inode():
    visited = {(1, 2)}
    assert not is_cycle((1, 3), visited)
    assert not is_cycle((2, 2), visited)
