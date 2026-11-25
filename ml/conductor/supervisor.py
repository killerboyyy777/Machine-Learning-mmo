"""Supervisor: per-task isolation, episode management, fault recovery.

Each agent runs in its own isolated task (asyncio Task). If an agent crashes,
the supervisor catches the exception, marks it dead, and spawns a replacement
without affecting other agents.
"""

import asyncio
import inspect
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import websockets

from ml.transcript import TranscriptWriter

# Reconnect backoff for dropped server connections (#290, slice 6/6):
# exponential per consecutive failure, capped, plus a deterministic
# per-agent stagger so a server restart doesn't thundering-herd it.
_RECONNECT_BASE_S = 1.0
_RECONNECT_MAX_S = 60.0
# A disconnect is never a crash: these escape neither reset (retried
# below) nor the step loop (they end the episode instead).
_CONNECTION_ERRORS = (
    ConnectionError,
    OSError,
    asyncio.TimeoutError,
    websockets.ConnectionClosed,
)
# #176: a plugin that raises or overruns its budget on every step is not a
# run of bad luck, it is a broken plugin, and one death per respawn is a
# crash loop that burns the fleet's agent slots. Three faults retire the
# policy; the conductor then stops scheduling it and names it in the log.
POLICY_FAULT_LIMIT = 3


def _env_floor(env):
    """Best-effort floor/cell id for an env (TextMMOEnv state shape).

    Dungeon floors become "d<N>", surface rooms keep their room id,
    anything unrecognized becomes "unknown" (Mixer drops those)."""
    s = getattr(env, "_state", None) or {}
    if s.get("is_dungeon"):
        return f"d{s.get('dungeon_floor', 0) or 0}"
    return s.get("room_id") or "unknown"


class PolicyTimeout(Exception):
    """A policy call outlived its wall-clock budget (#176).

    Distinct from ``TimeoutError`` on purpose: a plugin may raise that
    itself, and a budget expiry is the supervisor's finding, not the
    plugin's.
    """

    def __init__(self, budget, episode):
        super().__init__(f"policy timeout after {budget}s (episode {episode})")
        self.budget = budget
        self.episode = episode


