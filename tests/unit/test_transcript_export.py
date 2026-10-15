"""Unit tests for transcript-to-PDF export."""

from uuid import uuid4

from app.models import TranscriptWord
from app.services.transcript_export import (
    LINE_BREAK_GAP_SECONDS,
    MAX_LINE_CHARS,
    TranscriptLine,
    TranscriptPdfRenderer,
    assemble_lines,
    format_timestamp,
    is_rtl_language,
    is_rtl_transcript,
)


def _word(index: int, text: str, start: float, end: float) -> TranscriptWord:
    """Build an unsaved transcript word row."""
    return TranscriptWord(
        id=uuid4(),
        video_id=uuid4(),
        word_index=index,
        word=text,
        start_time=start,
        end_time=end,
    )


class TestFormatTimestamp:
    """Formatting a position as a human-readable timestamp."""

    def test_under_an_hour_omits_hours(self) -> None:
        assert format_timestamp(72.4) == "01:12"

    def test_over_an_hour_includes_hours(self) -> None:
        assert format_timestamp(3725.9) == "01:02:05"

    def test_zero_and_negative_floor_to_zero(self) -> None:
        assert format_timestamp(0.0) == "00:00"
        assert format_timestamp(-5.0) == "00:00"


class TestAssembleLines:
    """Grouping word rows into timestamped lines."""

    def test_empty_transcript_yields_no_lines(self) -> None:
        assert assemble_lines([]) == []

    def test_single_word_is_one_line(self) -> None:
        lines = assemble_lines([_word(0, "hello", 3.0, 3.4)])
        assert lines == [TranscriptLine(seconds=3.0, text="hello")]

    def test_continuous_speech_stays_on_one_line(self) -> None:
        words = [
            _word(0, "buy", 12.0, 12.4),
            _word(1, "a", 12.4, 12.5),
            _word(2, "brain", 12.5, 13.0),
        ]
        lines = assemble_lines(words)
        assert len(lines) == 1
        assert lines[0].seconds == 12.0
        assert lines[0].text == "buy a brain"

    def test_pause_opens_a_new_line(self) -> None:
        words = [
            _word(0, "hello", 0.0, 0.5),
            _word(1, "world", 0.5 + LINE_BREAK_GAP_SECONDS, 3.0),
        ]
        lines = assemble_lines(words)
        assert [line.text for line in lines] == ["hello", "world"]
        assert lines[1].seconds == 0.5 + LINE_BREAK_GAP_SECONDS

    def test_sentence_end_opens_a_new_line_even_without_a_pause(self) -> None:
        words = [
            _word(0, "done.", 0.0, 0.5),
            _word(1, "Next", 0.5, 0.8),
            _word(2, "part", 0.8, 1.1),
        ]
        lines = assemble_lines(words)
        assert [line.text for line in lines] == ["done.", "Next part"]

    def test_arabic_sentence_end_opens_a_new_line(self) -> None:
        words = [
            _word(0, "شكرا؟", 0.0, 0.6),
            _word(1, "نعم", 0.6, 0.9),
        ]
        lines = assemble_lines(words)
        assert [line.text for line in lines] == ["شكرا؟", "نعم"]

    def test_long_unpunctuated_run_wraps(self) -> None:
        words = [_word(i, "word", i * 0.1, i * 0.1 + 0.1) for i in range(200)]
        lines = assemble_lines(words)
        assert len(lines) > 1
        assert all(len(line.text) < MAX_LINE_CHARS + len("word ") for line in lines)

    def test_lines_keep_transcript_order_and_opening_timestamp(self) -> None:
        words = [
            _word(0, "first", 1.0, 1.4),
            _word(1, "thought.", 1.4, 1.9),
            _word(2, "second", 5.0, 5.4),
        ]
        lines = assemble_lines(words)
        assert [(line.seconds, line.text) for line in lines] == [
            (1.0, "first thought."),
            (5.0, "second"),
        ]


class TestTranscriptPdfRenderer:
    """Rendering assembled lines as a PDF document."""

    def test_render_returns_a_pdf_document(self) -> None:
        document = TranscriptPdfRenderer().render(
            title="Test Video",
            lines=[TranscriptLine(seconds=12.0, text="hello world")],
        )
        assert document.startswith(b"%PDF")
        assert len(document) > 1000

    def test_render_embeds_the_bundled_font_for_arabic(self) -> None:
        document = TranscriptPdfRenderer().render(
            title="اختبار",
            lines=[TranscriptLine(seconds=0.0, text="مرحبا بكم في الفيديو")],
        )
        assert b"FontFile2" in document

    def test_render_with_no_lines_still_produces_a_document(self) -> None:
        document = TranscriptPdfRenderer().render(title="Empty", lines=[])
        assert document.startswith(b"%PDF")

    def test_arabic_is_laid_out_right_to_left(self) -> None:
        # The two documents differing is what proves the stated direction
        # reaches the shaping engine rather than being ignored.
        renderer = TranscriptPdfRenderer()
        lines = [TranscriptLine(seconds=1.0, text="مرحبا بكم")]
        arabic = renderer.render(title="t", lines=lines, is_rtl=True)
        latin = renderer.render(title="t", lines=lines, is_rtl=False)
        assert arabic != latin


class TestIsRtlTranscript:
    """Deciding the layout direction from the transcript's own words."""

    def test_arabic_words_over_a_latin_label_are_right_to_left(self) -> None:
        words = [_word(0, "مرحبا", 0.0, 0.5), _word(1, "بكم", 0.5, 1.0)]
        assert is_rtl_transcript(words, "en") is True

    def test_latin_words_over_an_arabic_label_are_left_to_right(self) -> None:
        words = [_word(0, "hello", 0.0, 0.5), _word(1, "world", 0.5, 1.0)]
        assert is_rtl_transcript(words, "ar") is False

    def test_majority_decides_in_a_mixed_transcript(self) -> None:
        words = [
            _word(0, "hello", 0.0, 0.4),
            _word(1, "مرحبا", 0.4, 0.8),
            _word(2, "بكم", 0.8, 1.2),
        ]
        assert is_rtl_transcript(words, "en") is True

    def test_words_without_letters_fall_back_to_the_declared_language(self) -> None:
        words = [_word(0, "123", 0.0, 0.5), _word(1, "؟", 0.5, 1.0)]
        assert is_rtl_transcript(words, "ar") is True
        assert is_rtl_transcript(words, "en") is False

    def test_empty_transcript_falls_back_to_the_declared_language(self) -> None:
        assert is_rtl_transcript([], "he") is True
        assert is_rtl_transcript([], None) is False


class TestIsRtlLanguage:
    """Deciding the layout direction from a video's language."""

    def test_arabic_is_right_to_left(self) -> None:
        assert is_rtl_language("ar") is True

    def test_regional_variants_and_case_are_recognised(self) -> None:
        assert is_rtl_language("ar-EG") is True
        assert is_rtl_language("AR") is True

    def test_latin_languages_are_not(self) -> None:
        assert is_rtl_language("en") is False
        assert is_rtl_language("pt-BR") is False

    def test_missing_language_defaults_to_left_to_right(self) -> None:
        assert is_rtl_language(None) is False
        assert is_rtl_language("") is False
