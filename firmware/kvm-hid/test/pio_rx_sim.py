#!/usr/bin/env python3
"""Cycle-level simulation of Pico-PIO-USB's full-speed receiver.

Two PIO state machines share the D+/D- inputs (both read inverted):
  edge detector  96 MHz (clk_sys / 2.5)  jmp_pin = D-, in_pin = D+
  NRZI decoder   clk_sys (240 MHz)       jmp_pin = in_pin = D+, autopush 8, shift right
The detector raises DECODER_TRIGGER once per bit; the decoder turns each
trigger into one bit (or drops a stuffed bit) and pushes bytes to its FIFO.

Only the instructions these programs use are implemented. The line is
generated bit by bit (NRZI, bit stuffing, EOP, inter-packet gap) with a
host clock that may run off-nominal and at any phase.

    python3 pio_rx_sim.py            # run the checks, print a summary
"""
import random
import sys

SYS_HZ = 240_000_000
BIT_CYCLES = SYS_HZ / 12_000_000      # 20 clk_sys per full-speed bit
SYNC_DELAY = 2                        # GPIO input synchronizer (clk_sys cycles)

IRQ_BS_ERR, IRQ_EOP, IRQ_START, TRIG = 1, 2, 3, 4

# ---------------------------------------------------------------------------
# a tiny PIO model


class SM:
    def __init__(self, name, prog, wrap_target, wrap_top, jmp_pin, in_pin,
                 shift_right, autopush, step_cycles):
        self.name, self.prog = name, prog
        self.wrap_target, self.wrap_top = wrap_target, wrap_top
        self.jmp_pin, self.in_pin = jmp_pin, in_pin
        self.shift_right, self.autopush = shift_right, autopush
        self.steps = step_cycles      # function(cycle) -> bool (clock enable)
        self.reset()

    def reset(self):
        self.pc, self.x, self.y = 0, 0, 0
        self.isr, self.isr_count = 0, 0
        self.osr = 0xFFFFFFFF
        self.delay = 0
        self.fifo = []

    def restart(self):
        """pio_sm_restart: clears ISR/shift counters and delay, keeps PC."""
        self.isr, self.isr_count, self.delay = 0, 0, 0

    def _in(self, bits, n):
        if self.shift_right:
            self.isr = ((self.isr >> n) | (bits << (32 - n))) & 0xFFFFFFFF
        else:
            self.isr = ((self.isr << n) | bits) & 0xFFFFFFFF
        self.isr_count += n
        if self.autopush and self.isr_count >= 8:
            if getattr(self, "word_fifo", False):
                self.fifo.append(self.isr)          # whole 32-bit word, byte in 31:24
            else:
                self.fifo.append(self.isr >> 24 if self.shift_right else self.isr & 0xFF)
            self.isr, self.isr_count = 0, 0

    def step(self, pins, irq):
        """One enabled clock. pins: dict pin->value (already inverted)."""
        if self.delay:
            self.delay -= 1
            return
        op = self.prog[self.pc]
        kind, delay = op[0], op[-1]
        nxt = self.pc + 1 if self.pc != self.wrap_top else self.wrap_target
        stall = False
        if kind == "jmp":
            cond, target = op[1], op[2]
            take = {
                "always": True,
                "!x": self.x == 0,
                "!y": self.y == 0,
                "x!=y": self.x != self.y,
                "pin": pins[self.jmp_pin] == 1,
            }.get(cond)
            if cond == "x--":
                take = self.x != 0
                self.x = (self.x - 1) & 0xFFFFFFFF
            elif cond == "y--":
                take = self.y != 0
                self.y = (self.y - 1) & 0xFFFFFFFF
            if take:
                nxt = target
        elif kind == "wait_irq":
            if irq[op[1]]:
                irq[op[1]] = 0
            else:
                stall = True
        elif kind == "irq":                       # set, no wait
            irq[op[1]] = 1
        elif kind == "irq_wait":                  # set, then wait until cleared
            if getattr(self, "_irq_waiting", False):
                if irq[op[1]]:
                    stall = True
                else:
                    self._irq_waiting = False
            else:
                irq[op[1]] = 1
                self._irq_waiting = True
                stall = True
        elif kind == "irq_clear":
            irq[op[1]] = 0
        elif kind == "in":
            src = op[1]
            bit = {"pins": pins[self.in_pin], "null": 0, "osr": self.osr & 1}[src]
            self._in(bit, 1)
        elif kind == "mov":
            dst, src = op[1], op[2]
            pinval = pins[self.in_pin]
            if getattr(self, "in_count", 2) == 2:     # IN_COUNT = 2: D+ and D-
                pinval |= pins[self.in_pin + 1] << 1
            val = {"isr": self.isr, "null": 0, "x": self.x, "y": self.y,
                   "~x": (~self.x) & 0xFFFFFFFF, "~null": 0xFFFFFFFF, "pins": pinval}[src]
            if dst == "isr":
                self.isr, self.isr_count = val, 0
            elif dst == "x":
                self.x = val
            elif dst == "y":
                self.y = val
        elif kind == "set":
            setattr(self, op[1], op[2])
        elif kind == "push":                     # push noblock
            self.fifo.append(self.isr)
            self.isr, self.isr_count = 0, 0
        else:
            raise ValueError(kind)
        if stall:
            return
        self.pc = nxt
        self.delay = delay


