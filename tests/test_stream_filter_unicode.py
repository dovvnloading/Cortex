"""Unicode reply text must survive streaming independently of chunk boundaries."""

from __future__ import annotations

import pytest

from cortex_backend.services.stream_filter import EnvelopeStreamFilter


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("İstanbul.<memo>example</memo> Done.", "İstanbul. Done."),
        ("Before.<memo>İİ</memo> After.", "Before. After."),
        ("İstanbul.<memory_command>{}</memory_command> İzmir.", "İstanbul. İzmir."),
        ("Before.<code_executİon_request>{}</code_execution_request> After.", "Before. After."),
        ("Before.<code_execution_request>{}</code_executıon_requeſt> After.", "Before. After."),
        ("Before.<CODE_EXECUTION_REQUEſT>{}</CODE_EXECUTION_REQUEST> After.", "Before. After."),
        ("<thınK>example</thİnk>Answer.", "Answer."),
        ("<think>İİ</think>Answer.", "Answer."),
        ("İstanbul. <clear_memory/> İzmir.", "İstanbul.  İzmir."),
        ("Before.<code_executİon_request>{", "Before."),
        ("İstanbul and İzmir, ſ, ı, K and List<İtem>.", "İstanbul and İzmir, ſ, ı, K and List<İtem>."),
        ("Before. <code_executİon", "Before. <code_executİon"),
        ("Use the <thınK> tag.", "Use the <thınK> tag."),
    ],
)
def test_unicode_envelopes_at_every_chunk_boundary(reply: str, expected: str) -> None:
    """Check intermediate emissions as well as the text after close()."""
    chunkings = [[reply], list(reply)]
    chunkings.extend([reply[:split], reply[split:]] for split in range(1, len(reply)))
    for chunks in chunkings:
        seen: list[str] = []
        stream = EnvelopeStreamFilter(seen.append)
        for chunk in chunks:
            stream.feed(chunk)
            assert expected.startswith("".join(seen)), chunks
        stream.close()
        assert "".join(seen) == expected, chunks
