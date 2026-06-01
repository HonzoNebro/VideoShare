from pathlib import Path
import subprocess


def test_shell_scripts_have_valid_syntax() -> None:
    for script in Path("scripts").glob("*.sh"):
        subprocess.run(["bash", "-n", str(script)], check=True)
