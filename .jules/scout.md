# Scout Journal

## Cleared Areas
- 2026-09-27: `ml/plugins/torch_plugin.py` - Fixed `KeyError: 'done'` in `TorchPlugin.learn`.

## Findings & Bug Families
- `TorchPlugin.learn` omitted the `"done": done` key when storing transition dictionaries into `TorchDQNAgent` replay buffer. When `TorchDQNAgent.learn()` sampled minibatches and looked up `b["done"]`, it raised `KeyError: 'done'`. In `AgentTask.run()`, exceptions inside `learn_hook` were caught and ignored, causing gradient updates for DQN agents under `TorchPlugin` to silently fail every step.
