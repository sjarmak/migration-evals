# Python 2 str-is-bytes; relies on implicit ASCII coerce.

def emit_payload():
    payload = "foo".encode()
    return payload + b"-bar"

def join_with(label):
    # Py2: silent ASCII coerce; py3: TypeError without explicit decode.
    return label + emit_payload().decode("ascii")
