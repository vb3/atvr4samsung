"""Unit tests for the hold-repeat driver.

Stdlib only (``IsolatedAsyncioTestCase``): the repeater's timing is injected via a controllable
``sleep``/``clock`` so cadence, the safety cap, cancellation, and fail-closed behavior are
deterministic — no real time, no TV, no network.
"""
import asyncio
import unittest

from atvr4samsung.companion.repeater import HoldRepeater, HoldRepeatConfig


class _Controller:
    """A virtual clock + gated sleep.

    Each ``sleep(d)`` suspends on a fresh future recorded in ``pending``; the test releases the
    earliest one with :meth:`advance_one`, advancing the clock by that duration. This lets a test step
    the repeat loop one iteration at a time.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.t = start
        self.pending: list[tuple[float, asyncio.Future]] = []

    def clock(self) -> float:
        return self.t

    async def sleep(self, duration: float) -> None:
        fut = asyncio.get_running_loop().create_future()
        self.pending.append((duration, fut))
        await fut

    async def advance_one(self) -> None:
        """Release the earliest pending sleep (advance the clock by its duration) and let the woken
        coroutine run to its next suspension point."""
        duration, fut = self.pending.pop(0)
        self.t += duration
        if not fut.done():
            fut.set_result(None)
        await _settle()


async def _settle() -> None:
    for _ in range(4):
        await asyncio.sleep(0)


def _repeater(send, controller, **config):
    cfg = HoldRepeatConfig(**config) if config else HoldRepeatConfig()
    return HoldRepeater(
        send,
        config=cfg,
        loop=asyncio.get_event_loop(),
        sleep=controller.sleep,
        clock=controller.clock,
    )


class TestHoldRepeater(unittest.IsolatedAsyncioTestCase):
    async def test_no_repeat_before_initial_delay(self):
        sends = []

        async def send(key):
            sends.append(key)

        ctl = _Controller()
        rep = _repeater(send, ctl, initial_delay=0.35, interval=0.18, max_hold=10.0)
        rep.start("KEY_VOLUP")
        await _settle()

        # The task is parked on the initial-delay sleep; nothing sent yet (the server sends the
        # immediate first click, not the repeater).
        self.assertEqual(sends, [])
        self.assertEqual(len(ctl.pending), 1)
        self.assertAlmostEqual(ctl.pending[0][0], 0.35)
        self.assertTrue(rep.active)
        await rep.stop_all()

    async def test_cancel_during_initial_delay_yields_zero_repeats(self):
        sends = []

        async def send(key):
            sends.append(key)

        ctl = _Controller()
        rep = _repeater(send, ctl, initial_delay=0.35, interval=0.18, max_hold=10.0)
        rep.start("KEY_VOLUP")
        await _settle()
        rep.stop("KEY_VOLUP")
        await _settle()

        self.assertEqual(sends, [])
        self.assertFalse(rep.active)

    async def test_repeats_at_interval_until_stop(self):
        sends = []

        async def send(key):
            sends.append(key)

        ctl = _Controller()
        rep = _repeater(send, ctl, initial_delay=0.35, interval=0.18, max_hold=10.0)
        rep.start("KEY_VOLUP")
        await _settle()

        await ctl.advance_one()  # release initial delay -> first repeat send
        self.assertEqual(sends, ["KEY_VOLUP"])
        await ctl.advance_one()  # release interval -> second repeat
        self.assertEqual(sends, ["KEY_VOLUP", "KEY_VOLUP"])

        rep.stop("KEY_VOLUP")
        await _settle()
        # No further sends after stop.
        self.assertEqual(len(sends), 2)
        self.assertFalse(rep.active)

    async def test_safety_cap_stops_repeating(self):
        sends = []

        async def send(key):
            sends.append(key)

        ctl = _Controller(start=0.0)
        # deadline = 0 + 3.0; initial 0, interval 1.0 -> sends at t=0,1,2 then t=3 stops => 3 sends.
        rep = _repeater(send, ctl, initial_delay=0.0, interval=1.0, max_hold=3.0)
        rep.start("KEY_VOLDOWN")
        await _settle()

        for _ in range(10):
            if not rep.active:
                break
            await ctl.advance_one()

        self.assertEqual(sends, ["KEY_VOLDOWN", "KEY_VOLDOWN", "KEY_VOLDOWN"])
        self.assertFalse(rep.active)

    async def test_starting_other_direction_cancels_the_first(self):
        async def send(key):
            pass

        ctl = _Controller()
        rep = _repeater(send, ctl)
        rep.start("KEY_VOLUP")
        await _settle()
        rep.start("KEY_VOLDOWN")
        await _settle()

        self.assertNotIn("KEY_VOLUP", rep._tasks)
        self.assertIn("KEY_VOLDOWN", rep._tasks)
        self.assertTrue(rep.active)
        await rep.stop_all()

    async def test_restarting_same_direction_is_a_noop(self):
        async def send(key):
            pass

        ctl = _Controller()
        rep = _repeater(send, ctl)
        rep.start("KEY_VOLUP")
        await _settle()
        first_task = rep._tasks["KEY_VOLUP"]
        rep.start("KEY_VOLUP")
        await _settle()
        self.assertIs(rep._tasks["KEY_VOLUP"], first_task)
        await rep.stop_all()

    async def test_send_error_fails_closed(self):
        async def bad_send(key):
            raise RuntimeError("socket gone")

        ctl = _Controller()
        rep = _repeater(bad_send, ctl, initial_delay=0.35, interval=0.18, max_hold=10.0)
        rep.start("KEY_VOLUP")
        await _settle()
        await ctl.advance_one()  # release initial delay -> send raises -> loop ends, handle cleared

        self.assertFalse(rep.active)

    async def test_stop_all_cancels_and_awaits(self):
        async def send(key):
            pass

        ctl = _Controller()
        rep = _repeater(send, ctl)
        rep.start("KEY_VOLUP")
        await _settle()
        self.assertTrue(rep.active)

        await rep.stop_all()
        self.assertFalse(rep.active)

    async def test_stop_and_stop_all_are_safe_when_idle(self):
        async def send(key):
            pass

        ctl = _Controller()
        rep = _repeater(send, ctl)
        rep.stop("KEY_VOLUP")  # not held -> no error
        await rep.stop_all()   # nothing active -> no error
        self.assertFalse(rep.active)


class TestShouldContinue(unittest.IsolatedAsyncioTestCase):
    """The dead-man gate (used by the directional repeater keyed on touch-frame liveness)."""

    def _repeater(self, send, ctl, should_continue, **config):
        cfg = HoldRepeatConfig(**config) if config else HoldRepeatConfig()
        return HoldRepeater(
            send, config=cfg, loop=asyncio.get_event_loop(),
            sleep=ctl.sleep, clock=ctl.clock, should_continue=should_continue,
        )

    async def test_stops_before_first_send_when_not_alive(self):
        sends = []

        async def send(key):
            sends.append(key)

        ctl = _Controller()
        rep = self._repeater(send, ctl, should_continue=lambda: False,
                             initial_delay=0.25, interval=0.12, max_hold=15.0)
        rep.start("KEY_RIGHT")
        await _settle()
        await ctl.advance_one()  # release initial delay -> loop checks should_continue BEFORE sending

        self.assertEqual(sends, [], "a dead liveness signal must stop before the first repeat send")
        self.assertFalse(rep.active)

    async def test_stops_mid_run_when_liveness_ends(self):
        sends = []
        alive = {"v": True}

        async def send(key):
            sends.append(key)

        ctl = _Controller()
        rep = self._repeater(send, ctl, should_continue=lambda: alive["v"],
                             initial_delay=0.25, interval=0.12, max_hold=15.0)
        rep.start("KEY_RIGHT")
        await _settle()
        await ctl.advance_one()  # first repeat
        self.assertEqual(sends, ["KEY_RIGHT"])
        alive["v"] = False       # touch frames stopped arriving
        await ctl.advance_one()  # next iteration checks should_continue -> stop
        self.assertEqual(sends, ["KEY_RIGHT"], "no send after the liveness signal ended")
        self.assertFalse(rep.active)

    async def test_predicate_that_raises_fails_closed(self):
        sends = []

        def boom():
            raise RuntimeError("predicate blew up")

        async def send(key):
            sends.append(key)

        ctl = _Controller()
        rep = self._repeater(send, ctl, should_continue=boom,
                             initial_delay=0.25, interval=0.12, max_hold=15.0)
        rep.start("KEY_RIGHT")
        await _settle()
        await ctl.advance_one()  # loop polls the predicate, which raises -> fail closed, no send

        self.assertEqual(sends, [])
        self.assertFalse(rep.active)


if __name__ == "__main__":
    unittest.main()
