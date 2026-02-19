"""Trigger a Cursor window reload to pick up database changes."""

import platform
import shutil
import subprocess
import sys


def reload_cursor_window() -> bool:
    """Attempt to trigger 'Developer: Reload Window' in Cursor.

    Cursor caches all conversation data in memory at startup and never
    watches the SQLite files for external changes.  The only way to make
    it re-read the database is to reload the renderer process, which is
    what 'Developer: Reload Window' does.

    Returns True if the reload was triggered, False if we couldn't.
    """
    system = platform.system()

    if system == "Darwin":
        return _reload_macos()
    elif system == "Linux":
        return _reload_linux()
    else:
        return False


def _reload_macos() -> bool:
    """Use osascript to send Cmd+Shift+P and type the reload command."""
    try:
        # Check if Cursor is running
        result = subprocess.run(
            ["pgrep", "-f", "Cursor"],
            capture_output=True,
        )
        if result.returncode != 0:
            print("  Cursor is not running, skipping reload.", file=sys.stderr)
            return False

        # Use AppleScript to trigger the command palette and reload
        script = '''
            tell application "Cursor" to activate
            delay 0.3
            tell application "System Events"
                keystroke "p" using {command down, shift down}
                delay 0.4
                keystroke "Developer: Reload Window"
                delay 0.3
                key code 36  -- Return
            end tell
        '''
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0

    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _reload_linux() -> bool:
    """Use xdotool to send Ctrl+Shift+P and type the reload command."""
    if not shutil.which("xdotool"):
        return False

    try:
        # Find Cursor windows
        result = subprocess.run(
            ["xdotool", "search", "--name", "Cursor"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            print("  No Cursor window found, skipping reload.", file=sys.stderr)
            return False

        # Get the first window ID
        window_id = result.stdout.strip().split("\n")[0]

        # Activate the window and send the command palette shortcut
        subprocess.run(
            ["xdotool", "windowactivate", "--sync", window_id],
            capture_output=True,
            timeout=5,
        )

        # Send Ctrl+Shift+P to open command palette
        subprocess.run(
            ["xdotool", "key", "--window", window_id,
             "ctrl+shift+p"],
            capture_output=True,
            timeout=5,
        )

        # Small delay for command palette to open
        import time
        time.sleep(0.4)

        # Type the reload command
        subprocess.run(
            ["xdotool", "type", "--delay", "20",
             "Developer: Reload Window"],
            capture_output=True,
            timeout=5,
        )

        time.sleep(0.3)

        # Press Enter
        subprocess.run(
            ["xdotool", "key", "Return"],
            capture_output=True,
            timeout=5,
        )

        return True

    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def print_reload_hint():
    """Print instructions for restarting Cursor to pick up changes."""
    print("Restart Cursor (quit and reopen) to see imported conversations.")