# ---------------------------------------------------------------------------
# programs. Each op: (kind, args..., delay)

def edge_detector_orig():
    p = [
        ("irq_wait", IRQ_EOP, 0),          # 0 eop
        ("jmp", "pin", 1, 0),              # 1 start: wait for K (D- high)
        ("irq", IRQ_START, 1),             # 2
        ("irq", TRIG, 1),                  # 3 pin_still_low  (wrap target)
        ("jmp", "pin", 12, 0),             # 4 pin_low
        ("jmp", "pin", 12, 0),             # 5
        ("jmp", "pin", 12, 0),             # 6 pin_went_low
        ("jmp", "pin", 12, 0),             # 7
        ("jmp", "pin", 12, 0),             # 8
        ("jmp", "pin", 12, 0),             # 9   (wrap top)
        ("mov", "x", "isr", 1),            # 10 pin_still_high
        ("jmp", "x--", 0, 0),              # 11 -> eop if SE0
        ("mov", "isr", "null", 1),         # 12 pin_went_high
        ("irq", TRIG, 0),                  # 13
        ("in", "pins", 0),                 # 14 capture D+ (inverted) for the EOP check
        ("jmp", "pin", 10, 0),             # 15
        ("jmp", "always", 6, 0),           # 16 -> pin_went_low
    ]
    return p, 3, 9


def single_sm_receiver():
    """Edge detection and NRZI decoding in one state machine at 96 MHz
    (8 cycles per bit on every path). The line level lives in the program
    counter (K loop / J loop): staying is a 1, switching is a 0. Every
    packet enters through "start" (idle J -> K), so level, stuff counter (y)
    and ISR (cleared at EOP) start fresh: nothing carries over and the CPU
    has no deadline. jmp_pin = D-, in_base = D+ with IN_COUNT = 1 (inverted
    inputs: D- reads 1 for J/SE0, D+ reads 1 for K/SE0). Shift right,
    autopush 8, OSR = all ones. Exactly 32 instructions.

    Timing: every timing reference is a detected edge, which lags the wire
    by D = synchronizer + rise time + clock-divider jitter (1 to ~3.2
    detector cycles). The K loop watches for the next rising edge with 6
    polls covering -2..+3 cycles around its expected time, so an edge seen
    with a different D than the reference still lands inside. Line states
    are read 3+ cycles after the reference edge."""
    p = [
        ("mov", "y", "y", 1),              # 0  rise: let the edge settle
        ("mov", "x", "pins", 0),           # 1  D+: 0 = J, 1 = SE0
        ("jmp", "!x", 4, 0),               # 2
        ("jmp", "always", 27, 0),          # 3  SE0 -> eop
        ("jmp", "!y", 8, 0),               # 4  J_tr: stuffed bit?
        ("in", "null", 0),                 # 5  J_zero: 0
        ("set", "y", 6, 2),                # 6  J_c1
        ("jmp", "always", 12, 0),          # 7  -> J_chk
        ("jmp", "always", 6, 0),           # 8  J_st: no bit -> J_c1
        ("in", "osr", 0),                  # 9  J_one: 1
        ("jmp", "y--", 11, 0),             # 10
        ("jmp", "!y", 23, 2),              # 11 six ones: next must be a stuffed transition
        ("jmp", "pin", 25, 0),             # 12 J_chk: still J or SE0?
        ("in", "null", 0),                 # 13 K_zero: went K: 0
        ("set", "y", 6, 0),                # 14 K_c1
        ("jmp", "pin", 0, 0),              # 15 K_poll (wrap target): rising edge?
        ("jmp", "pin", 0, 0),              # 16
        ("jmp", "pin", 0, 0),              # 17
        ("jmp", "pin", 0, 0),              # 18
        ("jmp", "pin", 0, 0),              # 19
        ("jmp", "pin", 0, 0),              # 20
        ("in", "osr", 0),                  # 21 K_one: still K: 1
        ("jmp", "y--", 15, 0),             # 22 (wrap top)
        ("jmp", "pin", 25, 0),             # 23 J_chk_st: J again = stuffing error, go on
        ("jmp", "always", 14, 0),          # 24 K: stuffed transition, no bit -> K_c1
        ("mov", "x", "pins", 0),           # 25 J_smp: D+
        ("jmp", "!x", 9, 0),               # 26 J -> J_one
        ("mov", "isr", "~null", 0),        # 27 eop (SE0): drop a partial byte, and
        ("push", 0),                       # 28 push 0xffffffff as the end marker
        ("jmp", "pin", 29, 0),             # 29 start: idle J/SE0 until K
        ("irq", IRQ_START, 1),             # 30
        ("jmp", "always", 13, 0),          # 31 first SYNC bit: a transition -> K_zero
    ]
    return p, 15, 22


