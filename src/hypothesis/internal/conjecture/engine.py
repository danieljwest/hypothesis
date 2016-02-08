# coding=utf-8
#
# This file is part of Hypothesis (https://github.com/DRMacIver/hypothesis)
#
# Most of this work is copyright (C) 2013-2015 David R. MacIver
# (david@drmaciver.com), but it contains contributions by others. See
# https://github.com/DRMacIver/hypothesis/blob/master/CONTRIBUTING.rst for a
# full list of people who may hold copyright, and consult the git log if you
# need to determine who owns an individual contribution.
#
# This Source Code Form is subject to the terms of the Mozilla Public License,
# v. 2.0. If a copy of the MPL was not distributed with this file, You can
# obtain one at http://mozilla.org/MPL/2.0/.
#
# END HEADER

from __future__ import division, print_function, absolute_import

import time
from random import Random, getrandbits

from hypothesis import settings as Settings
from hypothesis.reporting import debug_report
from hypothesis.internal.compat import Counter
from hypothesis.internal.conjecture.data import Status, StopTest, TestData
from hypothesis.internal.conjecture.minimizer import minimize


class RunIsComplete(Exception):
    pass


class TestRunner(object):

    def __init__(
        self, test_function, settings=None, random=None,
        database_key=None,
    ):
        self._test_function = test_function
        self.settings = settings or Settings()
        self.last_data = None
        self.changed = 0
        self.shrinks = 0
        self.failed_shrinks = 0
        self.examples_considered = 0
        self.iterations = 0
        self.valid_examples = 0
        self.start_time = time.time()
        self.random = random or Random(getrandbits(128))
        self.database_key = database_key

    def new_buffer(self):
        self.last_data = TestData(
            max_length=self.settings.buffer_size,
            draw_bytes=lambda data, n, distribution:
            distribution(self.random, n)
        )
        self.test_function(self.last_data)
        self.last_data.freeze()
        self.note_for_corpus(self.last_data)

    def test_function(self, data):
        self.iterations += 1
        try:
            self._test_function(data)
            data.freeze()
        except StopTest as e:
            if e.uuid != data.uuid:
                self.save_buffer(data.buffer)
                raise e
        except:
            self.save_buffer(data.buffer)
            raise
        if data.status >= Status.VALID:
            self.valid_examples += 1

    def consider_new_test_data(self, data):
        # Transition rules:
        #   1. Transition cannot decrease the status
        #   2. Any transition which increases the status is valid
        #   3. If the previous status was interesting, only shrinking
        #      transitions are allowed.
        if self.last_data.status < data.status:
            return True
        if self.last_data.status > data.status:
            return False
        if data.status == Status.INVALID:
            return data.index >= self.last_data.index
        if data.status == Status.OVERRUN:
            return data.index <= self.last_data.index
        if data.status == Status.INTERESTING:
            assert len(data.buffer) <= len(self.last_data.buffer)
            if len(data.buffer) == len(self.last_data.buffer):
                assert data.buffer < self.last_data.buffer
            return True
        return True

    def save_buffer(self, buffer):
        if (
            self.settings.database is not None and
            self.database_key is not None
        ):
            self.settings.database.save(
                self.database_key, buffer
            )

    def note_for_corpus(self, data):
        if data.status == Status.INTERESTING:
            self.save_buffer(data.buffer)

    def incorporate_new_buffer(self, buffer):
        assert self.last_data.status == Status.INTERESTING
        if (
            self.settings.timeout > 0 and
            time.time() >= self.start_time + self.settings.timeout
        ):
            raise RunIsComplete()
        self.examples_considered += 1
        if (
            len(buffer) >= len(self.last_data.buffer) and (
                buffer[:self.last_data.index] >=
                self.last_data.buffer[:self.last_data.index])
        ):
            return False
        data = TestData.for_buffer(buffer[:self.last_data.index])
        self.test_function(data)
        data.freeze()
        self.note_for_corpus(data)
        if data.status >= self.last_data.status:
            debug_report('%d bytes %r -> %r, %s' % (
                data.index,
                list(data.buffer[:data.index]), data.status,
                data.output,
            ))
        if self.consider_new_test_data(data):
            self.shrinks += 1
            self.last_data = data
            if self.shrinks >= self.settings.max_shrinks:
                raise RunIsComplete()
            self.last_data = data
            self.changed += 1
            return True
        else:
            if data.status >= Status.VALID:
                self.failed_shrinks += 1
                if self.failed_shrinks >= 10 * self.settings.max_shrinks:
                    raise RunIsComplete()
        return False

    def run(self):
        with self.settings:
            try:
                self._run()
            except RunIsComplete:
                pass
            debug_report(
                'Run complete after %d examples (%d valid) and %d shrinks' % (
                    self.iterations, self.valid_examples, self.shrinks,
                ))

    def _new_mutator(self):
        def draw_new(data, n, distribution):
            return distribution(self.random, n)

        def draw_existing(data, n, distribution):
            return self.last_data.buffer[data.index:data.index + n]

        def draw_smaller(data, n, distribution):
            existing = self.last_data.buffer[data.index:data.index + n]
            r = distribution(self.random, n)
            if r <= existing:
                return r
            return _draw_predecessor(self.random, existing)

        def draw_larger(data, n, distribution):
            existing = self.last_data.buffer[data.index:data.index + n]
            r = distribution(self.random, n)
            if r >= existing:
                return r
            return _draw_successor(self.random, existing)

        def reuse_existing(data, n, distribution):
            choices = data.block_starts.get(n, []) or \
                self.last_data.block_starts.get(n, [])
            if choices:
                i = self.random.choice(choices)
                return self.last_data.buffer[i:i + n]
            else:
                return distribution(self.random, n)

        def flip_bit(data, n, distribution):
            buf = bytearray(
                self.last_data.buffer[data.index:data.index + n])
            i = self.random.randint(0, n - 1)
            k = self.random.randint(0, 7)
            buf[i] ^= (1 << k)
            return bytes(buf)

        def draw_zero(data, n, distribution):
            return bytes(n)

        def draw_constant(data, n, distribution):
            return bytes([
                self.random.randint(0, 255)
            ] * n)

        options = [
            draw_new,
            reuse_existing, reuse_existing,
            draw_existing, draw_smaller, draw_larger,
            flip_bit, draw_zero, draw_constant,
        ]

        bits = [
            self.random.choice(options) for _ in range(3)
        ]

        def draw_mutated(data, n, distribution):
            if (
                data.index + n > len(self.last_data.buffer)
            ):
                return distribution(self.random, n)
            return self.random.choice(bits)(data, n, distribution)
        return draw_mutated

    def _run(self):
        self.last_data = None
        mutations = 0
        start_time = time.time()

        if (
            self.settings.database is not None and
            self.database_key is not None
        ):
            corpus = sorted(
                self.settings.database.fetch(self.database_key),
                key=lambda d: (len(d), d)
            )
            for existing in corpus:
                if self.valid_examples >= self.settings.max_examples:
                    return
                if self.iterations >= max(
                    self.settings.max_iterations, self.settings.max_examples
                ):
                    return
                data = TestData.for_buffer(existing)
                self.test_function(data)
                data.freeze()
                self.last_data = data
                if data.status < Status.VALID:
                    self.settings.database.delete(
                        self.database_key, existing)
                elif data.status == Status.VALID:
                    # Incremental garbage collection! we store a lot of
                    # examples in the DB as we shrink: Those that stay
                    # interesting get kept, those that become invalid get
                    # dropped, but those that are merely valid gradually go
                    # away over time.
                    if self.random.randint(0, 2) == 0:
                        self.settings.database.delete(
                            self.database_key, existing)
                else:
                    assert data.status == Status.INTERESTING
                    self.last_data = data
                    break

        if (
            self.last_data is None or
            self.last_data.status < Status.INTERESTING
        ):
            self.new_buffer()
        mutator = self._new_mutator()
        while self.last_data.status != Status.INTERESTING:
            if self.valid_examples >= self.settings.max_examples:
                return
            if self.iterations >= max(
                self.settings.max_iterations, self.settings.max_examples
            ):
                return
            if (
                self.settings.timeout > 0 and
                time.time() >= start_time + self.settings.timeout
            ):
                return
            if mutations >= self.settings.max_mutations:
                mutations = 0
                self.new_buffer()
                mutator = self._new_mutator()
            else:
                data = TestData(
                    draw_bytes=mutator,
                    max_length=self.settings.buffer_size
                )
                self.test_function(data)
                data.freeze()
                self.note_for_corpus(data)
                prev_data = self.last_data
                if self.consider_new_test_data(data):
                    self.last_data = data
                    if data.status > prev_data.status:
                        mutations = 0
                else:
                    mutator = self._new_mutator()

            mutations += 1

        if self.settings.max_shrinks <= 0:
            return

        if not self.last_data.buffer:
            return

        data = TestData.for_buffer(self.last_data.buffer)
        self.test_function(data)
        if data.status != Status.INTERESTING:
            return

        change_counter = -1

        while self.changed > change_counter:
            change_counter = self.changed
            i = 0
            while i < len(self.last_data.intervals):
                u, v = self.last_data.intervals[i]
                if not self.incorporate_new_buffer(
                    self.last_data.buffer[:u] +
                    self.last_data.buffer[v:]
                ):
                    i += 1
            i = 0
            while i < len(self.last_data.blocks):
                u, v = self.last_data.blocks[i]
                buf = self.last_data.buffer
                block = buf[u:v]
                n = v - u
                all_blocks = sorted(set([bytes(n)] + [
                    buf[a:a + n]
                    for a in self.last_data.block_starts[n]
                ]))
                better_blocks = all_blocks[:all_blocks.index(block)]
                for b in better_blocks:
                    if self.incorporate_new_buffer(
                        buf[:u] + b + buf[v:]
                    ):
                        break
                i += 1

            block_counter = -1
            while block_counter < self.changed:
                block_counter = self.changed
                blocks = [
                    k for k, v in
                    Counter(
                        self.last_data.buffer[u:v]
                        for u, v in self.last_data.blocks).items()
                    if v > 1
                ]
                for block in blocks:
                    parts = self.last_data.buffer.split(block)
                    assert self.last_data.buffer == block.join(parts)
                    minimize(
                        block,
                        lambda b: self.incorporate_new_buffer(
                            b.join(parts)),
                        self.random
                    )

            i = 0
            while i < len(self.last_data.blocks):
                u, v = self.last_data.blocks[i]
                minimize(
                    self.last_data.buffer[u:v],
                    lambda b: self.incorporate_new_buffer(
                        self.last_data.buffer[:u] + b +
                        self.last_data.buffer[v:],
                    ), self.random
                )
                i += 1

            i = 0
            alternatives = None
            while i < len(self.last_data.intervals):
                if alternatives is None:
                    alternatives = sorted(set(
                        self.last_data.buffer[u:v]
                        for u, v in self.last_data.intervals), key=len)
                u, v = self.last_data.intervals[i]
                for a in alternatives:
                    if len(a) < v - u:
                        if self.incorporate_new_buffer(
                            self.last_data.buffer[:u] + a +
                            self.last_data.buffer[v:]
                        ):
                            alternatives = None
                            break
                i += 1


def _draw_predecessor(rnd, xs):
    r = bytearray()
    any_strict = False
    for x in xs:
        if not any_strict:
            c = rnd.randint(0, x)
            if c < x:
                any_strict = True
        else:
            c = rnd.randint(0, 255)
        r.append(c)
    return bytes(r)


def _draw_successor(rnd, xs):
    r = bytearray()
    any_strict = False
    for x in xs:
        if not any_strict:
            c = rnd.randint(x, 255)
            if c > x:
                any_strict = True
        else:
            c = rnd.randint(0, 255)
        r.append(c)
    return bytes(r)
