#!/usr/bin/env python3
"""
Skill Packager - Creates a distributable .skill file of a skill folder

Usage:
    python -m scripts.package_skill <path/to/skill-folder> [output-directory]

Example:
    python -m scripts.package_skill ./my-skill
    python -m scripts.package_skill ./my-skill ./dist
"""

import fnmatch
import stat
import sys
import zipfile
from pathlib import Path
from scripts.quick_validate import validate_skill


EXCLUDE_DIRS = {"__pycache__", "node_modules"}
EXCLUDE_GLOBS = {"*.pyc"}
EXCLUDE_FILES = {".DS_Store"}

ROOT_EXCLUDE_DIRS = {"evals"}


def should_exclude(rel_path: Path) -> bool:
    """Check if a path should be excluded from packaging."""
    parts = rel_path.parts
    if any(part in EXCLUDE_DIRS for part in parts):
        return True


    if len(parts) > 1 and parts[1] in ROOT_EXCLUDE_DIRS:
        return True
    name = rel_path.name
    if name in EXCLUDE_FILES:
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in EXCLUDE_GLOBS)


def package_skill(skill_path, output_dir=None):
    """
    Package a skill folder into a .skill file.

    Args:
        skill_path: Path to the skill folder
        output_dir: Optional output directory for the .skill file (defaults to current directory)

    Returns:
        Path to the created .skill file, or None if error
    """
    skill_path = Path(skill_path).resolve()


    if not skill_path.exists():
        print(f"❌ Error: Skill folder not found: {skill_path}")
        return None

    if not skill_path.is_dir():
        print(f"❌ Error: Path is not a directory: {skill_path}")
        return None




    skill_md = skill_path / "SKILL.md"
    try:
        md = skill_md.lstat()
    except OSError:
        print(f"❌ Error: SKILL.md not found in {skill_path}")
        return None
    if not stat.S_ISREG(md.st_mode) or md.st_nlink > 1:
        print("❌ Error: SKILL.md must be the skill folder's own regular file, not a link, directory or special file")
        return None


    print("🔍 Validating skill...")
    valid, message = validate_skill(skill_path)
    if not valid:
        print(f"❌ Validation failed: {message}")
        print("   Please fix the validation errors before packaging.")
        return None
    print(f"✅ {message}\n")


    skill_name = skill_path.name
    if output_dir:
        output_path = Path(output_dir).resolve()
        output_path.mkdir(parents=True, exist_ok=True)
    else:
        output_path = Path.cwd()

    skill_filename = output_path / f"{skill_name}.skill"


    try:




        left_out = 0
        with zipfile.ZipFile(skill_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:

            for file_path in skill_path.rglob('*'):
                arcname = file_path.relative_to(skill_path.parent)
                try:
                    st = file_path.lstat()
                except OSError as e:
                    print(f"  Skipped (unreadable: {e.strerror}): {arcname}")
                    left_out += 1
                    continue
                if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
                    continue
                if should_exclude(arcname):
                    print(f"  Skipped: {arcname}")
                    continue
                if stat.S_ISLNK(st.st_mode):
                    print(f"  Skipped (symlink): {arcname}")
                    left_out += 1
                    continue
                if st.st_nlink > 1:
                    print(f"  Skipped (hard link: the file has another name elsewhere): {arcname}")
                    left_out += 1
                    continue
                if skill_path not in file_path.resolve().parents:
                    print(f"  Skipped (resolves outside the skill folder): {arcname}")
                    left_out += 1
                    continue
                zipf.write(file_path, arcname)
                print(f"  Added: {arcname}")

        if left_out:
            print(
                f"\n⚠ {left_out} file(s) were left out because they are links, resolve outside "
                "the skill folder or could not be read (package-manager installs made inside the "
                "folder are the usual cause; such directories should not be packaged). The archive "
                "does not contain them."
            )
        print(f"\n✅ Successfully packaged skill to: {skill_filename}")
        return skill_filename

    except Exception as e:
        print(f"❌ Error creating .skill file: {e}")
        return None


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m scripts.package_skill <path/to/skill-folder> [output-directory]")
        print("\nExample:")
        print("  python -m scripts.package_skill ./my-skill")
        print("  python -m scripts.package_skill ./my-skill ./dist")
        sys.exit(1)

    skill_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else None

    print(f"📦 Packaging skill: {skill_path}")
    if output_dir:
        print(f"   Output directory: {output_dir}")
    print()

    result = package_skill(skill_path, output_dir)

    if result:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
