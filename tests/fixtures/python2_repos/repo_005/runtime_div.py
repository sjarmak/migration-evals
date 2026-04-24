# Variant: division + iterator semantics.


def average(xs):
    # Py2 floor division if len(xs) > sum(xs); py3 returns float.
    return sum(xs) / len(xs)


def keys_as_list(d):
    # Py2 dict.keys() is a list; py3 it's a view.
    return d.keys()
