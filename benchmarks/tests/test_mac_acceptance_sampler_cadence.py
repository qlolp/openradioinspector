"""The sampling period includes collection time; late polls remain visible."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from benchmarks.ori_bench import metrics


class Clock:
    def __init__(self): self.value = 0.0
    def now(self): return self.value


class InlineThread:
    def __init__(self, *, target, **kwargs): self.target = target
    def start(self): self.target()
    def join(self): pass


class ResourceSamplerCadence(unittest.TestCase):
    def poll_starts(self, duration, costs):
        clock = Clock()
        starts = []

        class Stop:
            def __init__(self): self.done = False
            def wait(self, seconds):
                if seconds < 0: raise AssertionError('Negative wait')
                clock.value += seconds
                if clock.value >= duration: self.done = True
                return self.done
            def set(self): self.done = True

        with patch.object(metrics.time, 'monotonic', clock.now), \
             patch.object(metrics.threading, 'Event', Stop), \
             patch.object(metrics.threading, 'Thread', InlineThread):
            sampler = metrics.Sampler(SimpleNamespace(pids={}, sample_interval=.5))
            def sample():
                starts.append(round(clock.value, 6))
                clock.value += costs[min(len(starts)-1, len(costs)-1)]
            sampler.sample = sample
            with sampler:
                # Exclude only __exit__'s separate final collection, as before.
                result = list(starts)
        return result

    def test_nonzero_collection_cost_does_not_reduce_sampling_frequency(self):
        self.assertEqual(self.poll_starts(3, [.3]), [0, .5, 1, 1.5, 2, 2.5])

    def test_late_poll_skips_missed_ticks_without_fabricating_catchup_samples(self):
        starts = self.poll_starts(5, [.1, 2.1, .1])
        self.assertEqual(starts, [0, .5, 3, 3.5, 4, 4.5])
        self.assertGreater(starts[2]-starts[1], 2)

    def test_stopped_sampler_does_not_start_another_scheduled_poll(self):
        self.assertEqual(self.poll_starts(.5, [.1]), [0])


if __name__ == '__main__': unittest.main()
