"""Tiny greeting CLI for the evaluation fixture."""

import argparse


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="friend")
    args = parser.parse_args()
    print(f"Hello, {args.name}!")


if __name__ == "__main__":
    main()
