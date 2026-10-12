"""Write deck/slides.md, the deck with no speaker notes, from deck/slides.private.md, the deck with them.

    python3 deck/strip_notes.py

slides.private.md is the file that is edited and presented from. Git does not track it. slides.md is the copy that
is committed and shared: the same slides, with every speaker note taken out. A speaker note is an HTML comment. A
comment that sets something for the slide, such as <!-- _class: dense -->, is kept.
"""

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
SETTING = re.compile(r"<!--\s*_?[A-Za-z]+\s*:[^\n]*-->")  # one line, a name and a value


def without_notes(text: str) -> str:
    kept = re.sub(r"<!--.*?-->", lambda found: found[0] if SETTING.fullmatch(found[0]) else "", text, flags=re.S)
    return re.sub(r"\n{3,}", "\n\n", kept)


if __name__ == "__main__":
    private = (HERE / "slides.private.md").read_text()
    public = without_notes(private)
    (HERE / "slides.md").write_text(public)
    print(f"slides.md written: {public.count(chr(10) + '---' + chr(10))} slides, "
          f"{len(re.findall(r'<!--', private)) - len(re.findall(r'<!--', public))} notes taken out")
