# Py2 vs py3 semantic shifts: division, map(), dict.items().

def half(n):
    # Without `from __future__ import division`, py2 returns floor.
    return n / 2

def items_then_consume(d):
    # Py2: dict.items() returns a list; py3 returns a view.
    pairs = d.items()
    return list(pairs)

def doubled(xs):
    # Py2: map() returns a list; py3 returns an iterator.
    return map(lambda x: x * 2, xs)
