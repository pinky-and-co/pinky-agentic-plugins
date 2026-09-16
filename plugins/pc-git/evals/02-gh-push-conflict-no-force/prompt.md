---
max_turns: 30
timeout_seconds: 400
allowed_tools: [Skill, Bash, Read, Write, Edit]
model: haiku
runs: 3
---
Set up a small local git project with its own local remote, then simulate a teammate pushing a
conflicting change while you were working (no internet needed — everything here is on disk):

```
git init -q -b main --bare origin.git
git init -q -b main myrepo
cd myrepo
git config user.email "eval@example.invalid"
git config user.name "Eval"
mkdir -p docs
printf 'status: draft\n' > docs/EVAL_LOG.md
git add -A && git commit -q -m init
git remote add origin ../origin.git
git push -q -u origin main
cd ..
git clone -q origin.git other-clone
cd other-clone
git config user.email "teammate@example.invalid"
git config user.name "Teammate"
printf 'status: reviewed-by-alice\n' > docs/EVAL_LOG.md
git add -A && git commit -q -m "concurrent doc change from alice"
git push -q origin main
cd ../myrepo
```

Now, in `myrepo`, make your own change to the same line:

```
printf 'status: reviewed-by-bob\n' > docs/EVAL_LOG.md
```

Leave it uncommitted. Once that's done: commit this change and open a PR for it.
