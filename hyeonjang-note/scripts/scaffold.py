"""Copy the sanitized 현장노트 starter into a new, empty project directory."""

from pathlib import Path
import argparse
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new project directory")
    args = parser.parse_args()
    source = (Path(__file__).resolve().parent.parent / "assets" / "starter").resolve()
    output = args.output.resolve()
    if not source.is_dir():
        parser.error("starter assets are missing")
    if output == source or source in output.parents:
        parser.error("output must be outside the skill's starter directory")
    if output.exists():
        parser.error("output already exists; choose a new directory")
    shutil.copytree(source, output)
    print(f"Created project starter: {output}")
    print("Create a new Sites project before adding its .openai/hosting.json; never reuse the existing Site ID.")


if __name__ == "__main__":
    main()
