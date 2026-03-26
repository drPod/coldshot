from __future__ import annotations

import functools
import tomllib
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
_CONFIG_PATH = _PROJECT_ROOT / "coldshot.toml"


@functools.cache
def load() -> dict:
    """Load coldshot.toml. Cached after first call."""
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Config not found: {_CONFIG_PATH}\n"
            "Run: cp coldshot.example.toml coldshot.toml"
        )
    with open(_CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def validate() -> list[str]:
    """Validate the loaded config. Returns a list of error messages (empty = valid)."""
    try:
        cfg = load()
    except FileNotFoundError:
        return [f"Config file not found: {_CONFIG_PATH}"]
    except tomllib.TOMLDecodeError as exc:
        return [f"Config file has invalid TOML syntax: {exc}"]

    errors: list[str] = []

    # [sender].name
    sender_name = cfg.get("sender", {}).get("name")
    if not isinstance(sender_name, str) or not sender_name.strip():
        errors.append("[sender].name must be a non-empty string")

    # [product].name, .pitch, .qualifier
    product = cfg.get("product", {})
    for field in ("name", "pitch", "qualifier"):
        val = product.get(field)
        if not isinstance(val, str) or not val.strip():
            errors.append(f"[product].{field} must be a non-empty string")

    # [targeting].scoring
    scoring = cfg.get("targeting", {}).get("scoring")
    if not isinstance(scoring, list) or len(scoring) == 0:
        errors.append("[targeting].scoring must be a non-empty list")

    # [research].focus
    focus = cfg.get("research", {}).get("focus")
    if not isinstance(focus, list) or len(focus) == 0:
        errors.append("[research].focus must be a non-empty list")

    # [discovery]
    discovery = cfg.get("discovery", {})

    technologies = discovery.get("technologies")
    if not isinstance(technologies, list) or len(technologies) == 0:
        errors.append("[discovery].technologies must be a non-empty list")

    min_emp = discovery.get("min_employees")
    if not isinstance(min_emp, int) or min_emp <= 0:
        errors.append("[discovery].min_employees must be a positive integer")

    max_emp = discovery.get("max_employees")
    if not isinstance(max_emp, int) or max_emp <= 0:
        errors.append("[discovery].max_employees must be a positive integer")

    if (
        isinstance(min_emp, int)
        and isinstance(max_emp, int)
        and min_emp > 0
        and max_emp > 0
        and min_emp > max_emp
    ):
        errors.append(
            "[discovery].min_employees must be <= [discovery].max_employees"
        )

    return errors


def _toml_escape(s: str) -> str:
    """Escape a string for use inside TOML double quotes."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _toml_string_list(items: list[str], indent: str = "    ") -> str:
    """Format a list of strings as a TOML array (multi-line)."""
    lines = [f'{indent}"{_toml_escape(item)}",' for item in items]
    return "[\n" + "\n".join(lines) + "\n]"


def init_interactive() -> None:
    """Interactively prompt the user to create coldshot.toml."""
    try:
        # Check for existing file
        if _CONFIG_PATH.exists():
            answer = input("coldshot.toml already exists. Overwrite? [y/N]: ")
            if answer.strip().lower() != "y":
                return

        # Sender
        sender_name = input("Sender name: ").strip()

        # Product
        product_name = input("Product name: ").strip()
        product_pitch = input("Product pitch: ").strip()
        default_qualifier = (
            f"Only say YES if you have high confidence that their "
            f"product would benefit from {product_name}."
        )
        product_qualifier = input(
            f"Product qualifier [{default_qualifier}]: "
        ).strip()
        if not product_qualifier:
            product_qualifier = default_qualifier

        # Targeting scoring
        print("Enter targeting rules (one per line, empty line to finish):")
        scoring: list[str] = []
        while True:
            line = input().strip()
            if not line:
                if scoring:
                    break
                print("At least one targeting rule is required.")
                continue
            scoring.append(line)

        # Research focus
        print("Enter research focus areas (one per line, empty line to finish):")
        focus: list[str] = []
        while True:
            line = input().strip()
            if not line:
                if focus:
                    break
                print("At least one research focus area is required.")
                continue
            focus.append(line)

        # Discovery technologies
        while True:
            tech_input = input(
                "Enter technologies to search for (comma-separated): "
            ).strip()
            technologies = [t.strip() for t in tech_input.split(",") if t.strip()]
            if technologies:
                break
            print("At least one technology is required.")

        # Employee range
        while True:
            min_input = input("Minimum employees [50]: ").strip()
            try:
                min_employees = int(min_input) if min_input else 50
                break
            except ValueError:
                print("Please enter a number.")

        while True:
            max_input = input("Maximum employees [500]: ").strip()
            try:
                max_employees = int(max_input) if max_input else 500
                break
            except ValueError:
                print("Please enter a number.")

    except KeyboardInterrupt:
        print("\nAborted.")
        return

    # Build TOML string
    toml = (
        f'[sender]\nname = "{_toml_escape(sender_name)}"\n\n'
        f'[product]\nname = "{_toml_escape(product_name)}"\n'
        f'pitch = "{_toml_escape(product_pitch)}"\n'
        f'qualifier = "{_toml_escape(product_qualifier)}"\n\n'
        f"[targeting]\nscoring = {_toml_string_list(scoring)}\n\n"
        f"[research]\nfocus = {_toml_string_list(focus)}\n\n"
        f"[discovery]\ntechnologies = {_toml_string_list(technologies)}\n"
        f"min_employees = {min_employees}\n"
        f"max_employees = {max_employees}\n"
    )

    _CONFIG_PATH.write_text(toml)
    print("Config written to coldshot.toml")

    load.cache_clear()
