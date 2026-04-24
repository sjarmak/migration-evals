# Python 2 returns int (2); Python 3 returns float (2.5).
# 2to3 catches some cases but semantic equivalence requires runtime check.


def half(n):
    # Without `from __future__ import division`, py2 returns floor.
    return n / 2


def items_then_consume(d):
    # Python 2: dict.items() returns a list. Python 3: returns a view.
    pairs = d.items()
    # Code that mutates `d` while iterating `pairs` behaves differently.
    return list(pairs)


def doubled(xs):
    # Python 2: map() returns a list. Python 3: returns an iterator.
    return map(lambda x: x * 2, xs)
