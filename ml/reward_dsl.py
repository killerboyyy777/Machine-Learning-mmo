"""Custom reward formulas for ML training (#68).

A small arithmetic language over named per-step signals, so the people who
know what an agent should actually want can write
``0.5*xp_delta + 1.2*profit - 0.1*deaths`` in a config field instead of
editing Python. Parsed once at construction and walked as a tree -- never
eval'd -- so a formula can only combine numbers with the operators and
functions listed here. It cannot import, assign, branch, or reach anything
outside this module.

Deliberately not Turing-complete: there is no assignment and no
conditional, because an expression a reviewer can read at a glance is the
point of the feature. A researcher who needs real Python (a learned
reward, a lookup table, a stateful counter) wants the plugin hook
``ml.plugins.AgentPlugin.reward`` instead, which is ordinary trusted code
at the same trust level as the plugin file itself.

Formula authors are usually not programmers, so every error names the
offending character position and lists what *was* available.
"""

import math
import re

SIGNALS = {
    "score_delta": "server score gained this step (a death penalty arrives here as a negative)",
    "xp_delta": "raw xp gained this step",
    "level_delta": "levels gained this step",
    "gold_delta": "gold earned minus gold spent this step",
    "inv_delta": "carried-value flow: selling loot is positive, buying is negative",
    "profit": "gold_delta + inv_delta, the net economic change",
    "deaths": "1.0 if the agent died this step, else 0.0",
    "deaths_total": "deaths so far this episode",
    "gold_lost": "gold permanently destroyed by deaths this step",
    "gold_dropped": "gold dropped on the floor (recoverable) this step",
    "xp_lost": "xp lost to deaths this step",
    "level": "current level",
    "hp_frac": "hp as a fraction of max hp",
    "party_size": "current party size (1 means solo)",
    "social": "the group-play shaping the 'score' mode would have paid this step",
    "formation": "the one-time party-formation bonus the 'score' mode would have paid",
    "novelty": "reserved for the novelty reward axis; always 0 today",
    "steps": "env steps taken this episode",
    "steps_since_death": "steps since the last death (0 on the death step itself)",
}


def _sign(x):
    return float(x > 0) - float(x < 0)


# name -> (min args, max args or None for unbounded, implementation)
FUNCTIONS = {
    "abs": (1, 1, abs),
    "sign": (1, 1, _sign),
    "floor": (1, 1, lambda x: float(math.floor(x))),
    "ceil": (1, 1, lambda x: float(math.ceil(x))),
    "min": (2, None, min),
    "max": (2, None, max),
    "clamp": (3, 3, lambda x, lo, hi: min(max(x, lo), hi)),
}

EXAMPLES = (
    "0.5*xp_delta + 1.2*profit - 0.1*deaths",
    "score_delta + 0.2*social",
    "profit - 2*gold_lost - 5*clamp(steps_since_death - 30, 0, 1)",
    "xp_delta + 5*level_delta",
)


def reference():
    """Signal and function vocabulary, for CLI help and error messages."""
    lines = ["signals:"]
    lines += [f"  {name:<17} {doc}" for name, doc in SIGNALS.items()]
    lines.append("functions:")
    lines += [f"  {name}(...)" for name in sorted(FUNCTIONS)]
    lines.append("operators: + - * / and parentheses")
    lines.append("examples:")
    lines += [f"  {ex}" for ex in EXAMPLES]
    return "\n".join(lines)


def _show(value):
    """Render a token for an error message: 3.0 reads worse than 3."""
    return f"{value:g}" if isinstance(value, float) else repr(value)


class RewardFormulaError(ValueError):
    """A formula that cannot be parsed, or that names something unknown.

    Subclasses ValueError so the existing config-validation call sites,
    which already catch ValueError and name the offending key, keep
    working unchanged.
    """

    def __init__(self, formula, position, message):
        self.formula = formula
        self.position = position
        if position is None:
            self.detail = message
        else:
            self.detail = f"{message} (at character {position + 1} of {formula!r})"
        super().__init__(f"reward formula {self.detail}")


_TOKEN_RE = re.compile(
    r"\s*(?:"
    r"(?P<num>(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)"
    r"|(?P<name>[A-Za-z_][A-Za-z_0-9]*)"
    r"|(?P<op>[+\-*/(),])"
    r")"
)


def _tokenize(text):
    tokens = []
    pos = 0
    while pos < len(text):
        match = _TOKEN_RE.match(text, pos)
        if match is None:
            rest = text[pos:]
            if not rest.strip():
                break
            # Report the character the author actually typed, not the
            # whitespace in front of it.
            offset = pos + (len(rest) - len(rest.lstrip()))
            raise RewardFormulaError(
                text, offset, f"cannot read {rest.lstrip()[0]!r} here"
            )
        kind = match.lastgroup
        raw = match.group(kind)
        tokens.append((kind, float(raw) if kind == "num" else raw, match.start(kind)))
        pos = match.end()
    return tokens


