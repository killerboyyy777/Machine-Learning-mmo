"""Action-level transcripts (#624, v0.17 phase 1).

Run from the repo root:  python tests/test_transcript.py

A runlog records identity and scalar samples but throws away every per-step
detail, so an exploit report cannot ship the trajectory that produced it.
These cases pin the JSONL writer/reader and the additive hook in AgentTask:
transcripts capture the steps without changing the episode event or the
reward/step accounting the supervisor already relies on.
"""

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml.conductor.supervisor import AgentTask, Supervisor
from ml.transcript import TranscriptWriter, read_transcript


def test_writer_reader_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "deep", "agent.jsonl")
        writer = TranscriptWriter(path)
        writer.record(agent_id="a1", episode=1, step=0, action=3, reward=1.5)
        writer.record(agent_id="a1", episode=1, step=1, action=4, reward=2.0)
        rows = read_transcript(path)
        assert len(rows) == 2, rows
        assert rows[0]["agent_id"] == "a1"
        assert rows[0]["step"] == 0
        assert rows[1]["step"] == 1
        assert rows[1]["reward"] == 2.0
        assert "elapsed_s" in rows[0]
        assert rows[1]["elapsed_s"] >= rows[0]["elapsed_s"]
        # Every line is valid JSON on its own.
        with open(path, encoding="utf-8") as fh:
            assert all(json.loads(line) for line in fh)
    print("TRANSCRIPT_ROUND_TRIP_OK")


def test_writer_never_raises_and_reader_empty():
    assert (
        read_transcript(os.path.join(tempfile.gettempdir(), "no-such-transcript.jsonl"))
        == []
    )
    # An unwritable path must not raise out of record().
    writer = TranscriptWriter(os.path.join(tempfile.gettempdir(), "\x00bad", "x.jsonl"))
    writer.record(agent_id="x", step=0)
    print("TRANSCRIPT_NEVER_RAISES_OK")


class FakeRegistry:
    def __init__(self):
        self.episodes = []

    def get(self, agent_id):
        return None

    def record_episode(self, agent_id, reward):
        self.episodes.append((agent_id, reward))


class FakeEnv:
    """Two steps per episode; suspends so it cannot starve the loop."""

    def __init__(self):
        self.steps = 0

    async def reset(self):
        await asyncio.sleep(0)
        self.steps = 0
        return {"room": "town"}

    async def step(self, action):
        await asyncio.sleep(0)
        self.steps += 1
        return (
            {"room": "town", "step": self.steps},
            1.0,
            self.steps >= 2,
            {
                "reward_vector": {"score": 1.0},
                "reward_mode": "score",
            },
        )

    def valid_action_mask(self):
        return [True, True]

    async def close(self):
        pass


def test_agent_task_records_steps_without_changing_episode_event():
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "transcript.jsonl")
            writer = TranscriptWriter(path)
            env = FakeEnv()
            # A 2-arg policy (obs, agent_id) so arity detection takes the simple path.
            task = AgentTask(
                "a1", env, lambda obs, aid: 0, max_steps=10, transcript_writer=writer
            )
            events = []
            count = 0
            async for event in task.run():
                events.append(event)
                count += 1
                if count >= 2:
                    task.done.set()
                    break
            assert len(events) == 2
            first = events[0]
            assert set(first) == {"agent_id", "episode", "reward", "steps"}, first
            assert first["agent_id"] == "a1"
            assert first["reward"] == 2.0
            rows = read_transcript(path)
            # Two episodes, two steps each = four recorded steps.
            assert len(rows) == 4, rows
            steps = sorted(row["step"] for row in rows)
            assert steps == [0, 0, 1, 1], steps
            assert all(row["action"] == 0 for row in rows)
            assert all(row["reward"] == 1.0 for row in rows)
            assert all(row["info"]["reward_mode"] == "score" for row in rows)
            assert any(row["done"] for row in rows)

    asyncio.run(run())
    print("TRANSCRIPT_AGENT_HOOK_OK")


def test_supervisor_builds_writer_from_transcript_dir():
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            sup = Supervisor(FakeRegistry(), transcript_dir=tmp)
            assert sup._transcript_dir == tmp
            env = FakeEnv()
            ok = await sup.start_agent(
                "agent-7", lambda aid: env, lambda obs, aid: 0, max_steps=10
            )
            assert ok is True
            at = sup._tasks["agent-7"]
            assert isinstance(
                at.transcript_writer, TranscriptWriter
            ), at.transcript_writer
            assert at.transcript_writer.path.endswith("agent-7.jsonl")
            await sup.stop_agent("agent-7")

    asyncio.run(run())
    print("TRANSCRIPT_SUPERVISOR_WIRING_OK")


def test_transcript_is_off_by_default():
    async def run():
        sup = Supervisor(FakeRegistry())
        assert sup._transcript_dir is None
        env = FakeEnv()
        await sup.start_agent(
            "plain", lambda aid: env, lambda obs, aid: 0, max_steps=10
        )
        assert sup._tasks["plain"].transcript_writer is None
        await sup.stop_agent("plain")

    asyncio.run(run())
    print("TRANSCRIPT_DEFAULT_OFF_OK")


if __name__ == "__main__":
    test_writer_reader_round_trip()
    test_writer_never_raises_and_reader_empty()
    test_agent_task_records_steps_without_changing_episode_event()
    test_supervisor_builds_writer_from_transcript_dir()
    test_transcript_is_off_by_default()
    print("ALL_TRANSCRIPT_OK")
