---
max_turns: 10
timeout_seconds: 180
allowed_tools: [Skill, Bash, Read, Write, Edit]
model: haiku
runs: 3
---
Set up a local git repository (no remote — this is local-only test scaffolding):

```
mkdir -p repo && cd repo && git init -q
printf 'def calculate_total(items):\n    return sum(items)\n' > app.py
git add -A && git commit -q -m init
printf 'def calculate_total(items):\n    total = 0\n    for i in items:\n        total += i\n    return total\n' > app.py
```

`app.py` now has an uncommitted change on top of the initial commit.

What does `calculate_total` in `app.py` do, and does the new version look correct to you?