class PolicyFailure(RuntimeError):
    """The policy failed in a way that belongs to the plugin, not the world.

    Raised instead of letting the plugin's own exception reach the generic
    crash path, so the supervisor can tell "this plugin is broken" from
    "this agent hit a bad episode" and retire a plugin that keeps doing it.
    RuntimeError keeps the pre-#176 contract for callers that caught the
    budget expiry by that type.
    """

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class AgentTask:
    """Wraps one agent's episode loop with fault isolation."""

    def __init__(
        self,
        agent_id,
        env,
        policy_fn,
        max_steps=2000,
        step_timeout=None,
        policy_timeout=None,
        policy_executor=None,
        learn_hook=None,
        reward_hook=None,
        transcript_writer=None,
        episode_offset=0,
        telemetry=None,
    ):
        self.agent_id = agent_id
        self.env = env
        self.policy_fn = policy_fn
        self.max_steps = max_steps
        # Per-step learning hook (#292, slice 5/6): called as
        # hook(prev_obs, action, reward, next_obs, done) after every env
        # step; wires the transition into LinearQAgent.update /
        # TorchDQNAgent.store+learn (dead code from the conductor until
        # now). Never raises (guarded per call); None disables.
        self.learn_hook = learn_hook
        # Plugin reward hook (#68). Lives on the env, not here: the reward
        # is computed inside env.step(), before the supervisor sees it, so
        # this is a carrier handed over in start_agent and attached to the
        # env right after construction. None disables.
        self.reward_hook = reward_hook
        # Action-level transcript (#624). One JSON line per step, written
        # as the step happens so a finding carries its trajectory. None
        # disables; never raises (guarded per call) like learn_hook.
        self.transcript_writer = transcript_writer
        # Watchdog (#50): max wall-clock seconds per env.step; a hung
        # step ends the episode instead of wedging the task forever.
        # None disables. Only guards the async env call.
        self.step_timeout = step_timeout
        # Policy budget (#176): a sync policy_fn runs on the loop thread,
        # so the step watchdog cannot cover it and one wedged policy takes
        # every agent down with it. When set, the policy runs on a worker
        # thread and this bounds the wait; the abandoned thread is the
        # price of an in-process plugin (see the isolation plan).
        self.policy_timeout = policy_timeout
        # The worker the policy runs on. The supervisor hands every agent of
        # a slot the executor that belongs to the slot's shared plugin
        # instance; a task built on its own lazily gets one (#176 review).
        self._policy_executor = policy_executor
        self._own_executor = None
        self._policy_arity = None  # probed from the fn signature once
        self.task = None
        self.total_reward = 0.0
        self.steps = 0
        # PBT restarts rebuild this task for the same agent. Carry the
        # episode count so the shared transcript keeps a monotonic key
        # instead of starting a second episode-1 sequence (#703 review).
        self.episodes = episode_offset
        # Consecutive failed connects (reset on every success); drives
        # the reconnect backoff. Disconnects never kill the task.
        self.reconnects = 0
        self.telemetry = telemetry
        self.last_error = None
        self.done = asyncio.Event()

    async def _call_policy(self, args):
        """Call the policy, off the loop when a budget is configured.

        Plugins are synchronous by contract, so without a budget the call
        happens inline exactly as it always has. With one, the call moves
        to a worker thread and a policy that never returns costs this task
        its episode instead of the whole fleet its event loop.

        The worker comes from a single-thread executor per policy closure,
        never from the loop's shared default pool: agents in a slot share
        one plugin instance, so one worker keeps ``act`` single-threaded as
        the contract promises, while a wedged call only occupies the worker
        of the plugin that wedged and not the capacity a healthy agent
        behind a different plugin needs.

        A budget expiry and a policy that raises ``TimeoutError`` itself are
        different failures, so the wait cannot be ``wait_for``: that
        re-raises the policy's exception as if the budget had run out.
        """
        if self.policy_timeout is None:
            return self.policy_fn(*args)
        fut = asyncio.get_running_loop().run_in_executor(
            self.policy_executor(), self.policy_fn, *args
        )
        done, _pending = await asyncio.wait({fut}, timeout=self.policy_timeout)
        if not done:
            # Cancels the future, not the thread: Python cannot interrupt a
            # running call, so the worker stays busy until it returns.
            fut.cancel()
            raise PolicyTimeout(self.policy_timeout, self.episodes + 1)
        return fut.result()

    def policy_executor(self):
        """The single-worker executor this task's policy runs on."""
        if self._policy_executor is not None:
            return self._policy_executor
        if self._own_executor is None:
            self._own_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=f"policy-{self.agent_id}"
            )
        return self._own_executor

    async def run(self):
        """Run episodes until the task is cancelled or the agent dies."""
        try:
            while not self.done.is_set():
                if self.telemetry is not None:
                    reset_started = time.perf_counter_ns()
                try:
                    obs = await self.env.reset()
                except _CONNECTION_ERRORS:
                    # Server down/restarting: back off (staggered per
                    # agent) and retry. A disconnect is not a death, so
                    # it never escapes to the crash path below.
                    self.reconnects += 1
                    delay = (
                        min(
                            _RECONNECT_MAX_S,
                            _RECONNECT_BASE_S * 2 ** min(self.reconnects, 6),
                        )
                        + (hash(self.agent_id) % 1000) / 1000.0
                    )
                    self.last_error = (
                        f"disconnected, reconnecting in {delay:.1f}s "
                        f"(attempt {self.reconnects})"
                    )
                    await asyncio.sleep(delay)
                    continue
                if self.telemetry is not None:
                    self.telemetry.record(
                        "env_reset_ms",
                        (time.perf_counter_ns() - reset_started) / 1e6,
                        agent_id=self.agent_id,
                        episode=self.episodes + 1,
                    )
                self.reconnects = 0
                episode_reward = 0.0
                for step in range(self.max_steps):
                    if self.done.is_set():
                        break
                    if self._policy_arity is None:
                        try:
                            n_params = len(inspect.signature(self.policy_fn).parameters)
                        except (TypeError, ValueError):
                            n_params = 2
                        # 2 args: (obs, agent_id); 3: + mask; 4: + env
                        # (scripted plugins work straight from the env).
                        self._policy_arity = n_params
                    if self._policy_arity >= 3:
                        try:
                            mask = self.env.valid_action_mask()
                        except Exception:
                            mask = None
                        if self._policy_arity >= 4:
                            call_args = (obs, self.agent_id, mask, self.env)
                        else:
                            call_args = (obs, self.agent_id, mask)
                    else:
                        call_args = (obs, self.agent_id)
                    if self.telemetry is not None:
                        step_started = time.perf_counter_ns()
                    try:
                        action = await self._call_policy(call_args)
                    except PolicyTimeout as exc:
                        # A budget exists to stop one plugin taking the
                        # fleet down; the task has to die for that, or the
                        # next episode retries the same wedged call.
                        self.last_error = str(exc)
                        raise PolicyFailure(self.last_error) from None
                    except Exception as exc:
                        # The plugin raised inside act(). Retrying in place
                        # would raise again, so this ends the task too - but
                        # tagged as the plugin's fault, not the world's, so
                        # the supervisor can retire it after a few of these.
                        self.last_error = (
                            f"plugin raised {type(exc).__name__}: {exc} "
                            f"(episode {self.episodes + 1})"
                        )
                        raise PolicyFailure(self.last_error) from exc
                    prev_obs = obs
                    try:
                        if self.step_timeout is not None:
                            obs, reward, done, info = await asyncio.wait_for(
                                self.env.step(action), self.step_timeout
                            )
                        else:
                            obs, reward, done, info = await self.env.step(action)
                    except TimeoutError:
                        self.last_error = (
                            f"step timeout after {self.step_timeout}s "
                            f"(episode {self.episodes + 1})"
                        )
                        break
                    except (OSError, websockets.ConnectionClosed):
                        # Socket died mid-episode (ml_env converts
                        # send-time closes; anything else surfaces here):
                        # end the episode, reconnect on the next reset.
                        # Not a crash, not a death.
                        self.last_error = (
                            f"connection lost mid-episode "
                            f"(episode {self.episodes + 1})"
                        )
                        break
                    episode_reward += reward
                    if self.telemetry is not None:
                        self.telemetry.record(
                            "agent_step_ms",
                            (time.perf_counter_ns() - step_started) / 1e6,
                            agent_id=self.agent_id,
                            episode=self.episodes + 1,
                            step=step,
                        )
                    self.steps += 1
                    if self.learn_hook is not None:
                        try:
                            self.learn_hook(prev_obs, action, reward, obs, done)
                        except Exception:
                            pass
                    if self.transcript_writer is not None:
                        try:
                            self.transcript_writer.record(
                                agent_id=self.agent_id,
                                episode=self.episodes + 1,
                                step=step,
                                prev_obs=prev_obs,
                                action=action,
                                reward=reward,
                                obs=obs,
                                done=done,
                                info=info,
                            )
                        except Exception:
                            pass
                    if done:
                        break
                self.episodes += 1
                self.total_reward += episode_reward
                yield {
                    "agent_id": self.agent_id,
                    "episode": self.episodes,
                    "reward": episode_reward,
                    "steps": self.steps,
                }
        except Exception:
            # Record, then re-raise: an escaping exception is a task-killing
            # crash, and the supervisor (not this generator) owns the
            # recovery policy + death accounting. CancelledError and
            # GeneratorExit are BaseExceptions: they still pass through.
            self.last_error = traceback.format_exc()
            raise
        finally:
            self.done.set()

    def cancel(self):
        self.done.set()
        if self.task and not self.task.done():
            self.task.cancel()

    async def aclose(self):
        """Stop the task AND close its env. Without the env close, the
        socket (and its reader task) stays open server-side: the server
        keeps a live, logged-in ghost that counts as online forever."""
        self.cancel()
        env_close = getattr(self.env, "close", None)
        if env_close is not None:
            try:
                await env_close()
            except Exception:
                pass


