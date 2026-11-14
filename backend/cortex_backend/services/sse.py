"""Read data fields from a server-sent event stream."""

from collections.abc import Iterable, Iterator


def sse_payloads(lines: Iterable[str]) -> Iterator[str]:
    """Yield complete SSE data fields (multiple data: lines make one event).

    The HTTP response iterator supplies lines, not events. Parsing each line
    independently can discard a valid multiline JSON delta and still report a
    completed answer. Keepalives and metadata fields are not message content.
    """
    data_lines: list[str] = []
    for line in lines:
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
                data_lines = []
            continue
        if line.startswith("data:"):
            value = line[len("data:"):]
            data_lines.append(value[1:] if value.startswith(" ") else value)
    # Be permissive of EOF directly after the last data line. The caller still
    # enforces an explicit terminal completion marker (or cancellation).
    if data_lines:
        yield "\n".join(data_lines)
