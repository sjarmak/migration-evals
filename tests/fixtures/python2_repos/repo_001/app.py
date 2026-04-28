# Python 2 idiom: implicit ASCII coercion + str-is-bytes.
# Under Python 3, "foo".encode() returns bytes (was already bytes-ish in py2).


def emit_payload():
    payload = "foo".encode()  # noqa: UP012 — fixture exemplifies py2 str-is-bytes idiom
    # Python 2 happily concatenates a str (bytes) with another str.
    # Python 3 forbids bytes + str without explicit decode/encode.
    return payload + b"-bar"


def main():
    return emit_payload()
