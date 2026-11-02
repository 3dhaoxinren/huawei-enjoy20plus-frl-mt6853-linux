"""Pure terminal key encoding shared by the GUI and regression tests."""
ARROWS = {"←": "\x1b[D", "↓": "\x1b[B", "↑": "\x1b[A", "→": "\x1b[C",
          "Esc": "\x1b", "Tab": "\t", "⌫": "\x7f", "Enter": "\r", "space": " ", "Ctrl-C": "\x03"}
SHIFT_DIGITS = dict(zip("1234567890", "!@#$%^&*()"))


def key_bytes(key, shift=False, ctrl=False):
    if key in ARROWS:
        return ARROWS[key].encode()
    if len(key) != 1:
        return b""
    if ctrl and "@" <= key.upper() <= "_":
        return bytes([ord(key.upper()) & 31])
    if shift:
        key = SHIFT_DIGITS.get(key, key.upper())
    return key.encode("utf-8")
