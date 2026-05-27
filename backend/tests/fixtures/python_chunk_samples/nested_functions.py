def outer():
    """Has a closure-returning inner function — inner should NOT be its own chunk."""

    def inner():
        return 1

    return inner


def another():
    if True:
        def deeply_nested():
            return 2
        return deeply_nested
    return None
