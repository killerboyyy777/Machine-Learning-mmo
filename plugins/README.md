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

On startup each file is imported once in a child process first; the survivors
are imported here and checked. One that fails is dropped from the registry
with its reason printed, without stopping the others:

- the name is a non-empty string of lowercase letters, digits, `-` or `_`,
  and not the reserved `base` (duplicates are refused at import time);
- `agent_type` is one of `linear`, `scripted`, `torch`, `custom` ÔÇö per-type
  status and PBT read that label, so an invented family lands in a bucket
  nothing else shares;
- `config_schema()` returns a dict of `{name: {"type": ...}}` entries;
- the plugin can be built from its own defaults.

A file that crashes or hangs on import is caught by that probe and named as
rejected, so a broken drop-in costs its own slot instead of the whole run.

## What this does not do yet

Plugins run in the conductor's process. There is no sandbox: a policy that
raises inside `act` takes its own agent down (the supervisor logs the death and
restarts it) and a policy that hangs costs that agent its episode. Both are
contained to one agent, not the fleet.

`--policy-timeout` (default 5 seconds per step; `0` runs the policy inline on
the event loop, the pre-#176 behaviour) bounds the hang by running the policy
on a worker thread. A timed-out call abandons its thread, because Python cannot
interrupt it: the agent dies, the thread leaks until the call returns.

Every plugin instance gets its own single worker thread, and agents that share
an instance (the agents of one `--slot`) share that worker. So `act` is never
entered by two threads at once for the same plugin -- a stateful policy needs
no lock -- and a plugin that wedges only blocks itself: it cannot occupy the
threads a healthy plugin behind a different slot needs. A policy that raises
`TimeoutError` itself is reported as its own crash, not as a budget expiry.

The import probe runs in a child process, but a plugin's policy does not: the
first `act` still happens in the conductor's process, so a policy that only
fails on a real observation gets no rehearsal. Running a policy against a
fixture in a child, and isolating a plugin process for the whole run, remain
(#176).
