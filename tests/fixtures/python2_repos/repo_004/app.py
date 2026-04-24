# Implicit ASCII coercion variant: bytes + str concat.


def join_payloads(prefix, body_bytes):
    # Python 2 silently coerces ASCII bytes to str. Python 3 raises TypeError.
    return prefix + body_bytes.decode("ascii")