class Supervisor:
    """Manages isolated agent tasks with fault recovery."""

    def __init__(
        self,
        registry,
        max_concurrent=50,
        mixer=None,
        floor_fn=None,
        step_timeout=None,
        policy_timeout=None,
        metrics=None,
        episode_hook=None,
        transcript_dir=None,
        structured_log=None,
    ):
        self.registry = registry
        self.max_concurrent = max_concurrent
        # Mixer auto-integration (#51): every completed episode is fed to
        # the mixer via floor_fn(env) -- no manual record_reward calls.
        self._mixer = mixer
        self._floor_fn = floor_fn or _env_floor
        self._step_timeout = step_timeout
        self._policy_timeout = policy_timeout
        # Death accounting (#135): crashes and churn reaps are logged so
        # soak metrics can tell *why* agents die, not just that they did.
        self._metrics = metrics
        # Episode hook (#1): called as hook(agent_id, event) per completed
        # episode -- the conductor wires churn.report_episode here so
        # lifetimes count episodes, not wall-clock ticks.
        self._episode_hook = episode_hook
        # Transcript directory (#624): when set, each agent gets one JSONL
        # file of per-step observations/actions/rewards. None writes nothing.
        self._transcript_dir = transcript_dir
        self._structured_log = structured_log
        self._tasks = {}  # agent_id -> AgentTask
        # Policy closures are shared by every agent in a slot (one plugin
        # instance per slot), so the worker that serialises calls belongs to
        # the closure, not the agent; a rebuilt policy gets a fresh worker
        # (#176 review).
        self._policy_executors = {}  # policy closure -> single-worker executor
        # #176: faults and retirements per policy closure. Keyed the same
        # way as the executors, so one broken plugin instance is retired
        # rather than every agent that happened to be handed it.
        self._policy_faults = {}  # policy closure -> consecutive faults
        self._policy_failed = {}  # policy closure -> reason it was retired
        # Launch specs per agent so PBT exploit can restart losers with
        # fresh envs/policies that reload the copied weights (#228).
        # policy_factory(checkpoint_path, hparams) rebuilds the policy
        # from the loser's checkpoint file; None keeps the stored closure.
        self._specs = (
            {}
        )  # agent_id -> (env_factory, policy_fn, max_steps, step_timeout, policy_factory, learn_hook, reward_hook, policy_timeout)
        self._lock = asyncio.Lock()

    def _drop_specs(self, agent_ids):
        for aid in agent_ids:
            try:
                self._specs.pop(aid, None)
            except Exception:
                pass

    def _log_death(self, agent_id, episodes, total_reward, error=None):
        if self._metrics is None:
            return
        try:
            self._metrics.log_agent_death(agent_id, episodes, total_reward, error=error)
        except Exception:
            pass

    def _log_episode(self, agent_id, event):
        # Every completed episode is logged, unsampled: at observed soak
        # rates (~0.3 eps/s fleet-wide, ~120 bytes/row) this is tens of
        # bytes/s -- a 50MB segment lasts ~2 weeks, six of them ~3 months.
        # Rotation caps are the volume guard; revisit sampling if a run
        # ever rotates twice (none has).
        if self._metrics is None:
            return
        try:
            entry = self.registry.get(agent_id)
            goal = entry.goal if entry is not None else None
            self._metrics.log_episode(
                agent_id,
                event["episode"],
                event["reward"],
                event.get("steps", 0),
                goal=goal,
            )
        except Exception:
            pass

    def _executor_for(self, policy_fn):
        """The single-worker executor a policy closure runs on (#176 review).

        Agents in one slot share one plugin instance through one closure, so
        they share this worker and ``act`` is never entered twice at once. A
        wedged call therefore blocks only the plugin that wedged, never the
        loop's default pool that healthy agents behind other plugins use.
        Keyed by the closure itself; an unhashable callable returns None and
        the task falls back to its own worker.
        """
        try:
            executor = self._policy_executors.get(policy_fn)
            if executor is None:
                executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="policy"
                )
                self._policy_executors[policy_fn] = executor
            return executor
        except TypeError:
            return None

    def _record_policy_fault(self, policy_fn, reason):
        """Count a plugin-side failure; retire the policy at the limit (#176).

        Without this a plugin that raises on every step dies, is respawned
        onto the same slot, and dies again: three agent slots and three death
        rows per broken plugin, forever. Retiring the closure makes the
        conductor skip that slot, and the log say why, while the rest of the
        fleet keeps running.

        Faults are consecutive: a policy that manages a whole episode resets
        its count, so a plugin that fails occasionally is never retired.
        """
        try:
            faults = self._policy_faults.get(policy_fn, 0) + 1
            self._policy_faults[policy_fn] = faults
            if faults < POLICY_FAULT_LIMIT or policy_fn in self._policy_failed:
                return
            self._policy_failed[policy_fn] = reason
        except TypeError:
            # An unhashable policy cannot be tracked; its agents keep the old
            # one-death-per-crash behaviour rather than sharing a counter.
            return
        if self._metrics is not None:
            try:
                self._metrics.log(
                    "plugin_disabled", faults=faults, reason=str(reason)[-300:]
                )
            except Exception:
                pass
        print(
            f"[conductor] plugin disabled after {faults} policy faults: {reason}",
            file=sys.stderr,
        )
        if self._structured_log is not None:
            self._structured_log.log(
                "plugin_disabled", faults=faults, error=str(reason)
            )

    def _clear_policy_faults(self, policy_fn):
        try:
            self._policy_faults.pop(policy_fn, None)
        except TypeError:
            pass

    def policy_disabled(self, policy_fn):
        """Reason this policy was retired, or None while it is still used."""
        try:
            return self._policy_failed.get(policy_fn)
        except TypeError:
            return None

    def disabled_policies(self):
        """Snapshot of retired policies and the reason each was retired."""
        return dict(self._policy_failed)

    async def start_agent(
        self,
        agent_id,
        env_factory,
        policy_fn,
        max_steps=2000,
        step_timeout=None,
        policy_timeout=None,
        policy_factory=None,
        learn_hook=None,
        reward_hook=None,
        episode_offset=0,
        transcript_incarnation=0,
        transcript_started=None,
        telemetry=None,
    ):
        """Start an isolated task for one agent.

        Returns True when a task is running afterwards, False when the
        agent was skipped (duplicate, at capacity) or its factory raised.
        A raising factory must never propagate: one bad spawn used to end
        the whole conductor run.
        """
        async with self._lock:
            if agent_id in self._tasks:
                return False
            if len(self._tasks) >= self.max_concurrent:
                return False
            disabled = self.policy_disabled(policy_fn)
            if disabled is not None:
                # A retired plugin gets no new agent (#176): the crash would
                # only repeat the fault that retired it.
                if self._metrics is not None:
                    try:
                        self._metrics.log(
                            "spawn_error",
                            agent_id=agent_id,
                            error=f"policy disabled: {disabled}",
                        )
                    except Exception:
                        pass
                return False
            try:
                env = env_factory(agent_id)
            except Exception:
                return False
            # Carry the registry goal onto the env (#162 Phase 1): envs
            # expose it in step info (no reward change in this phase).
            # Plain assignment is the contract -- envs must tolerate it
            # (TextMMOEnv takes goal= in its constructor; test fakes are
            # plain objects).
            try:
                entry = self.registry.get(agent_id)
                if entry is not None and entry.goal:
                    env.goal = dict(entry.goal)
                # Plugin reward hook (#68). Plain assignment, same contract
                # as the goal: a slot can pair any plugin with any env, so
                # only envs that accept reward_fn act on it. Must run before
                # the task's first reset, or the first episode pays a
                # different reward than the rest.
                if reward_hook is not None:
                    env.reward_fn = reward_hook
                if telemetry is not None:
                    env.telemetry = telemetry
            except Exception:
                pass
            writer = None
            if self._transcript_dir is not None:
                writer = TranscriptWriter(
                    os.path.join(self._transcript_dir, f"{agent_id}.jsonl"),
                    incarnation=transcript_incarnation,
                    started=transcript_started,
                )
            at = AgentTask(
                agent_id,
                env,
                policy_fn,
                max_steps,
                step_timeout=(
                    step_timeout if step_timeout is not None else self._step_timeout
                ),
                policy_timeout=(
                    policy_timeout
                    if policy_timeout is not None
                    else self._policy_timeout
                ),
                policy_executor=self._executor_for(policy_fn),
                learn_hook=learn_hook,
                reward_hook=reward_hook,
                transcript_writer=writer,
                episode_offset=episode_offset,
                telemetry=telemetry,
            )
            self._tasks[agent_id] = at
            self._specs[agent_id] = (
                env_factory,
                policy_fn,
                max_steps,
                step_timeout,
                policy_factory,
                learn_hook,
                reward_hook,
                policy_timeout,
            )
            at.task = asyncio.ensure_future(self._run_with_recovery(at))
            return True

    async def _run_with_recovery(self, at):
        """Run an agent task, handling crashes gracefully."""
        try:
            async for event in at.run():
                # An episode finished under this policy, so whatever it did
                # wrong before was survivable: the fault budget counts
                # consecutive failures, not a lifetime total (#176).
                self._clear_policy_faults(at.policy_fn)
                self.registry.record_episode(at.agent_id, event["reward"])
                self._log_episode(at.agent_id, event)
                if self._episode_hook is not None:
                    try:
                        self._episode_hook(at.agent_id, event)
                    except Exception:
                        pass
                if self._mixer is not None:
                    try:
                        floor = self._floor_fn(at.env)
                    except Exception:
                        floor = None
                    if floor:
                        self._mixer.record_reward(floor, at.agent_id, event["reward"])
        except asyncio.CancelledError:
            pass
        except PolicyFailure as exc:
            # A plugin fault: the agent dies as it always did, but the cause
            # is attributed to the plugin so a repeat retires it (#176).
            entry = self.registry.get(at.agent_id)
            if entry:
                entry.alive = False
            self._log_death(at.agent_id, at.episodes, at.total_reward, error=str(exc))
            self._record_policy_fault(at.policy_fn, str(exc))
        except Exception:
            entry = self.registry.get(at.agent_id)
            if entry:
                entry.alive = False
            self._log_death(
                at.agent_id, at.episodes, at.total_reward, error=traceback.format_exc()
            )
        finally:
            # A restart installs a fresh task and spec while this one is
            # shutting down; its teardown must not drop the replacement.
            owns_spec = self._tasks.get(at.agent_id) is at
            self._tasks.pop(at.agent_id, None)
            if owns_spec:
                self._drop_specs((at.agent_id,))
            # The agent isn't running anymore (cancelled, crashed, or
            # reaped): drop it from the mixer too, or dead ids pile up in
            # floor lists and inflate rebalance/snapshot counts.
            if self._mixer is not None:
                try:
                    self._mixer.remove_agent(at.agent_id)
                except Exception:
                    pass

    async def _shutdown(self, at):
        """Cancel a task and close its env (idempotent, never raises,
        never blocks longer than a bounded close)."""
        if at is None:
            return
        try:
            await asyncio.wait_for(at.aclose(), 15.0)
        except Exception:
            pass
        task = at.task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), 5.0)
            except (asyncio.CancelledError, Exception):
                pass

    async def stop_agent(self, agent_id):
        async with self._lock:
            at = self._tasks.pop(agent_id, None)
            self._drop_specs((agent_id,))
        await self._shutdown(at)

    async def restart_agent(self, agent_id, hparams=None):
        """Stop + relaunch with the stored specs (PBT exploit, #228).

        The stored policy closure is NOT reused: it closes over the
        plugin instance that loaded the pre-exploit weights, so relaunching
        it would train stale weights while the copied checkpoint file sits
        untouched. Instead the stored policy_factory rebuilds the policy
        from the agent's checkpoint file (now holding the winner's weights)
        with the new hparams overlaid; a failing rebuild falls back to the
        stored closure so the agent survives. A successful restart
        re-assigns the agent to the mixer (shutdown dropped it). Returns
        True when a task runs after. Refusals (unknown/dead entries) leave
        any running task untouched.
        """
        async with self._lock:
            spec = self._specs.get(agent_id)
        if spec is None:
            return False
        entry = self.registry.get(agent_id)
        if entry is None or not entry.alive:
            self._drop_specs((agent_id,))
            return False
        if self.policy_disabled(spec[1]):
            # PBT must not hand a retired plugin another agent (#176): the
            # restart would fault again and trip the same limit.
            return False
        async with self._lock:
            at = self._tasks.pop(agent_id, None)
        # A PBT restart reuses the agent's transcript file. Carry its episode
        # count, incarnation and start clock so the next generation extends
        # one timeline instead of overwriting the key space (#703 review).
        episode_offset = 0
        incarnation = 0
        transcript_started = None
        if at is not None and at.transcript_writer is not None:
            episode_offset = at.episodes
            incarnation = at.transcript_writer.incarnation + 1
            transcript_started = at.transcript_writer.started
        await self._shutdown(at)
        (
            env_factory,
            policy_fn,
            max_steps,
            step_timeout,
            policy_factory,
            learn_hook,
            reward_hook,
            policy_timeout,
        ) = spec
        if policy_factory is not None:
            try:
                policy_fn = policy_factory(entry.checkpoint_path, hparams or {})
            except Exception:
                policy_fn = spec[1]
        started = await self.start_agent(
            agent_id,
            env_factory,
            policy_fn,
            max_steps=max_steps,
            step_timeout=step_timeout,
            policy_timeout=policy_timeout,
            policy_factory=policy_factory,
            learn_hook=learn_hook,
            reward_hook=reward_hook,
            episode_offset=episode_offset,
            transcript_incarnation=incarnation,
            transcript_started=transcript_started,
        )
        if started and self._mixer is not None:
            try:
                # Shutdown already dropped the id from floor lists, but
                # remove-then-add keeps this exact-once either way.
                self._mixer.remove_agent(agent_id)
                self._mixer.assign_one(agent_id)
            except Exception:
                pass
        return started

    async def reap(self):
        """Stop tasks whose registry entry is dead or gone (churn deaths).
        Reaped agents get a death metric (no error: ended by churn, not by
        crash). Returns the number of reaped tasks."""
        async with self._lock:
            dead = []
            for aid in self._tasks:
                entry = self.registry.get(aid)
                if entry is None or not entry.alive:
                    dead.append(aid)
            tasks = [self._tasks.pop(aid, None) for aid in dead]
            self._drop_specs(dead)
        for at in tasks:
            if at is not None:
                self._log_death(at.agent_id, at.episodes, at.total_reward)
            await self._shutdown(at)
        return len(dead)

    async def stop_all(self):
        async with self._lock:
            tasks = list(self._tasks.values())
            self._tasks.clear()
            self._drop_specs([at.agent_id for at in tasks])
        for at in tasks:
            await self._shutdown(at)

    def status(self):
        return {
            "running": len(self._tasks),
            "agents": {
                aid: {"episodes": at.episodes, "reward": round(at.total_reward, 2)}
                for aid, at in self._tasks.items()
            },
        }
