"""Export a video's transcript as a PDF document.

An export is a document, not a search result: the whole transcript, in order,
one timestamped line at a time, so a viewer can read it away from the player.
The transcript is stored word by word, so lines are re-grouped here - there is
no stored notion of a sentence or a paragraph to rely on.

The font is bundled rather than taken from the system: the deploy target ships
no fonts of its own, and Arabic needs a face that covers it. Amiri is used
because it carries both scripts, so a mixed Latin/Arabic transcript renders in
one pass without switching fonts mid-line.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from fpdf import FPDF

from app.models import TranscriptWord

#: A pause at least this long between two words opens a new line. Transcribed
#: speech runs on within a sentence, so the gap is the break a speaker made.
LINE_BREAK_GAP_SECONDS = 1.5

#: Sentence-final marks, Latin and Arabic. A line also ends here: a wall of text
#: is not readable just because the speaker never paused to breathe.
SENTENCE_ENDS = (".", "!", "?", "…", "؟", "۔")

#: Longest line, in characters, before a wrap. Bounds the rendered width so a
#: long unpunctuated stretch still breaks instead of running off the page.
MAX_LINE_CHARS = 200

#: Languages written right to left. An Arabic transcript is laid out RTL so the
#: timestamp sits where reading starts and the words keep their natural order.
RTL_LANGUAGES = frozenset({"ar", "he", "fa", "ur", "ps", "sd", "ug", "yi", "dv"})

#: The Arabic Unicode block. A word whose first letter falls inside it is read
#: right to left, which is what the transcript's own text is judged by.
_ARABIC_BLOCK_START = "\u0600"
_ARABIC_BLOCK_END = "\u06ff"

_FONT_PATH = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "Amiri-Regular.ttf"
_FONT_NAME = "amiri"
_TITLE_SIZE = 14
_BODY_SIZE = 11
_LINE_HEIGHT = 6


def is_rtl_language(language: str | None) -> bool:
    """Whether a transcript in ``language`` is written right to left."""
    primary = (language or "").strip().lower().replace("_", "-").split("-")[0]
    return primary in RTL_LANGUAGES


def _is_rtl_word(word: str) -> bool | None:
    """Whether ``word`` reads right to left, judged by its first letter.

    ``None`` for words carrying no letter at all, such as digits or
    punctuation, which say nothing about the direction of the text.
    """
    for character in word:
        if not character.isalpha():
            continue
        return _ARABIC_BLOCK_START <= character <= _ARABIC_BLOCK_END
    return None


def is_rtl_transcript(words: Sequence[TranscriptWord], declared_language: str | None) -> bool:
    """Whether a transcript is laid out right to left.

    The words decide whenever they show a clear direction: videos arrive
    mislabeled, and a declared ``en`` over Arabic speech would otherwise put
    every timestamp at the end of its line. The declared language is the
    fallback for transcripts whose words carry no script at all.
    """
    rtl = 0
    ltr = 0
    for word in words:
        direction = _is_rtl_word(word.word)
        if direction is None:
            continue
        if direction:
            rtl += 1
        else:
            ltr += 1
    if rtl == ltr:
        return is_rtl_language(declared_language)
    return rtl > ltr


@dataclass(frozen=True)
class TranscriptLine:
    """One timestamped line of a transcript."""

    seconds: float
    text: str


def format_timestamp(seconds: float) -> str:
    """Format a position as HH:MM:SS, dropping the hour when there is none."""
    total = max(int(seconds), 0)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _breaks_before(previous: TranscriptWord, current: TranscriptWord, text: str) -> bool:
    """Whether ``current`` opens a new line instead of extending the current one."""
    if current.start_time - previous.end_time >= LINE_BREAK_GAP_SECONDS:
        return True
    if previous.word.endswith(SENTENCE_ENDS):
        return True
    return len(text) >= MAX_LINE_CHARS


def assemble_lines(words: Sequence[TranscriptWord]) -> list[TranscriptLine]:
    """Group transcript words into timestamped lines, in transcript order.

    Each line keeps the position of the word that opened it, so the timestamp
    points at the start of the thought rather than at the last word in it.
    """
    lines: list[TranscriptLine] = []
    previous: TranscriptWord | None = None
    for word in words:
        if previous is None or _breaks_before(previous, word, lines[-1].text):
            lines.append(TranscriptLine(seconds=word.start_time, text=word.word))
        else:
            open_line = lines[-1]
            lines[-1] = TranscriptLine(
                seconds=open_line.seconds, text=f"{open_line.text} {word.word}"
            )
        previous = word
    return lines


class TranscriptPdfRenderer:
    """Render timestamped transcript lines as a PDF document."""

    def render(
        self,
        *,
        title: str,
        lines: Sequence[TranscriptLine],
        is_rtl: bool = False,
    ) -> bytes:
        """Return the finished document as bytes."""
        document = FPDF()
        document.set_auto_page_break(auto=True, margin=15)
        document.add_page()
        document.add_font(_FONT_NAME, "", str(_FONT_PATH))
        # Shaping renders Arabic as joined script rather than isolated
        # letters; the direction sets the line's base direction, which is
        # what places the leading timestamp on the side reading starts from.
        document.set_text_shaping(True, direction="rtl" if is_rtl else "ltr")
        self._write_title(document, title)
        document.set_font(_FONT_NAME, size=_BODY_SIZE)
        for line in lines:
            self._write_line(document, line)
        return bytes(document.output())

    @staticmethod
    def _write_title(document: FPDF, title: str) -> None:
        document.set_font(_FONT_NAME, size=_TITLE_SIZE)
        document.multi_cell(0, _LINE_HEIGHT * 2, title, new_x="LMARGIN", new_y="NEXT")

    @staticmethod
    def _write_line(document: FPDF, line: TranscriptLine) -> None:
        text = f"{format_timestamp(line.seconds)}  {line.text}"
        document.multi_cell(0, _LINE_HEIGHT, text, new_x="LMARGIN", new_y="NEXT")
