from helpers import greet
from card import card_line
from summary import summary_line


def test_all_references_use_new_name() -> None:
    assert greet("Ada") == "Hello, Ada!"
    assert card_line("Ada") == "Hello, Ada!"
    assert summary_line("Ada") == "Summary: Hello, Ada!"
