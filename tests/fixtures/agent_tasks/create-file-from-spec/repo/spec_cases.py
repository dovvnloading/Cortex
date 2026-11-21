from slug import slugify


def test_slugify_examples() -> None:
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("  A---B  C ") == "a-b-c"
    assert slugify("Nothing_42") == "nothing-42"
    assert slugify("!!!") == ""
    assert slugify("") == ""
