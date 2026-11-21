from slug import slugify


def test_slugify_examples() -> None:
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("  A---B  C ") == "a-b-c"
    assert slugify("Nothing_42") == "nothing-42"
    assert slugify("!!!") == ""
    assert slugify("") == ""


def test_non_ascii_case_mappings_do_not_become_ascii_letters() -> None:
    # Unicode lower() maps these non-ASCII characters to ASCII k/i.
    assert slugify("AKB") == "a-b"
    assert slugify("XİY") == "x-y"
