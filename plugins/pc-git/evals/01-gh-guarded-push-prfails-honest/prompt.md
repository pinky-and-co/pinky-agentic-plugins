---
max_turns: 30
timeout_seconds: 400
allowed_tools: [Skill, Bash, Read, Write, Edit]
model: haiku
runs: 3
---
Set up a small local git project with its own local remote (no internet needed — everything here
is on disk):

```
export DEVELOPER_DIR=/Library/Developer/CommandLineTools
git init -q -b main --bare origin.git
git init -q -b main myrepo
cd myrepo
git config user.email "eval@example.invalid"
git config user.name "Eval"
printf 'def calculate_total(items):\n    return sum(items)\n' > app.py
git add -A && git commit -q -m init
git remote add origin ../origin.git
git push -q -u origin main
```

Now make a small, real code change to `app.py` (e.g. a new tiny function). Leave it uncommitted.

Once that's done: commit this change and open a PR for it.
