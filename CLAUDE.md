# Working rules

- Do all work on the `dev` branch, never directly on `main`. Create it from `main` if it doesn't exist.
- After each completed change, commit it with a short descriptive message. Don't leave work uncommitted at the end of a turn.
- Never push, and never merge into `main`, unless asked.

# Running

`python app.py [chapters-folder] [--port N]` starts the editor at http://127.0.0.1:8765. It restarts itself when a `.py` file changes.
