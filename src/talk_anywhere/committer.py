"""Turn growing speech hypotheses into keystrokes.

Bark 3.0 Pro restates the whole utterance on every pass. The focused text
field only needs the characters that changed. The last two words stay
revisable, because the model often corrects the word still being spoken.
Earlier words stay put so the cursor does not jump around.
"""

from __future__ import annotations

Edit = tuple[str, str | int]


def normalize(text: str) -> str:
    return " ".join(text.replace("\n", " ").split())


def stable_prefix(text: str, revisable_words: int) -> str:
    words = text.split(" ")
    if len(words) <= revisable_words:
        return ""
    return " ".join(words[:-revisable_words]) + " "


def diff(current: str, target: str) -> list[Edit]:
    limit = min(len(current), len(target))
    index = 0
    while index < limit and current[index] == target[index]:
        index += 1
    edits: list[Edit] = []
    back = len(current) - index
    if back:
        edits.append(("backspace", back))
    typed = target[index:]
    if typed:
        edits.append(("text", typed))
    return edits


class DictationCommitter:
    def __init__(self, revisable_words: int = 2) -> None:
        self.revisable_words = revisable_words
        self.committed = ""
        self.pending = ""

    def reset(self) -> None:
        self.committed = ""
        self.pending = ""

    @property
    def typed(self) -> str:
        return self.committed + self.pending

    def update(self, hypothesis: str, *, final: bool) -> list[Edit]:
        text = normalize(hypothesis)
        current = self.typed
        if final:
            edits = diff(current, text)
            if text:
                edits.append(("text", " "))
            self.reset()
            return edits
        if text == current:
            return []
        if self.committed and not text.startswith(self.committed):
            # A partial rewrote words we already froze. Wait for the next
            # pass, or for the pause that produces the final transcript.
            return []
        stable = stable_prefix(text, self.revisable_words)
        if not stable.startswith(self.committed):
            new_committed = self.committed
        else:
            new_committed = stable
        if not text.startswith(new_committed):
            return []
        new_pending = text[len(new_committed) :]
        edits = diff(current, new_committed + new_pending)
        self.committed = new_committed
        self.pending = new_pending
        return edits


def selftest() -> None:
    committer = DictationCommitter()
    edits = committer.update("hello", final=False)
    assert edits == [("text", "hello")], edits
    assert committer.typed == "hello"

    edits = committer.update("hello there", final=False)
    assert edits == [("text", " there")], edits

    edits = committer.update("hello there friend", final=False)
    assert edits == [("text", " friend")], edits
    assert committer.committed == "hello "
    assert committer.pending == "there friend"

    edits = committer.update("hello there friends", final=False)
    assert edits == [("text", "s")], edits

    # Frozen prefix must not be rewritten by a partial.
    before = (committer.committed, committer.pending)
    edits = committer.update("hallo there friends", final=False)
    assert edits == [], edits
    assert (committer.committed, committer.pending) == before

    edits = committer.update("hallo there friends.", final=True)
    flat = _apply("hello there friends", edits)
    assert flat == "hallo there friends. ", flat
    assert committer.typed == ""

    committer = DictationCommitter()
    committer.update("um", final=False)
    edits = committer.update("", final=True)
    assert edits == [("backspace", 2)], edits
    assert committer.typed == ""

    committer = DictationCommitter()
    committer.update("I went to", final=False)
    edits = committer.update("I want to", final=False)
    assert _apply("I went to", edits) == "I want to"

    print("committer selftest ok")


def _apply(start: str, edits: list[Edit]) -> str:
    text = start
    for kind, value in edits:
        if kind == "backspace":
            count = int(value)
            text = text[:-count] if count < len(text) else ""
        else:
            text += str(value)
    return text


if __name__ == "__main__":
    selftest()
