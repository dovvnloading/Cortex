from pkg.formatting import welcome


def test_welcome():
    assert welcome('Ada') == 'Hello Ada'
