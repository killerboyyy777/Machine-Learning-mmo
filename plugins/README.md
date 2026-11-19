# Drop-in agents

Put a `*.py` file in this folder and the conductor can run it as an agent.
Nothing else to wire: the training entry point scans this directory on
startup (`--plugin-dir`, which defaults to here), imports every file and
admits the plugins whose manifest is sound.

## The contract

A plugin is a subclass of `AgentPlugin` that is registered and declares a
name, a family and a config schema:

```python
from ml.plugins import AgentPlugin, register


@register
class Hello(AgentPlugin):
    name = "hello"          # selectable with --slot hello
    agent_type = "custom"   # linear, scripted, torch or custom

    @classmethod
    def config_schema(cls):
        return {"speed": {"type": int, "default": 3,
                          "help": "how hard to try"}}

    def act(self, obs, agent_id, mask=None):
        return 0            # any action index the mask allows
```

`--slot hello:speed=5` then runs it. Config keys become constructor
arguments, so `__init__` is inherited and validated: an unknown key or a
value of the wrong type is refused with the schema in the message.

Optional hooks, all no-ops by default: `on_episode_end(info)`,
`reward(signals)`, `learn(prev_obs, action, reward, next_obs, done)`,
`save(path)` and `load(path)`. A plugin that overrides `select(env)` instead
of `act` gets a policy built from it.

## What admission checks

On startup every newly imported plugin is checked, and one that fails is
dropped from the registry with its reason printed, without stopping the
others:

- the name is a non-empty string of lowercase letters, digits, `-` or `_`,
  and not the reserved `base` (duplicates are refused at import time);
- `agent_type` is one of `linear`, `scripted`, `torch`, `custom` — per-type
  status and PBT read that label, so an invented family lands in a bucket
  nothing else shares;
- `config_schema()` returns a dict of `{name: {"type": ...}}` entries;
- the plugin can be built from its own defaults.

A file that does not even import stops the run: a broken drop-in should fail
loudly at startup, not vanish and leave a slot silently empty.

## What this does not do yet

Plugins run in the conductor's process. There is no sandbox, and a policy
that blocks or raises inside `act` can still take a run with it. Process-level
admission and a wall-clock budget on the policy are the next phase (#176).