RX_START_PC = 29


def nrzi_decoder():
    p = [
        ("set", "y", 6, 0),                # 0 set_y (wrap target)
        ("wait_irq", TRIG, 0),             # 1 irq_wait
        ("jmp", "!y", 6, 0),               # 2 stuffed bit -> flip
        ("jmp", "pin", 7, 0),              # 3 -> pin_high (K)
        ("jmp", "!x", 8, 0),               # 4 pin_low (J): no transition -> K1
        ("in", "null", 0),                 # 5 J1/K2: transition -> 0
        ("mov", "x", "~x", 0),             # 6 flip  (wrap top)
        ("jmp", "!x", 5, 0),               # 7 pin_high: transition -> J1
        ("in", "osr", 0),                  # 8 K1/J2: no transition -> 1
        ("jmp", "y--", 1, 0),              # 9
    ]
    return p, 0, 6


# ---------------------------------------------------------------------------
# line generator

J, K, SE0 = "J", "K", "SE0"
PIN = {  # inverted inputs: D+ pin 0, D- pin 1
    J: {0: 0, 1: 1},
    K: {0: 1, 1: 0},
    SE0: {0: 1, 1: 1},
}
PID_IN, PID_OUT, PID_SOF, PID_SETUP = 0x69, 0xE1, 0xA5, 0x2D
PID_DATA0, PID_DATA1, PID_ACK, PID_NAK = 0xC3, 0x4B, 0xD2, 0x5A


def packet_bits(data):
    """SYNC + data bytes, LSB first, with bit stuffing -> list of 0/1."""
    bits, ones = [], 0
    for byte in [0x80] + list(data):
        for i in range(8):
            b = (byte >> i) & 1
            bits.append(b)
            if b:
                ones += 1
                if ones == 6:
                    bits.append(0)       # stuffed bit
                    ones = 0
            else:
                ones = 0
    return bits


def line_states(packets, gaps):
    """-> list of (state, duration_in_bits). Idle J before the first packet."""
    out = [(J, 6)]
    level = J
    for data, gap in zip(packets, gaps):
        for b in packet_bits(data):
            if b == 0:
                level = K if level == J else J
            out.append((level, 1))
        out.append((SE0, 2))            # EOP
        out.append((J, 1 + gap))       # EOP's J + inter-packet idle
        level = J
    return out


def random_packet(rng):
    kind = rng.random()
    if kind < 0.45:                    # token: PID + 2 bytes
        pid = rng.choice([PID_IN, PID_OUT, PID_SOF, PID_SETUP])
        return [pid, rng.randrange(256), rng.randrange(256)]
    if kind < 0.65:                    # handshake
        return [rng.choice([PID_ACK, PID_NAK])]
    n = rng.choice([0, 1, 3, 8, 16, 64, rng.randrange(65)])
    body = [rng.randrange(256) for _ in range(n)]
    if rng.random() < 0.3:             # long runs of ones exercise bit stuffing
        body = [0xFF] * n
    return [rng.choice([PID_DATA0, PID_DATA1])] + body + [rng.randrange(256), rng.randrange(256)]


# ---------------------------------------------------------------------------
# simulation

