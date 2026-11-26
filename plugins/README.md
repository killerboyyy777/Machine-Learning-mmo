# Drop-in agents

Put a `*.py` file in this folder and the conductor can run it as an agent. There is nothing else to wire. The training entry point scans this directory on startup (`--plugin-dir`, which defaults to here), imports every file, and admits the plugins whose manifest is sound.

## The contract

A plugin is a subclass of `AgentPlugin` that is registered and declares a name, a family, and a config schema.

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

`--slot hello:speed=5` then runs it. Config keys become constructor arguments, so `__init__` is inherited and validated. An unknown key or a value of the wrong type is refused with the schema in the message.

Optional hooks are all no-ops by default: `on_episode_end(info)`, `reward(signals)`, `learn(prev_obs, action, reward, next_obs, done)`, `save(path)`, and `load(path)`. A plugin that overrides `select(env)` instead of `act` gets a policy built from it.

## What admission checks

On startup each file is imported once in a child process first. The survivors are imported in the conductor and checked. A plugin that fails is dropped from the registry with its reason printed, without stopping the others:

- The name is a non-empty string of lowercase letters, digits, `-`, or `_`, and is not the reserved `base`. Duplicates are refused at import time.
- `agent_type` is one of `linear`, `scripted`, `torch`, or `custom`. Per-type status and PBT read that label, so an invented family lands in a bucket nothing else shares.
- `config_schema()` returns a dict of `{name: {"type": ...}}` entries.
- The plugin can be built from its own defaults.

A file that crashes or hangs on import is caught by the child probe and named as rejected. A broken drop-in costs its own slot, not the whole run.

## What this does not do yet

Plugins run in the conductor's process. There is no sandbox. A policy exception or timeout ends that agent's episode and records a death. The conductor can replace the agent, but three consecutive policy faults from the same plugin policy retire that slot. A successfully completed episode resets its fault count. Other slots continue running.

`--policy-timeout` defaults to 5 seconds per step. With a positive timeout, the policy runs on a dedicated daemon worker thread, so a hung call costs its agent the episode instead of blocking the event loop. A timed-out call abandons its worker because Python cannot interrupt it; the call keeps running until the process exits, but the daemon worker is never joined at interpreter shutdown, so it cannot hold the conductor process open. Each plugin policy closure has its own single-worker executor, shared by the agents using that slot, so a wedged plugin does not occupy another slot's worker.

Setting `--policy-timeout 0` restores the pre-#176 inline behavior, where policy code runs on the event-loop thread. A blocking or infinite policy can then stall the whole conductor, not just its own agent. Do not use zero with untrusted or potentially blocking plugins.

Every plugin instance gets its own single worker thread, and agents that share an instance, meaning the agents of one `--slot`, share that worker. The conductor never enters `act` from two threads at once for the same plugin, so a stateful policy needs no lock. A plugin that wedges blocks only itself and cannot occupy the threads a healthy plugin behind a different slot needs. A policy that raises `TimeoutError` itself is reported as its own crash, not as a budget expiry.

The import probe runs in a child process, but a plugin's policy does not. The first `act` still happens in the conductor's process, so a policy that fails only on a real observation gets no rehearsal. Running a policy against a fixture in a child, and isolating a plugin process for the whole run, remain open work under #176.
