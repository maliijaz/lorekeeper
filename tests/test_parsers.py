"""Front-matter detection: tables of contents (spoilers) and publisher pages (junk codex entries)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.parsers import Chapter, chapterize, Block, is_front_matter, is_toc  # noqa: E402


def test_toc_without_heading_markup():
    # Red Rising style: "Contents" is an ordinary paragraph, entries have no "Chapter" word.
    ch = Chapter("Section 2", ["Contents", "Cover", "Title Page", "Prologue", "Part I: Slave", "1: Helldiver", "2: The Township"])
    assert is_toc(ch)


def test_copyright_page_is_front_matter():
    ch = Chapter("Section 1", ["Red Rising is a work of fiction. Names, places, characters are fictitious.",
                               "Copyright © 2014 by Pierce Brown. All rights reserved.", "ISBN 978-0-345-53980-3"])
    assert is_front_matter(ch)


def test_story_chapters_are_kept():
    story = Chapter("Chapter I The Cyclone", ["Dorothy lived in the midst of the great Kansas prairies.",
                                              "Toto played all day long, and Dorothy played with him."])
    assert not is_toc(story) and not is_front_matter(story)
    # A story that merely mentions copyright once is not front matter.
    once = Chapter("7", ["The scribe claimed copyright 1402 over the royal annals, and nobody laughed."])
    assert not is_front_matter(once)


def test_chapterize_drops_front_matter_and_toc():
    blocks = [Block("break"), Block("p", "Copyright © 2020 Someone. All rights reserved. ISBN 123"),
              Block("break"), Block("p", "Contents"), Block("p", "1: Beginning"), Block("p", "2: The End"),
              Block("break"), Block("h", "1"), Block("p", "Beginning"), Block("p", "Once upon a time there was a map.")]
    titles = [c.title for c in chapterize(blocks)]
    assert titles == ["1"]