def simulate(edge_prog, packets, gaps, rng, drift_ppm=0, cpu=None):
    """cpu: None (free running), or a callable(event, sim) modelling the
    original firmware's per-packet re-arm. Returns decoded FIFO bytes and
    a list of (cycle, kind) events."""
    ep, ewt, ewtop = edge_prog
    dp, dwt, dwtop = nrzi_decoder()
    # edge detector at clk_sys/2.5: enable pattern 2,3,2,3,...
    edge = SM("edge", ep, ewt, ewtop, jmp_pin=1, in_pin=0, shift_right=False,
              autopush=False, step_cycles=None)
    edge.pc = 1                       # firmware clears the EOP flag first: start at "start"
    dec = SM("dec", dp, dwt, dwtop, jmp_pin=0, in_pin=0, shift_right=True,
             autopush=True, step_cycles=None)
    irq = [0] * 8
    states = line_states(packets, gaps)
    bit_cycles = BIT_CYCLES * (1 + drift_ppm * 1e-6)
    # expand to a per-cycle pin level
    phase = rng.random() * bit_cycles
    edges, t = [], phase
    for st, nbits in states:
        edges.append((t, st))
        t += nbits * bit_cycles
    total = int(t) + 200
    level_at = []
    i, cur = 0, J
    for c in range(total):
        while i < len(edges) and edges[i][0] <= c:
            cur = edges[i][1]
            i += 1
        level_at.append(cur)
    next_edge_step = 0.0
    events = []
    eop_seen = 0
    for c in range(total):
        pins = PIN[level_at[max(0, c - SYNC_DELAY)]]
        if c >= next_edge_step:
            edge.step(pins, irq)
            next_edge_step += 2.5
        dec.step(pins, irq)
        if irq[IRQ_EOP] and not eop_seen:
            eop_seen = 1
            events.append((c, "eop"))
            if cpu:
                cpu("eop", c, edge, dec, irq)
        if not irq[IRQ_EOP]:
            eop_seen = 0
        if cpu:
            cpu("tick", c, edge, dec, irq)
    return dec.fifo, events


PHYS = {J: (1, 0), K: (0, 1), SE0: (0, 0)}     # (D+, D-) on the wire


def line_pins(states, bit_cycles, phase, slew_cycles, rng):
    """Per clk_sys cycle (D+ inverted, D- inverted). Each wire switches at
    its nominal time plus its own random 0..slew delay (FS rise/fall times
    are up to 20 ns, and D+/D- need not cross together)."""
    events = {0: [], 1: []}
    t, prev = phase, PHYS[J]
    for st, nbits in states:
        now = PHYS[st]
        for w in (0, 1):
            if now[w] != prev[w]:
                events[w].append((t + rng.random() * slew_cycles, now[w]))
        prev = now
        t += nbits * bit_cycles
    total = int(t) + 200
    out = []
    idx, val = {0: 0, 1: 0}, {0: 1, 1: 0}
    for w in (0, 1):
        events[w].sort()
    for c in range(total):
        for w in (0, 1):
            ev = events[w]
            while idx[w] < len(ev) and ev[idx[w]][0] <= c:
                val[w] = ev[idx[w]][1]
                idx[w] += 1
        out.append({0: 1 - val[0], 1: 1 - val[1]})
    return out


def simulate_single(packets, gaps, rng, drift_ppm=0, prog=None, slew_ns=20):
    """single_sm_receiver() alone, no CPU at all -> decoded FIFO bytes and
    the cycles at which it raised EOP."""
    sp, wt, wtop = prog or single_sm_receiver()
    sm = SM("rx", sp, wt, wtop, jmp_pin=1, in_pin=0, shift_right=True,
            autopush=True, step_cycles=None)
    sm.in_count = 1
    sm.word_fifo = prog is None
    sm.pc = RX_START_PC if prog is None else 2   # "start"
    irq = [0] * 8
    bit_cycles = BIT_CYCLES * (1 + drift_ppm * 1e-6)
    pins = line_pins(line_states(packets, gaps), bit_cycles, rng.random() * bit_cycles,
                     slew_ns * SYS_HZ / 1e9, rng)
    nxt, eops = 0.0, []
    for c in range(len(pins)):
        if c >= nxt:
            sm.step(pins[max(0, c - SYNC_DELAY)], irq)
            nxt += 2.5
            if irq[IRQ_EOP]:
                eops.append(c)
                irq[IRQ_EOP] = 0
    if not sm.word_fifo:
        return sm.fifo, eops
    data, marks = [], 0
    for w in sm.fifo:
        if w == 0xFFFFFFFF:
            marks += 1
            data.append("EOP")
        elif w & 0xFFFFFF:
            data.append("BAD %08x" % w)
        else:
            data.append(w >> 24)
    return data, [None] * marks


def expected_stream(packets, markers=False):
    out = []
    for p in packets:
        out += [0x80] + list(p) + (["EOP"] if markers else [])
    return out