class _Parser:
    """Recursive descent over the token list.

    Grammar: expr := term (('+'|'-') term)*
              term := unary (('*'|'/') unary)*
              unary := ('-'|'+') unary | atom
              atom := number | signal | func '(' args ')' | '(' expr ')'

    Functions are resolved here rather than at eval time so a typo costs
    one error at construction instead of one per step for a whole run.
    """

    def __init__(self, tokens, text):
        self.tokens = tokens
        self.text = text
        self.index = 0
        self.used = set()

    def _peek(self):
        if self.index < len(self.tokens):
            return self.tokens[self.index]
        return (None, None, len(self.text))

    def _take(self):
        token = self._peek()
        self.index += 1
        return token

    def _expect_op(self, op):
        kind, value, position = self._take()
        if kind != "op" or value != op:
            raise RewardFormulaError(self.text, position, f"expected {op!r}")

    def parse(self):
        if not self.tokens:
            raise RewardFormulaError(self.text, 0, "formula is empty")
        node = self._expr()
        kind, value, position = self._peek()
        if kind is not None:
            raise RewardFormulaError(
                self.text,
                position,
                f"unexpected {_show(value)} after a complete formula",
            )
        return node

    def _expr(self):
        node = self._term()
        while True:
            kind, value, _pos = self._peek()
            if kind == "op" and value in ("+", "-"):
                self.index += 1
                node = ("bin", value, node, self._term())
                continue
            return node

    def _term(self):
        node = self._unary()
        while True:
            kind, value, _pos = self._peek()
            if kind == "op" and value in ("*", "/"):
                self.index += 1
                node = ("bin", value, node, self._unary())
                continue
            return node

    def _unary(self):
        kind, value, position = self._take()
        if kind == "op" and value in ("+", "-"):
            return ("neg", self._unary()) if value == "-" else ("pos", self._unary())
        if kind == "num":
            return ("num", value)
        if kind == "name":
            if self._peek()[1] == "(":
                return self._call(value, position)
            self.used.add(value)
            return ("var", value)
        if kind == "op" and value == "(":
            node = self._expr()
            self._expect_op(")")
            return node
        if kind is None:
            raise RewardFormulaError(self.text, position, "formula ends early")
        raise RewardFormulaError(self.text, position, f"unexpected {_show(value)}")

    def _call(self, name, position):
        spec = FUNCTIONS.get(name)
        if spec is None:
            raise RewardFormulaError(
                self.text,
                position,
                f"unknown function {name!r} (available: {', '.join(sorted(FUNCTIONS))})",
            )
        self._expect_op("(")
        args = [self._expr()]
        while self._peek()[1] == ",":
            self.index += 1
            args.append(self._expr())
        self._expect_op(")")
        low, high, _impl = spec
        count = len(args)
        if count < low or (high is not None and count > high):
            want = f"{low}" if low == high else f"{low} or more"
            if high is not None and high != low:
                want += f" (at most {high})"
            raise RewardFormulaError(
                self.text,
                position,
                f"{name}() takes {want} argument(s), got {count}",
            )
        return ("call", name, args)


def _evaluate(node, signals, text):
    kind = node[0]
    if kind == "num":
        return node[1]
    if kind == "var":
        name = node[1]
        if name not in signals:
            raise RewardFormulaError(
                text, None, f"signal {name!r} was not available this step"
            )
        try:
            return float(signals[name])
        except (TypeError, ValueError):
            raise RewardFormulaError(
                text, None, f"signal {name!r} held a non-numeric value"
            ) from None
    if kind == "neg":
        return -_evaluate(node[1], signals, text)
    if kind == "pos":
        return _evaluate(node[1], signals, text)
    if kind == "call":
        _low, _high, impl = FUNCTIONS[node[1]]
        return float(impl(*[_evaluate(a, signals, text) for a in node[2]]))
    op, left, right = node[1], node[2], node[3]
    lhs = _evaluate(left, signals, text)
    rhs = _evaluate(right, signals, text)
    if op == "+":
        return lhs + rhs
    if op == "-":
        return lhs - rhs
    if op == "*":
        return lhs * rhs
    return lhs / rhs


class RewardFormula:
    """A parsed formula, ready to evaluate once per step."""

    def __init__(self, text, tree, signals_used):
        self.text = text
        self.tree = tree
        self.signals_used = frozenset(signals_used)
        self.zero_divisions = 0

    def __call__(self, signals):
        return self.evaluate(signals)

    def evaluate(self, signals):
        try:
            return _evaluate(self.tree, signals, self.text)
        except ZeroDivisionError:
            # A non-coder will divide by a count that is legitimately zero
            # (an empty party, no deaths yet). Zeroing the formula and
            # counting that beats ending a training run, and the count
            # lets the caller see it happened rather than guessing.
            self.zero_divisions += 1
            return 0.0


def compile_formula(text):
    """Parse `text` into a RewardFormula, raising RewardFormulaError.

    Signal names are resolved here, so an unknown name fails at
    construction rather than on the first step of a long run.
    """
    if not isinstance(text, str):
        raise RewardFormulaError(str(text), None, "formula must be a string")
    stripped = text.strip()
    parser = _Parser(_tokenize(stripped), stripped)
    tree = parser.parse()
    unknown = sorted(n for n in parser.used if n not in SIGNALS)
    if unknown:
        near = _closest(unknown[0], list(SIGNALS))
        hint = f" -- did you mean {near!r}?" if near else ""
        raise RewardFormulaError(
            stripped,
            None,
            f"unknown signal {unknown[0]!r} (available: {', '.join(SIGNALS)}){hint}",
        )
    return RewardFormula(stripped, tree, parser.used)


def _closest(name, candidates):
    """Nearest candidate by common-prefix length: 'xp' should reach 'xp_delta'."""
    best, best_len = None, 0
    for candidate in candidates:
        shared = 0
        for a, b in zip(name, candidate):
            if a != b:
                break
            shared += 1
        if shared > best_len:
            best, best_len = candidate, shared
    return best if best_len >= 2 else None


def evaluate(formula, signals):
    """Evaluate a formula string (or a precompiled RewardFormula)."""
    if isinstance(formula, RewardFormula):
        return formula.evaluate(signals)
    return compile_formula(formula).evaluate(signals)
