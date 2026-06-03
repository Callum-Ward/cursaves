with open("cursor_saves/cli.py", "r", encoding="utf-8") as f:
    text = f.read()

# Fix E402
imports = "from .reload import print_reload_hint\nfrom .watch import watch_loop\n"
text = text.replace(imports, "")
text = text.replace("from . import __version__, db, export, paths\n", "from . import __version__, db, export, paths\n" + imports)

# Fix F821 undefined name `indices`
text = text.replace("len(indices) project(s)", "len(projects) project(s)")

with open("cursor_saves/cli.py", "w", encoding="utf-8") as f:
    f.write(text)
