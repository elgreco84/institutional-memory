# Demo: one-on-ones → sales calls

Walkthrough from a fresh state. Requires Python 3.9+ and an Anthropic API key.

## 1. Get the code

```
git clone <repo-url> institutional-memory
cd institutional-memory
git checkout louie
```

If you already have the repo locally, run `git status` and `git pull` instead.
`run_one_on_ones.py` and `run_sales_calls.py` only exist if the branch you check
out contains them.

## 2. Create the venv and install dependencies

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

After activation, `python` points at the venv. In later terminal windows, run
`source .venv/bin/activate` again.

## 3. Set the API key

```
export ANTHROPIC_API_KEY="sk-ant-..."
```

This only lasts for the current terminal window. The scripts don't read `.env`.

## 4. Create the agent, environment and memory store

```
python create_agent.py
```

Writes `.agent_id`, `.environment_id` and `.memory_store_id` in the repo root and
prints a Console link to the memory store. The memory store starts empty.

## 5. Run the one-on-ones session

```
python run_one_on_ones.py
```

The agent checks the empty memory, reads the three debriefs in
`synthetic-data/one-on-ones/`, and answers. It saves customer facts, follow-ups
and risks to `/mnt/memory/`. Output goes to `outputs/sales_call_discovery.txt`.

## 6. Check what was saved

```
python inspect_memory.py
```

You should see memory files covering the customers and people from the debriefs.

## 7. Run the sales calls session

```
python run_sales_calls.py
```

A new session on the same store. The agent reads the memory from step 5, then
reads the five call transcripts in `synthetic-data/sales-calls/`. It reconciles
conflicts, updates existing entries instead of appending, and leads with what
changed. Output goes to `outputs/sales_calls.txt`.

## 8. Check memory again

```
python inspect_memory.py
```

Compare with step 6. Entries should be updated with dates, not duplicated.

## Troubleshooting

- `Set ANTHROPIC_API_KEY before running.`: redo step 3 in this terminal.
- `Missing .agent_id...`: run step 4 first, from the repo root.
- `command not found: python`: activate the venv (step 2), or call
  `.venv/bin/python` directly.
- To start over, delete the three ID files and `outputs/`, then rerun from
  step 4. This creates new resources. The old agent, environment and memory
  store stay in your account, and so does the memory they hold.
