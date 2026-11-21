import subprocess
import sys


def run(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, "greet.py", *args], check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def test_default_and_named_greetings() -> None:
    assert run() == "Hello, friend!"
    assert run("--name", "Ada") == "Hello, Ada!"


def test_uppercase_flag() -> None:
    assert run("--name", "Ada", "--uppercase") == "HELLO, ADA!"
