"""Shared math notation normalization and speech-friendly rendering helpers."""

from __future__ import annotations

import re


_MATH_DELIMITED = re.compile(
    r"(\\\[.*?\\\]|\\\(.*?\\\)|\$\$.*?\$\$|"
    r"(?<!\$)\$(?!\s)(?=[^$\n]*(?:\\[A-Za-z]+|[=^_+*/]|\b[A-Za-z]\b))[^$\n]+?\$)", re.S
)
_GREEK = {
    "alpha": "alpha", "beta": "beta", "gamma": "gamma", "delta": "delta",
    "epsilon": "epsilon", "theta": "theta", "lambda": "lambda", "mu": "mu",
    "pi": "pi", "rho": "rho", "sigma": "sigma", "tau": "tau",
    "phi": "phi", "omega": "omega", "Delta": "capital delta", "Sigma": "capital sigma",
    "Omega": "capital omega",
}


def canonicalize_math(text: str) -> str:
    """Keep prose intact; standardize explicit math and wrap equation-like lines.

    Dollar delimiters are intentionally preserved when explicit. A currency
    amount in prose is never inferred as math.
    """
    if not text:
        return ""

    def normalize_span(match: re.Match[str]) -> str:
        raw = match.group(0)
        if raw.startswith("\\["):
            return "\\[" + _normalize_tex(raw[2:-2]) + "\\]"
        if raw.startswith("\\("):
            return "\\(" + _normalize_tex(raw[2:-2]) + "\\)"
        if raw.startswith("$$"):
            return "$$" + _normalize_tex(raw[2:-2]) + "$$"
        if raw.startswith("$"):
            return "\\(" + _normalize_tex(raw[1:-1]) + "\\)"
        return raw

    text = _MATH_DELIMITED.sub(normalize_span, text)
    lines = text.splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        if ":" in s:
            lead, tail = s.split(":", 1)
            if _looks_equation(tail.strip()):
                lines[i] = f"{lead}: \\({_normalize_tex(tail.strip())}\\)"
                continue
        if not (s.startswith(("\\[", "\\(", "$$")) and s.endswith(("\\]", "\\)", "$$"))) and _looks_equation(s):
            lines[i] = f"\\[{_normalize_tex(s)}\\]"
    return "\n".join(lines)


def _looks_equation(text: str) -> bool:
    if not text or len(text) >= 240 or ":" in text:
        return False
    if len(re.findall(r"\b[A-Za-z]{4,}\b", text)) > 2:
        return False
    return bool(re.search(r"[=≤≥<>≠≈∑∫]|\\(?:frac|sum|sqrt|alpha|beta)|\^[A-Za-z0-9{]|_[A-Za-z0-9{]", text))


def _normalize_tex(text: str) -> str:
    text = text.strip()
    # PDF/OCR convention: p1x1 -> p_{1}x_{1}; avoid rewriting words/prose.
    text = re.sub(r"(?<![A-Za-z\\])([A-Za-z])([0-9])(?=[A-Za-z0-9(=+−*/ }.,;]|$)", r"\1_{\2}", text)
    text = re.sub(r"(?<![A-Za-z\\])([A-Za-z])_([A-Za-z0-9])", r"\1_{\2}", text)
    text = re.sub(r"(?<![A-Za-z\\])([A-Za-z])\^([A-Za-z0-9])", r"\1^{\2}", text)
    text = text.replace("≤", r"\le ").replace("≥", r"\ge ").replace("≠", r"\ne ").replace("×", r"\times ")
    text = re.sub(r"(?<!\\)\bfrac\s*\{", r"\\frac{", text)
    return re.sub(r"[ \t]{2,}", " ", text)


def math_to_speech(text: str) -> str:
    """Convert explicit TeX math to spoken notation; leave ordinary prose alone."""
    if not text:
        return ""
    text = canonicalize_math(text)

    def speak(match: re.Match[str]) -> str:
        raw = match.group(0)
        if raw.startswith("\\["):
            body = raw[2:-2]
        elif raw.startswith("\\("):
            body = raw[2:-2]
        elif raw.startswith("$$"):
            body = raw[2:-2]
        else:
            body = raw[1:-1]
        return " " + _speak_tex(body) + " "

    text = _MATH_DELIMITED.sub(speak, text)
    # Do not pass raw TeX punctuation from malformed/unwrapped expressions.
    text = re.sub(r"\\([A-Za-z]+)", lambda m: _GREEK.get(m.group(1), m.group(1)), text)
    for delimiter in (r"\[", r"\]", r"\(", r"\)"):
        text = text.replace(delimiter, " ")
    return re.sub(r"[{}]", " ", text)