class OrigCpu:
    """The original firmware: after each EOP, latency L cycles later, drop
    the trigger flag, put the decoder back to its start and release the
    detector (rearm_receiver_at_eop)."""

    def __init__(self, latency):
        self.latency, self.due = latency, None
        self.packets = []

    def __call__(self, ev, c, edge, dec, irq):
        if ev == "eop":
            self.due = c + self.latency
        elif ev == "tick" and self.due is not None and c >= self.due:
            self.due = None
            self.packets.append(list(dec.fifo))
            dec.fifo.clear()
            irq[TRIG] = 0
            dec.pc, dec.x = 0, 0
            dec.restart()
            irq[IRQ_EOP] = irq[IRQ_START] = irq[IRQ_BS_ERR] = 0


def check_original(latency_cycles, gap_bits, trials=200, seed=2):
    """Original detector + CPU re-arm after `latency_cycles`: fraction of
    packets whose bytes (after SYNC) come out wrong."""
    rng = random.Random(seed)
    bad = total = 0
    for _ in range(trials):
        pk = [random_packet(rng) for _ in range(4)]
        gaps = [gap_bits] * len(pk)
        cpu = OrigCpu(latency_cycles)
        got, _ = simulate(edge_detector_orig(), pk, gaps, rng, 0, cpu)
        cpu.packets.append(list(got))
        for want, g in zip(pk, cpu.packets):
            total += 1
            if g[1:] != list(want):
                bad += 1
    return bad / max(total, 1)


def count_triggers(edge_prog, data, rng):
    """Decoder triggers caused by one packet (bits on the wire incl. SYNC
    and stuffing, versus what the detector emitted)."""
    ep, ewt, ewtop = edge_prog
    edge = SM("edge", ep, ewt, ewtop, 1, 0, False, False, None)
    edge.pc = 1
    irq = [0] * 8
    states = line_states([data], [20])
    t, edges = rng.random() * BIT_CYCLES, []
    for st, nb in states:
        edges.append((t, st))
        t += nb * BIT_CYCLES
    total, trig, i, cur, nxt = int(t) + 100, 0, 0, J, 0.0
    for c in range(total):
        while i < len(edges) and edges[i][0] <= c:
            cur = edges[i][1]
            i += 1
        if c >= nxt:
            before = irq[TRIG]
            edge.step(PIN[cur], irq)
            if irq[TRIG] and not before:
                trig += 1
                irq[TRIG] = 0           # consumed at once by the decoder
            nxt += 2.5
    return trig, len(packet_bits(data))


def check_receiver(trials, drifts, slew_ns, seed):
    """single_sm_receiver(), no CPU: the FIFO must hold exactly the packets
    back to back, each followed by its end marker."""
    rng = random.Random(seed)
    bad, packets = 0, 0
    for _ in range(trials):
        pk = [random_packet(rng) for _ in range(rng.randrange(2, 8))]
        gaps = [rng.choice([1, 1, 1, 2, 3, 10, 40]) for _ in pk]
        got, marks = simulate_single(pk, gaps, rng, rng.choice(drifts), slew_ns=slew_ns)
        packets += len(pk)
        if got != expected_stream(pk, markers=True) or len(marks) != len(pk):
            bad += 1
    return bad, packets


def main():
    trials = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    rng = random.Random(0)
    res = [count_triggers(edge_detector_orig(), random_packet(rng), rng) for _ in range(100)]
    print("original receiver: decoder triggers per packet minus wire bits: %s"
          % sorted(set(t - b for t, b in res)))
    print("original receiver, CPU re-arm after EOP -> wrong packets:")
    for lat_ns in (150, 250, 350, 500):
        cyc = int(lat_ns * SYS_HZ / 1e9)
        row = ["%5.1f%%" % (100 * check_original(cyc, gap, trials=40)) for gap in (1, 2, 4)]
        print("  latency %3d ns, idle 2/3/5 bits after EOP: %s" % (lat_ns, " ".join(row)))
    ok = True
    for label, drifts, slew, must_pass in (
            ("spec (+-2500 ppm, rise <= 20 ns)", [-2500, -1250, 0, 1250, 2500], 20, True),
            ("2x clock error (+-5000 ppm)", [-5000, 5000], 20, False),
            ("1.5x rise time (30 ns)", [-2500, 2500], 30, False)):
        bad, n = check_receiver(trials, drifts, slew, 99)
        print("single-SM receiver, %-34s %d of %d sequences wrong (%d packets)" % (label + ":", bad, trials, n))
        if must_pass and bad:
            ok = False
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
