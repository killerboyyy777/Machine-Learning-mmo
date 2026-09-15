"""Quest grind-decay proof (no live server needed).

Run from the repo root:  python tests/test_quest_grind.py
Confirms that repeat-looping the quest turn-in decays as intended instead of
becoming the optimal grind:
  1. Pure repetition earns strictly less per action over time (variety decay
     kicks in once history fills, and the difficulty curve bites as score
     grows -- first gain > last gain).
  2. A varied action sequence earns more in total than the same number of
     identical repeats at equal base points (variety rewards novelty).
  3. Even varied gains diminish marginally as total score grows (first-5
     average > last-5 average), so no fixed strategy pays constantly forever.

Uses the same record_action + award_points sequence handle_connection runs
per command, with a fresh character so history starts empty.
"""
import asyncio
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as srv


async def _run_sequence(name, signatures, base=10.0):
    gained = []
    for sig in signatures:
        srv.record_action(name, sig)
        g = await srv.award_points_to_name(name, base, "grind-test")
        gained.append(g)
    return gained


async def main():
    random.seed(7)
    name_repeat = f"GrindRepeat{random.randint(1000, 9999)}"
    name_varied = f"GrindVaried{random.randint(1000, 9999)}"

    # 1. Pure quest_turn_in loop decays.
    repeat_sigs = [("quest_turn_in", "turn_in")] * 25
    repeat_gains = await _run_sequence(name_repeat, repeat_sigs)
    assert repeat_gains[0] > repeat_gains[-1], (repeat_gains[0], repeat_gains[-1])
    print(f"REPEAT_DECAY_OK first={repeat_gains[0]:.3f} last={repeat_gains[-1]:.3f}")

    # 2. Varied earns more than pure repeat at equal base points.
    varied_pool = [
        ("quest_accept", "accept"), ("quest_turn_in", "turn_in"),
        ("craft", "ancient_guardian_charm"), ("move", "north"),
        ("attack", "rat"), ("take", "sword"),
    ]
    varied_sigs = [varied_pool[i % len(varied_pool)] for i in range(25)]
    varied_gains = await _run_sequence(name_varied, varied_sigs)
    assert sum(varied_gains) > sum(repeat_gains), (sum(varied_gains), sum(repeat_gains))
    print(f"VARIETY_WINS_OK varied_total={sum(varied_gains):.2f} repeat_total={sum(repeat_gains):.2f}")

    # 3. Even varied gains diminish marginally as score grows.
    first5 = sum(varied_gains[:5]) / 5
    last5 = sum(varied_gains[-5:]) / 5
    assert first5 > last5, (first5, last5)
    print(f"DIMINISH_OK first5avg={first5:.3f} last5avg={last5:.3f}")

    print("ALL_GRIND_OK")

asyncio.run(main())