def _speak_tex(source: str) -> str:
    """Small recursive TeX reader for common course notation."""
    s = source.strip()
    greek = {k: v for k, v in _GREEK.items()}
    commands = {
        "le": "less than or equal to", "leq": "less than or equal to",
        "ge": "greater than or equal to", "geq": "greater than or equal to",
        "neq": "not equal to", "times": "times", "cdot": "times",
        "pm": "plus or minus", "to": "approaches", "approx": "approximately",
        "infty": "infinity", "sum": "the sum of", "int": "the integral of",
        "partial": "partial", "sqrt": "square root of", "log": "log",
        "ln": "natural log", "exp": "exponential",
        "lim": "the limit", "left": "", "right": "",
        "max": "maximum", "min": "minimum", "percent": "percent",
    }

    # Alignment environments carry layout instructions, not spoken content.
    s = re.sub(r"\\(?:begin|end)\{(?:aligned|align|gathered|cases)\}", " ", s)
    s = re.sub(r"\\\\\s*", "; next, ", s).replace("&", " ")

    def group(at: int) -> tuple[str, int]:
        while at < len(s) and s[at].isspace():
            at += 1
        if at >= len(s):
            return "", at
        if s[at] != "{":
            if s[at] == "\\":
                m = re.match(r"\\([A-Za-z]+)", s[at:])
                if m:
                    return m.group(0), at + len(m.group(0))
            return s[at], at + 1
        depth, end = 1, at + 1
        while end < len(s) and depth:
            depth += (s[end] == "{") - (s[end] == "}")
            end += 1
        return s[at + 1:end - 1], end

    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            if s[i:i + 2] == r"\%":
                out.append(" percent ")
                i += 2
                continue
            m = re.match(r"\\([A-Za-z]+)", s[i:])
            if not m:
                i += 1
                continue
            cmd, i = m.group(1), i + len(m.group(0))
            if cmd == "frac":
                numerator, i = group(i)
                denominator, i = group(i)
                out.append(f"{_speak_tex(numerator)} divided by {_speak_tex(denominator)} ")
            elif cmd in {"text", "mathrm", "mathbf"}:
                value, i = group(i)
                out.append(_speak_tex(value))
            elif cmd in {"sum", "int", "lim"}:
                lower = upper = ""
                if i < len(s) and s[i] == "_":
                    lower, i = group(i + 1)
                if i < len(s) and s[i] == "^":
                    upper, i = group(i + 1)
                if cmd == "lim":
                    spoken_lower = _speak_tex(lower)
                    if "approaches" in spoken_lower:
                        out.append(f"the limit as {spoken_lower} ")
                    elif lower and upper:
                        out.append(f"the limit as {spoken_lower} approaches {_speak_tex(upper)} ")
                    else:
                        out.append(f"the limit as {spoken_lower or _speak_tex(upper)} ")
                else:
                    phrase = "the sum" if cmd == "sum" else "the integral"
                    if lower or upper:
                        low, high = _speak_tex(lower), _speak_tex(upper)
                        phrase += f" from {_number_word(low)} to {_number_word(high)}"
                    out.append(phrase + " of ")
            else:
                spoken = greek.get(cmd, commands.get(cmd, cmd))
                if out and out[-1] and out[-1][-1].isalnum() and spoken:
                    out.append(" ")
                out.append(spoken + " ")
        elif c in "^_":
            mark = c
            value, i = group(i + 1)
            spoken = _speak_tex(value)
            if mark == "^":
                out.append({"2": " squared ", "3": " cubed "}.get(spoken, f" to the {spoken} "))
            else:
                out.append(f" sub {_number_word(spoken)} ")
        elif c in "{}$":
            i += 1
        elif c == "≤":
            out.append(" less than or equal to "); i += 1
        elif c == "≥":
            out.append(" greater than or equal to "); i += 1
        elif c == "=":
            out.append(" equals "); i += 1
        elif c == "+":
            out.append(" plus "); i += 1
        elif c in "−-":
            out.append(" minus "); i += 1
        elif c == "/":
            out.append(" divided by "); i += 1
        elif c == "*":
            out.append(" times "); i += 1
        elif c in "()[]":
            out.append(" "); i += 1
        elif c == ",":
            out.append(", "); i += 1
        elif c == ";":
            out.append("; "); i += 1
        else:
            out.append(c); i += 1
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _number_word(value: str) -> str:
    return {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
            "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine"}.get(value, value)
