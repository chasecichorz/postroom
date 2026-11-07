"""IMAP modified UTF-7 (RFC 3501 section 5.1.3) for folder names."""
import base64


def decode(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "&":
            j = s.find("-", i)
            if j == -1:
                out.append(s[i:])
                break
            if j == i + 1:
                out.append("&")
            else:
                b64 = s[i + 1:j].replace(",", "/")
                b64 += "=" * (-len(b64) % 4)
                try:
                    out.append(base64.b64decode(b64).decode("utf-16-be"))
                except (ValueError, UnicodeDecodeError):
                    out.append(s[i:j + 1])
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def encode(s: str) -> str:
    out, buf = [], []

    def flush():
        if buf:
            b = base64.b64encode("".join(buf).encode("utf-16-be")).decode()
            out.append("&" + b.rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for c in s:
        if 0x20 <= ord(c) <= 0x7E:
            flush()
            out.append("&-" if c == "&" else c)
        else:
            buf.append(c)
    flush()
    return "".join(out)
